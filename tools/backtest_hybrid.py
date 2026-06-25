#!/usr/bin/env python3
"""Hybrid backtest: TICK fills where tick data covers a signal, M1 elsewhere.

Same signal/chart/strategy contract as ``tools/backtest_explicit.py`` (it reuses
that tool's parser + config), PLUS ``--ticks``. For EACH signal, in chronological
order, it picks the best available data:

  * if the committed tick archive covers the signal's lifecycle window
    (``data/ticks/XAUUSD_TICK_*_ELEV8.csv``), the signal is evaluated on the REAL
    ``Mt5Executor`` against those ticks (exactly as ``tools/tick_backtest.py``) --
    the closest-to-live fills;
  * otherwise it falls back to the M1 OHLC engine (the exact ``run_backtest`` row
    construction, reused verbatim via ``replay_signal_rows``).

Equity compounds across the interleaved tick/M1 signals in one curve, and the
combined result is aggregated with the SAME ``aggregate_backtest_result`` the M1
backtest uses, so the workbook (Summary / Daily / Per-Entry) is byte-shape
identical -- with one extra **Data Source** column tagging each row TICK or M1.

WHY: ticks are the truth for fills (the M1 engine over-states locked exits); see
sweep_reports/tick_r4_full and docs/BACKTEST_REALISM.md. Tick data currently
covers only 2026-05..2026-06, so 2026-06 runs are pure TICK, pre-2026-05 runs are
pure M1, and a window spanning the boundary is mixed -- automatically.

Parity: with NO ticks in range (or ``--ticks`` omitted) the output is identical to
``backtest_explicit.py`` (tests/test_backtest_hybrid_parity.py pins this), so the
existing M1 CLI behaviour is unchanged.
"""
from __future__ import annotations

import argparse
import sys
from datetime import timedelta
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
for p in (str(ROOT), str(ROOT / "tools")):
    if p not in sys.path:
        sys.path.insert(0, p)

from trading.engine import (  # noqa: E402
    CsvChartSource,
    aggregate_backtest_result,
    parse_signals_file,
    replay_signal_rows,
    screen_signal,
    write_backtest_outputs,
)
from trading.engine.strategy.backtest import _atr_lookup, _realized_rr  # noqa: E402

# Reuse the M1 tool's parser + config + date filter (one source of truth for the
# strategy contract flags), and the tick tool's real-executor machinery.
import backtest_explicit as bx  # noqa: E402
import tick_backtest as tk  # noqa: E402


def _entry_number_from_comment(comment: str) -> int | None:
    """Map a broker deal back to its engine entry via the ``.N`` comment suffix
    (mt5_entry_comment renders ``[TAG-]MMDD#DD.N``; the suffix is never trimmed)."""
    if not comment:
        return None
    tail = comment.rsplit(".", 1)
    if len(tail) == 2 and tail[1].isdigit():
        return int(tail[1])
    return None


def _classify_tick_entry(reason: str, pnl: float, close_px: float, sig) -> str:
    """Best-effort engine entry-status for a tick deal (broker reasons are coarse:
    SL / TP / market_close). Prices are real, so this only labels the exit kind."""
    if reason == "market_close":
        return "TIME_EXIT"
    if reason == "SL":
        # A stop that filled in profit is a locked floor (TP1-lock / BEP), not a loss.
        return "LOCK_TP1" if pnl > 0 else "SL"
    if reason == "TP":
        for lvl, name in ((sig.tp3, "TP3"), (sig.tp2, "TP2"), (sig.tp1, "TP1")):
            if lvl and abs(close_px - lvl) <= max(0.5, abs(lvl) * 1e-4):
                return name
        return "TP1"
    return "TP1"


def _tick_signal_rows(sig, chart, equity, sig_config, base_config, ticks,
                      symbol, watch_seconds, clock):
    """Evaluate ONE signal on ticks via the real executor and return rows in the
    exact run_backtest shape (row, entry_rows, status, equity_after, data_source).

    The canonical entry geometry (planned entry/SL/targets, original levels,
    rr_planned) is taken from the M1 replay (replay_signal_rows); the EXECUTED
    columns (fill/exit price+time, lot, P&L, realized R, status) are overlaid from
    the real tick deals, matched to entries by the ``.N`` comment suffix. Returns
    ``None`` if ticks do not actually cover the window (caller falls back to M1)."""
    res = tk.run_signal(sig, sig_config, chart, ticks, symbol, watch_seconds, clock)
    if res.get("no_ticks"):
        return None

    # Canonical M1 rows give every planned/original column + rr_planned.
    base = replay_signal_rows(sig, chart.dataframe, equity, sig_config, base_config)
    row = base["row"]
    entry_rows = base["entry_rows"]

    deals = res.get("deals", [])
    by_entry: dict[int, dict] = {}
    for d in deals:
        n = _entry_number_from_comment(d.get("comment", ""))
        if n is not None and n not in by_entry:
            by_entry[n] = d  # one closing deal per entry leg

    open_left = res.get("open_left", 0)
    pending_left = res.get("pending_left", 0)
    trading = float(res.get("trading", 0.0))
    bonus = float(res.get("bonus", 0.0))
    total = trading + bonus
    closed_lots = sum(float(d["volume"]) for d in deals)
    bonus_rate = float(getattr(base_config, "bonus_per_closed_lot", 0.0) or 0.0)

    if open_left or pending_left:
        status = "OPEN"
    elif not deals:
        status = "NO_FILL"
    elif trading > 0:
        status = "WIN"
    elif trading < 0:
        status = "LOSS"
    else:
        status = "BREAKEVEN"

    equity_after = equity if status == "OPEN" else equity + total

    # Overlay executed (tick) fields onto each canonical entry row.
    for er in entry_rows:
        n = er.get("entry_number")
        d = by_entry.get(n)
        er["data_source"] = "TICK"
        er["equity_before"] = equity
        er["equity_after"] = equity_after
        er["signal_status"] = status
        if d is None:
            # No closing deal for this leg: open (best-effort) or never filled.
            er["entry_status"] = "OPEN" if (open_left or pending_left) else "NO_FILL"
            for k in ("fill_time", "exit_time", "exit_price"):
                er[k] = None
            er["trading_pnl"] = None
            er["closed_lots"] = 0.0
            er["bonus"] = 0.0
            er["pnl"] = None
            er["rr"] = None
            continue
        fill_t = pd.Timestamp(d["open_ms"], unit="ms").to_pydatetime() if d.get("open_ms") else None
        exit_t = pd.Timestamp(d["close_ms"], unit="ms").to_pydatetime() if d.get("close_ms") else None
        leg_lots = float(d["volume"])
        leg_bonus = leg_lots * bonus_rate
        er["entry_price"] = float(d["open"])
        er["exit_price"] = float(d["close"])
        er["fill_time"] = fill_t
        er["exit_time"] = exit_t
        er["lot"] = leg_lots
        er["closed_lots"] = leg_lots
        er["trading_pnl"] = float(d["pnl"])
        er["bonus"] = leg_bonus
        er["pnl"] = float(d["pnl"]) + leg_bonus
        er["entry_status"] = _classify_tick_entry(d.get("reason", ""), float(d["pnl"]),
                                                  float(d["close"]), sig)
        er["rr"] = _realized_rr(sig.side, float(d["open"]), er.get("effective_SL"),
                                float(d["close"]), filled=True)

    row["data_source"] = "TICK"
    row["status"] = status
    row["pnl"] = total if status != "OPEN" else None
    row["trading_pnl"] = trading if status != "OPEN" else None
    row["bonus"] = bonus if status != "OPEN" else None
    row["closed_lots"] = closed_lots
    row["equity_before"] = equity
    row["equity_after"] = equity_after
    return {"row": row, "entry_rows": entry_rows, "status": status,
            "equity_after": equity_after, "data_source": "TICK"}


def _covered(tick_times: np.ndarray, sig, config, chart_end) -> bool:
    """True if the tick archive has any tick in the signal's lifecycle window
    (the SAME guard tick_backtest.run_signal uses to decide it can tick-replay)."""
    if tick_times.size == 0:
        return False
    sim_start = sig.signal_time_chart
    sim_end = sim_start + timedelta(minutes=config.pending_expiry_minutes
                                    + config.max_hold_minutes + 5)
    if chart_end is not None and sim_end > chart_end:
        sim_end = chart_end
    lo = np.searchsorted(tick_times, np.datetime64(sim_start), side="left")
    hi = np.searchsorted(tick_times, np.datetime64(sim_end), side="right")
    return hi > lo


def run_hybrid_backtest(signals, chart, ticks, config, *, symbol="XAUUSD",
                        watch_seconds=3, exclude_structural_anomalies=False,
                        config_resolver=None, progress=None):
    """Per-signal tick-preferred / M1-fallback backtest. Returns a result dict in
    the run_backtest shape, with ``data_source`` on every row + a ``data_sources``
    summary. Mirrors run_backtest's screen->replay->aggregate flow, swapping the
    per-signal engine by tick coverage."""
    chart_df = chart.dataframe
    chart_start = chart.first_time()
    chart_end = chart.last_time()
    tick_times = (ticks["time"].values.astype("datetime64[ns]")
                  if ticks is not None and len(ticks) else np.array([], dtype="datetime64[ns]"))

    # tick_backtest._install_sim_clock() monkeypatches the live/trailing executor
    # modules' _wall_clock_chart_now GLOBALLY; save the originals and restore them
    # in a finally so a hybrid run never leaks the sim clock into other code/tests.
    clock = None
    _orig_clocks = None
    if tick_times.size:
        import trading.engine.execution.mt5_executor_live as _live_mod
        import trading.engine.execution.mt5_executor_trailing as _trail_mod
        _orig_clocks = (_live_mod, _live_mod._wall_clock_chart_now,
                        _trail_mod, _trail_mod._wall_clock_chart_now)
        clock = tk._install_sim_clock()

    try:
        return _run_hybrid_loop(
            signals, chart, chart_df, chart_start, chart_end, ticks, tick_times,
            clock, config, symbol, watch_seconds, exclude_structural_anomalies,
            config_resolver, progress)
    finally:
        if _orig_clocks is not None:
            lm, lf, tm, tf = _orig_clocks
            lm._wall_clock_chart_now = lf
            tm._wall_clock_chart_now = tf


def _run_hybrid_loop(signals, chart, chart_df, chart_start, chart_end, ticks,
                     tick_times, clock, config, symbol, watch_seconds,
                     exclude_structural_anomalies, config_resolver, progress):
    atr_at = (_atr_lookup(chart_df, config.atr_period)
              if getattr(config, "sl_source", "signal") == "atr" else None)

    equity = config.initial_capital
    rows: list[dict] = []
    entry_rows: list[dict] = []
    excluded: list[dict] = []
    n_tick = n_m1 = 0

    for i, sig in enumerate(signals):
        screened, reason = screen_signal(
            sig, config, chart_start, chart_end, atr_at=atr_at,
            exclude_structural_anomalies=exclude_structural_anomalies,
            config_resolver=config_resolver)
        if screened is None:
            excluded.append({"signal_key": sig.signal_key, "reason": reason})
            continue
        tsig, sig_config = screened

        built = None
        if tick_times.size and _covered(tick_times, tsig, sig_config, chart_end):
            built = _tick_signal_rows(tsig, chart, equity, sig_config, config,
                                      ticks, symbol, watch_seconds, clock)
        if built is None:  # no ticks (or not actually covered) -> M1 fallback
            built = replay_signal_rows(tsig, chart_df, equity, sig_config, config)
            built["data_source"] = "M1"
            built["row"]["data_source"] = "M1"
            for er in built["entry_rows"]:
                er["data_source"] = "M1"
            n_m1 += 1
        else:
            n_tick += 1

        rows.append(built["row"])
        entry_rows.extend(built["entry_rows"])
        if built["status"] != "OPEN":
            equity = built["equity_after"]
        if progress is not None:
            progress(i + 1, len(signals), n_tick, n_m1)
        if equity <= 0:
            break

    result = aggregate_backtest_result(
        rows, entry_rows, excluded, config, chart_df, chart_start, chart_end,
        equity, len(signals))
    result["data_sources"] = {"tick_signals": n_tick, "m1_signals": n_m1}
    return result


def build_parser() -> argparse.ArgumentParser:
    p = bx.build_parser()
    p.description = ("Hybrid backtest: TICK fills where tick data covers a signal, "
                     "M1 elsewhere. Same flags as backtest_explicit plus --ticks.")
    p.add_argument("--ticks", nargs="+",
                   default=["data/ticks/XAUUSD_TICK_*_ELEV8.csv"],
                   help="Tick CSV glob(s) (ELEV8 tab format). Default: the committed "
                        "data/ticks archive. Signals whose window is covered run on "
                        "real ticks; the rest fall back to M1.")
    p.add_argument("--watch-seconds", type=int, default=3,
                   help="Tick poll cadence for the real executor (matches live "
                        "--watch-interval 3). Default 3.")
    # --mt5-symbol is already defined by backtest_explicit's chart-sync args; reused.
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = bx.config_from_args(args)

    signals = bx.filter_signals_by_date(
        parse_signals_file(Path(args.signals)), args.start_date, args.end_date,
        args.date_tz)
    chart = CsvChartSource(bx._expand_chart_paths(args.charts))

    tick_paths = tk._expand(args.ticks)
    ticks = tk.load_ticks(tick_paths) if tick_paths else None
    if ticks is not None and len(ticks):
        print(f"ticks: {len(ticks)} rows  {ticks['time'].iloc[0]} -> {ticks['time'].iloc[-1]} "
              f"({len(tick_paths)} file(s))")
    else:
        print("ticks: none found -> pure M1 backtest (identical to backtest_explicit)")

    def _progress(done, total, nt, nm):
        if done == total or done % 100 == 0:
            print(f"  [{done}/{total}] tick={nt} m1={nm}", flush=True)

    result = run_hybrid_backtest(
        signals, chart, ticks, config, symbol=args.mt5_symbol,
        watch_seconds=args.watch_seconds,
        exclude_structural_anomalies=args.exclude_structural_anomalies,
        progress=_progress)

    ds = result.get("data_sources", {})
    print(f"\n=== HYBRID BACKTEST ===")
    print(f"  signals: {result['signals_included']} included "
          f"({ds.get('tick_signals', 0)} TICK, {ds.get('m1_signals', 0)} M1)  "
          f"{result['signals_excluded']} excluded")
    print(f"  net P&L: ${result['net_profit']:.2f}   final equity: ${result['final_equity']:.2f}")
    print(f"  max drawdown: {result['max_drawdown_pct']:.2f}%   "
          f"win rate: {result['win_rate_pct']:.1f}%")
    print(f"  wins={result['wins']} losses={result['losses']} "
          f"breakevens={result['breakevens']} no_fills={result['no_fills']} open={result['open']}")

    path = write_backtest_outputs(result, Path(args.output_dir))
    print(f"  report: {path}")

    dd = abs(result["max_drawdown_pct"])
    if getattr(args, "fail_on_drawdown_limit", False) and dd > args.max_drawdown_limit_pct:
        print(f"  DRAWDOWN {dd:.2f}% exceeds limit {args.max_drawdown_limit_pct}%", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
