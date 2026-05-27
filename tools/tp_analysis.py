#!/usr/bin/env python3
"""TP-touch and SL-lock cost analysis for the XAUUSD strategy.

Three questions the headline backtest does not answer:

  1. How often does price actually reach TP1 / TP2 / TP3 after a signal
     fires? (Pure price action, spread-aware, using the engine's own
     `target_trigger`.)

  2. Of the signals whose price reaches TP1, how many run on to TP2 --
     and how many minutes later? That gap is the window in which the
     SL-to-TP1 lock can cut a winner short.

  3. What does `lock_after_tp1` cost or save? Every signal is replayed
     twice through the validated simulator -- once with the lock ON,
     once OFF -- and the totals compared. This is the rigorous answer
     to "is the lock leaving TP2 money on the table".

Per-signal P&L in the A/B test uses a fixed, non-compounded equity, so
the two columns are directly comparable; read the gap between them, not
the absolute dollars.

Usage (run from repo root):
    python tools/tp_analysis.py --signals signals.txt \\
                                --charts data/XAUUSD_M1_*.csv
"""
from __future__ import annotations

import argparse
import glob
import sys
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

# tools/ -> repo root on sys.path, same idiom as tools/sweep.py
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from xauusd_trading import (  # noqa: E402
    CsvChartSource, DEFAULT_CONFIG, parse_signals_file,
    open_position, advance_bars, iter_bars, slice_bars,
    position_status, target_trigger,
)


def _expand_chart_paths(patterns: list[str]) -> list[str]:
    paths: list[str] = []
    for pat in patterns:
        if any(c in pat for c in "*?["):
            matches = sorted(glob.glob(pat))
            if not matches:
                raise SystemExit(f"No files match pattern: {pat}")
            paths.extend(matches)
        elif not Path(pat).exists():
            raise SystemExit(f"Chart file not found: {pat}")
        else:
            paths.append(pat)
    return paths


def _first_touches(signal, bars) -> dict:
    """First touch time of TP1/TP2/TP3 over `bars` (None if untouched)."""
    out = {"TP1": None, "TP2": None, "TP3": None}
    levels = (("TP1", signal.tp1), ("TP2", signal.tp2), ("TP3", signal.tp3))
    for bar in bars:
        for name, lvl in levels:
            if out[name] is None and target_trigger(
                    signal.side, bar.high, bar.low, lvl, bar.spread_price):
                out[name] = bar.time
        if all(v is not None for v in out.values()):
            break
    return out


def _replay(signal, equity, config, chart_df, chart_end):
    """Replay one signal through its full possible lifetime."""
    pos = open_position(signal, equity, config)
    end = pos.expiry_time + timedelta(minutes=config.max_hold_minutes + 5)
    if end > chart_end:
        end = chart_end
    advance_bars(
        pos, iter_bars(slice_bars(chart_df, pos.activation_time, end)), config)
    return pos


def _pct(n: int, d: int) -> str:
    return f"{(100.0 * n / d):.1f}%" if d else "-"


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--signals", required=True)
    ap.add_argument("--charts", required=True, nargs="+")
    ap.add_argument("--equity", type=float,
                    default=DEFAULT_CONFIG.initial_capital,
                    help="Fixed per-signal equity for the lock A/B replay "
                         "(non-compounded, so per-signal P&L is comparable).")
    args = ap.parse_args()

    signals = parse_signals_file(Path(args.signals))
    chart = CsvChartSource(_expand_chart_paths(args.charts))
    chart_df = chart.dataframe
    chart_start = chart.first_time()
    chart_end = chart.last_time()
    if chart_start is None or chart_end is None:
        raise SystemExit("Chart has no bars.")

    cfg_lock = DEFAULT_CONFIG                                  # lock ON
    cfg_nolock = replace(DEFAULT_CONFIG, lock_after_tp1=False)  # lock OFF

    eligible = [s for s in signals
                if chart_start <= s.signal_time_chart <= chart_end]
    if not eligible:
        raise SystemExit(
            "No signals fall inside the chart date range. "
            "Check that --charts covers the --signals dates.")

    # ---- accumulators ----
    n_tp1 = n_tp2 = n_tp3 = 0
    n_tp1_to_tp2 = 0
    cont_minutes: list[float] = []
    cont_within = {5: 0, 15: 0, 30: 0, 60: 0}

    lock_pnl = nolock_pnl = 0.0
    lock_w = lock_l = nolock_w = nolock_l = 0
    lock_status: dict[str, int] = {}
    nolock_status: dict[str, int] = {}
    n_stage1 = 0
    lockcapped = 0

    for s in eligible:
        # window = the position's full possible lifetime
        activation = s.signal_time_chart + timedelta(
            minutes=cfg_lock.activation_delay_minutes)
        win_end = activation + timedelta(
            minutes=cfg_lock.pending_expiry_minutes
                    + cfg_lock.max_hold_minutes + 5)
        if win_end > chart_end:
            win_end = chart_end
        bars = list(iter_bars(slice_bars(chart_df, activation, win_end)))

        # --- sections 1 & 2: raw price-action touches ---
        t = _first_touches(s, bars)
        if t["TP1"]:
            n_tp1 += 1
        if t["TP2"]:
            n_tp2 += 1
        if t["TP3"]:
            n_tp3 += 1
        if t["TP1"] and t["TP2"] and t["TP2"] >= t["TP1"]:
            n_tp1_to_tp2 += 1
            gap = (t["TP2"] - t["TP1"]).total_seconds() / 60.0
            cont_minutes.append(gap)
            for thr in cont_within:
                if gap <= thr:
                    cont_within[thr] += 1

        # --- section 3: lock A/B replay ---
        pos_l = _replay(s, args.equity, cfg_lock, chart_df, chart_end)
        st_l, pnl_l = position_status(pos_l)
        lock_pnl += pnl_l
        if st_l == "WIN":
            lock_w += 1
        elif st_l == "LOSS":
            lock_l += 1
        if pos_l.stage >= 1:
            n_stage1 += 1
        for e in pos_l.entries:
            lock_status[e.status] = lock_status.get(e.status, 0) + 1
        for e in pos_l.entries:
            if (e.status == "LOCK_TP1" and e.exit_time is not None
                    and t["TP2"] and t["TP2"] > e.exit_time):
                lockcapped += 1
                break

        pos_n = _replay(s, args.equity, cfg_nolock, chart_df, chart_end)
        st_n, pnl_n = position_status(pos_n)
        nolock_pnl += pnl_n
        if st_n == "WIN":
            nolock_w += 1
        elif st_n == "LOSS":
            nolock_l += 1
        for e in pos_n.entries:
            nolock_status[e.status] = nolock_status.get(e.status, 0) + 1

    # ---- report ----
    n = len(eligible)
    print()
    print("=" * 68)
    print("TP-TOUCH & SL-LOCK ANALYSIS")
    print("=" * 68)
    print(f"Chart range:        {chart_start}  ->  {chart_end}")
    print(f"Eligible signals:   {n}  (of {len(signals)} parsed)")
    print(f"Per-signal equity:  ${args.equity:,.2f}  (fixed, non-compounded)")
    print()

    print("-" * 68)
    print("1. PRICE REACHES TP LEVEL  (raw price action, from signal time)")
    print("-" * 68)
    print(f"  TP1 touched:   {n_tp1:>4}   {_pct(n_tp1, n)}")
    print(f"  TP2 touched:   {n_tp2:>4}   {_pct(n_tp2, n)}")
    print(f"  TP3 touched:   {n_tp3:>4}   {_pct(n_tp3, n)}")
    print(f"  (TP1 touched while a backtest position was open: "
          f"{n_stage1}  {_pct(n_stage1, n)})")
    print()

    print("-" * 68)
    print("2. TP1 -> TP2 CONTINUATION  (of signals that touched TP1)")
    print("-" * 68)
    if n_tp1:
        print(f"  Touched TP1, then later touched TP2:  {n_tp1_to_tp2}  "
              f"({_pct(n_tp1_to_tp2, n_tp1)} of TP1-touchers)")
        print(f"    TP2 within  5 min of TP1:  {cont_within[5]:>4}   "
              f"{_pct(cont_within[5], n_tp1)}")
        print(f"    TP2 within 15 min of TP1:  {cont_within[15]:>4}   "
              f"{_pct(cont_within[15], n_tp1)}")
        print(f"    TP2 within 30 min of TP1:  {cont_within[30]:>4}   "
              f"{_pct(cont_within[30], n_tp1)}")
        print(f"    TP2 within 60 min of TP1:  {cont_within[60]:>4}   "
              f"{_pct(cont_within[60], n_tp1)}")
        if cont_minutes:
            sm = sorted(cont_minutes)
            med = sm[len(sm) // 2]
            print(f"  Median TP1 -> TP2 gap: {med:.0f} min")
    else:
        print("  No signal touched TP1.")
    print()

    print("-" * 68)
    print("3. LOCK_AFTER_TP1  ON vs OFF  (every signal replayed both ways)")
    print("-" * 68)
    statuses = sorted(set(lock_status) | set(nolock_status))
    print(f"  {'entry status':<14}{'lock ON':>12}{'lock OFF':>12}")
    for st in statuses:
        print(f"  {st:<14}{lock_status.get(st, 0):>12}"
              f"{nolock_status.get(st, 0):>12}")
    print(f"  {'-' * 38}")
    print(f"  {'signal wins':<14}{lock_w:>12}{nolock_w:>12}")
    print(f"  {'signal losses':<14}{lock_l:>12}{nolock_l:>12}")
    print(f"  {'total P&L':<14}{('$%+.2f' % lock_pnl):>12}"
          f"{('$%+.2f' % nolock_pnl):>12}")
    delta = nolock_pnl - lock_pnl
    print()
    verdict = ("the lock is COSTING you" if delta > 0
               else "the lock is PROTECTING you" if delta < 0
               else "the lock is neutral")
    print(f"  Turning the lock OFF changes total P&L by ${delta:+.2f}")
    print(f"  -> on this data, {verdict}.")
    print(f"  LOCK_TP1 exits cut off before a later TP2 touch (approx): "
          f"{lockcapped}")
    print("=" * 68)
    print()
    print("Notes:")
    print("  - Sections 1 & 2 are raw price action; they ignore whether an")
    print("    entry actually filled -- they describe the market, not the")
    print("    strategy.")
    print("  - Section 3 is the rigorous lock comparison: same simulator,")
    print("    same fills, only lock_after_tp1 differs.")
    print("  - P&L is fixed-equity / non-compounded -- compare the two")
    print("    columns against each other, not against your live equity.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())