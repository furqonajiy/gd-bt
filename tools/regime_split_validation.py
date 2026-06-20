#!/usr/bin/env python3
"""Regime-split validation (research): is the deployed R4 champion volatility-
scale-invariant across the two halves of 2026?

The regime-granularity assessment (docs/REGIME_ASSESSMENT.md) showed the live
absolute-ATR regime metric is price-biased and that today's single R4 band spans
two very different markets — 2026 Jan-Mar (extreme vol, uptrend) vs Apr-Jun (high
vol, downtrend). This script tests whether that mislabelling matters: it runs the
deployed champion (SQZ6 = rsi75_sqz6_rr40) on each half at fixed 0.01 lot and
compares per-trade edge. If the edge is the same, splitting R4 would not change
champion selection (the strategy self-normalizes via its ATR geometry), so the
regime/champion mapping should NOT be tuned.

Read-only. Generates a temp feed, runs two backtests, prints the comparison.
Run: ``python tools/regime_split_validation.py``.
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import trading.xauusd as x  # noqa: E402

# Deployed R4 champion (SQZ6), fixed 0.01 lot for a clean per-trade edge, with the
# measured locked-exit slippage so fills are realistic (cli_champion_R4_SQZ6).
CHAMPION = dict(
    sizing_mode="fixed", lot_per_entry=0.01, minimum_lot=0.01, lot_step=0.01,
    bonus_per_closed_lot=0.0, entry_count=8, entry_ladder="range_to_sl",
    entry_sl_gap=0.5, activation_delay_minutes=2, pending_expiry_minutes=180,
    max_hold_minutes=240, sl_multiplier=2.1, final_target="TP3",
    lock_after_tp1=True, lock_after_tp2=True, tp1_lock_delay_minutes=24,
    tp2_lock_delay_minutes=2, profit_lock_mode="tp_levels", bep_trigger_distance=3.0,
    tp1_lock_fraction=0.5, tp2_lock_target="TP1", runner_after_tp3=False,
    tp3_lock_target="TP2", shared_sl=False, trailing_open_distance=0.0,
    trailing_close_distance=0.0, lock_tp1_exit_slippage_points=2.0,
    lock_tp2_exit_slippage_points=1.0, initial_capital=50000.0,
)
HALVES = {"V4-extreme Jan-Mar": {"2026-01", "2026-02", "2026-03"},
          "V3-high   Apr-Jun": {"2026-04", "2026-05", "2026-06"}}


def main() -> int:
    charts = sorted(Path("data").glob("XAUUSD_M1_2026*_ELEV8.csv"))
    if not charts:
        print("no 2026 ELEV8 charts found", file=sys.stderr)
        return 1
    feed = Path(tempfile.gettempdir()) / "sqz6_2026_validation.txt"
    subprocess.run([sys.executable, "tools/generate_scalper_signals.py",
                    "--charts", *map(str, charts), "--output", str(feed),
                    "--start", "2026-01-01", "--session-start", "0", "--session-end", "0",
                    "--signal-tz", "7", "--rsi-buy-max", "75", "--rsi-sell-min", "25",
                    "--bb-bandwidth-min", "0.0006", "--rr1", "1.0", "--rr2", "2.0",
                    "--rr3", "4.0", "--progress-interval-seconds", "0"],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    cfg = x.StrategyConfig(**CHAMPION)
    signals = x.parse_signals_file(feed)
    chart = x.CsvChartSource(charts)

    print("SQZ6 champion across the two halves of 2026 (both labelled R4parab today):\n")
    for label, months in HALVES.items():
        sub = [s for s in signals if s.signal_time_chart
               and s.signal_time_chart.strftime("%Y-%m") in months]
        r = x.run_backtest(sub, chart, cfg)
        n = max(r["signals_included"], 1)
        print(f"  {label:20} sig={r['signals_included']:4}  "
              f"edge=${r['net_profit']:8,.0f}  ${r['net_profit']/n:5.1f}/sig  "
              f"DD={r['max_drawdown_pct']:5.1f}%  "
              f"W/L={r['wins']}/{r['losses']}  wr={r.get('win_rate_pct', 0):.0f}%")
    print("\nIf $/signal is ~equal, the champion is volatility-scale-invariant -> "
          "splitting R4 would not change champion selection (do NOT tune).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
