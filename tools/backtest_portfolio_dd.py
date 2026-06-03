"""Report the TRUE concurrent (mark-to-market) drawdown for a backtest config.

``run_backtest`` measures drawdown on the realized equity curve, one signal at a
time, so it never sees several positions open and underwater simultaneously --
and ``backtest_explicit.py`` gates ``--max-drawdown-limit-pct`` on that realized
number. This tool takes the SAME command line, reuses ``run_backtest``'s
per-entry fills (no new fill/exit logic), and adds the missing piece: per-bar
equity = realized + floating P&L of every position open at that bar. It reports
both drawdowns and gates the limit on the true one.

Scope: this corrects the drawdown MEASUREMENT only. It inherits run_backtest's
look-ahead lot sizing (signal N sized off signal N-1's resolved equity), and it
uses backtest exit prices, so a live account adds broker stop-level clamping and
slippage on trailing-close exits -- the true live drawdown is modestly worse.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for _p in (str(ROOT), str(ROOT / "tools")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from xauusd_trading import CsvChartSource, parse_signals_file, run_backtest  # noqa: E402
from backtest_explicit import build_parser, config_from_args, _expand_chart_paths  # noqa: E402

CONTRACT = 100.0
_DONE_STATUSES = {"OPEN", "NO_FILL"}


def _floating(side: str, entry_price: float, lot: float, close: float, spread: float) -> float:
    # value "if closed now": BUY exits at Bid (=close), SELL at Ask (=close+spread)
    if side == "BUY":
        return (close - entry_price) * lot * CONTRACT
    return (entry_price - (close + spread)) * lot * CONTRACT


def mtm_drawdown(entries, times, closes, spreads, initial):
    """Max drawdown off per-bar (realized + floating) equity. Returns (dd_pct, peak_concurrency, trough_index)."""
    fills_at, exits_at = {}, {}
    for i, e in enumerate(entries):
        fills_at.setdefault(e["fill_idx"], []).append(i)
        exits_at.setdefault(e["exit_idx"], []).append(i)
    open_set, realized = set(), initial
    peak, max_dd, trough_idx, peak_conc = initial, 0.0, None, 0
    for i in range(len(times)):
        for ei in fills_at.get(i, []):
            open_set.add(ei)
        for ei in exits_at.get(i, []):
            open_set.discard(ei)
            realized += entries[ei]["realized"]
        if open_set:
            peak_conc = max(peak_conc, len(open_set))
            c, sp = closes[i], spreads[i]
            eq = realized + sum(
                _floating(entries[ei]["side"], entries[ei]["entry_price"],
                          entries[ei]["lot"], c, sp) for ei in open_set
            )
        else:
            eq = realized
        if eq > peak:
            peak = eq
        if peak > 0:
            dd = (eq - peak) / peak * 100.0
            if dd < max_dd:
                max_dd, trough_idx = dd, i
    return max_dd, peak_conc, trough_idx


def realized_drawdown_exit_ordered(entries, initial):
    eq, peak, max_dd = initial, initial, 0.0
    for e in sorted(entries, key=lambda x: x["exit_idx"]):
        eq += e["realized"]
        if eq > peak:
            peak = eq
        if peak > 0:
            max_dd = min(max_dd, (eq - peak) / peak * 100.0)
    return max_dd


def _entries_from_result(result, tindex):
    entries, skipped = [], 0
    for r in result["entry_rows"]:
        if r["signal_status"] in _DONE_STATUSES or r["fill_time"] is None or r["exit_time"] is None:
            continue
        fi, xi = tindex.get(r["fill_time"]), tindex.get(r["exit_time"])
        if fi is None or xi is None or xi <= fi:
            skipped += 1
            continue
        entries.append(dict(side=r["side"], entry_price=float(r["entry_price"]),
                            lot=float(r["lot"]), realized=float(r["pnl"] or 0.0),
                            fill_idx=fi, exit_idx=xi))
    return entries, skipped


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = config_from_args(args)

    chart = CsvChartSource(_expand_chart_paths(args.charts))
    df = chart.dataframe
    times = [t.to_pydatetime() for t in df["time"]]
    closes = df["close"].to_numpy()
    spreads = df["spread_price"].to_numpy()
    tindex = {t: i for i, t in enumerate(times)}

    result = run_backtest(
        parse_signals_file(Path(args.signals)), chart, config,
        exclude_structural_anomalies=args.exclude_structural_anomalies,
    )

    entries, skipped = _entries_from_result(result, tindex)
    mtm_dd, peak_conc, trough_idx = mtm_drawdown(entries, times, closes, spreads, config.initial_capital)
    realized_recon = realized_drawdown_exit_ordered(entries, config.initial_capital)
    reported_dd = float(result.get("max_drawdown_pct", 0.0) or 0.0)
    limit = float(args.max_drawdown_limit_pct)
    passes_true = abs(mtm_dd) <= limit
    gap = (mtm_dd / reported_dd) if reported_dd < 0 else float("nan")
    trough_time = times[trough_idx] if trough_idx is not None else None

    print("Portfolio drawdown audit (true concurrent mark-to-market)")
    print(f"  signals included           : {result['signals_included']}   filled positions: {len(entries)}"
          + (f"   (skipped {skipped} unmappable)" if skipped else ""))
    print(f"  realized max DD (reported) : {reported_dd:.2f}%")
    print(f"  reconstructed realized DD  : {realized_recon:.2f}%  (exit-ordered sanity)")
    print(f"  TRUE mark-to-market max DD : {mtm_dd:.2f}%" + (f"   (gap {gap:.2f}x)" if gap == gap else ""))
    print(f"  peak concurrent positions  : {peak_conc}")
    print(f"  worst MtM trough           : {trough_time}")
    print(f"  --max-drawdown-limit-pct {limit:.1f} : {'PASS' if passes_true else 'FAIL'} "
          f"(true DD {abs(mtm_dd):.2f}% {'<=' if passes_true else '>'} {limit:.1f}%)")
    print(f"  final equity {result['final_equity']:.2f}   net profit {result['net_profit']:.2f}")
    print("  NOTE: inherits run_backtest look-ahead lot sizing; live trailing-close exits add")
    print("        broker stop/freeze clamp + slippage, so true live DD is modestly worse.")

    if getattr(args, "fail_on_drawdown_limit", False) and not passes_true:
        print(f"True mark-to-market drawdown {abs(mtm_dd):.2f}% exceeds limit {limit:.2f}%.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())