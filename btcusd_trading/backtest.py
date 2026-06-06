"""BTC self-rejection backtest runner (pure replay; places no live orders).

Generates closed-candle rejection signals from a BTC M1 archive, feeds them
through the SAME generate -> format -> parse -> engine path the live runner will
use, and writes an Excel report. Refuses to run until btcusd_trading.strategy is
configured from mt5-info (assert_configured), so it can never replay placeholder
values.

Geometry override flags exist so research variations stay CLI flags (the blessed
btcusd_trading.strategy config is never mutated for a research run).

PowerShell (conda env `trading`):
    python -m btcusd_trading.backtest --charts "data/BTCUSD_M1_*_ELEV8.csv" --output reports/btc_self
"""
from __future__ import annotations

import argparse
import glob
import tempfile
from dataclasses import replace
from pathlib import Path

from xauusd_trading import (
    CsvChartSource,
    format_generated_signals,
    generate_rejection_signals,
    parse_signals_file,
    run_backtest,
    write_backtest_outputs,
)

from btcusd_trading import (
    BTC_REJECTION_CONFIG,
    BTC_SPEC,
    BTC_STRATEGY_CONFIG,
    assert_configured,
)


def _expand(patterns: list[str]) -> list[Path]:
    out: list[Path] = []
    for pat in patterns:
        matches = sorted(glob.glob(pat)) if any(c in pat for c in "*?[") else [pat]
        for m in matches:
            path = Path(m)
            if not path.exists():
                raise SystemExit(f"Chart file not found: {m}")
            out.append(path)
    if not out:
        raise SystemExit("No chart files matched")
    return list({path.resolve(): path for path in out}.values())


def run(
        chart_paths: list[str], output_dir: str, *,
        entry_range_width: float | None = None,
        sl_distance: float | None = None,
        tp1_distance: float | None = None,
        tp2_distance: float | None = None,
        tp3_distance: float | None = None,
        max_hold_minutes: int | None = None,
) -> dict:
    assert_configured()  # never replay on placeholder strategy values

    # Research geometry overrides -> dataclasses.replace, never mutate the module
    # config. Realized SL from a fill = sl_distance + entry_range_width, because
    # entries fill at the range edge away from the stop; TP is measured from that
    # same edge, so realized TP = tpN_distance.
    rej_over = {k: v for k, v in {
        "entry_range_width": entry_range_width,
        "sl_distance": sl_distance,
        "tp1_distance": tp1_distance,
        "tp2_distance": tp2_distance,
        "tp3_distance": tp3_distance,
    }.items() if v is not None}
    rej = replace(BTC_REJECTION_CONFIG, **rej_over) if rej_over else BTC_REJECTION_CONFIG
    cfg = (replace(BTC_STRATEGY_CONFIG, max_hold_minutes=max_hold_minutes)
           if max_hold_minutes is not None else BTC_STRATEGY_CONFIG)

    # BTC spread->price uses BTC's tick size, not gold's 0.01 default.
    chart = CsvChartSource(_expand(chart_paths), point_value=BTC_SPEC.point_value)

    bars = list(chart.bars_between(chart.first_time(), chart.last_time()))
    generated = generate_rejection_signals(bars, rej)

    # Round-trip GeneratedSignal -> canonical signal text -> Signal so the
    # backtest consumes exactly what the live generator emits (one parse path).
    tmp = tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8")
    try:
        tmp.write(format_generated_signals(generated))
        tmp.close()
        signals = parse_signals_file(Path(tmp.name))
    finally:
        Path(tmp.name).unlink(missing_ok=True)

    result = run_backtest(
        signals, chart, cfg, contract_size=BTC_SPEC.contract_size,
    )
    out_path = write_backtest_outputs(result, Path(output_dir))
    print(f"Generated {len(generated)} signal(s); backtested {len(signals)}. Report: {out_path}")
    return result


def main() -> None:
    ap = argparse.ArgumentParser(description="BTC self-rejection backtest (pure replay).")
    ap.add_argument("--charts", nargs="+", required=True, help="BTC M1 CSV path(s) or glob(s).")
    ap.add_argument("--output", default="reports/btc_self", help="Output stem/dir for the .xlsx.")
    g = ap.add_argument_group("Research geometry overrides (omit to use the strategy config)")
    g.add_argument("--entry-range-width", type=float, default=None)
    g.add_argument("--sl-distance", type=float, default=None)
    g.add_argument("--tp1-distance", type=float, default=None)
    g.add_argument("--tp2-distance", type=float, default=None)
    g.add_argument("--tp3-distance", type=float, default=None)
    g.add_argument("--max-hold", type=int, default=None, dest="max_hold_minutes")
    args = ap.parse_args()
    run(
        args.charts, args.output,
        entry_range_width=args.entry_range_width,
        sl_distance=args.sl_distance,
        tp1_distance=args.tp1_distance,
        tp2_distance=args.tp2_distance,
        tp3_distance=args.tp3_distance,
        max_hold_minutes=args.max_hold_minutes,
    )


if __name__ == "__main__":
    main()