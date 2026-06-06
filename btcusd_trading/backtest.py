"""BTC self-rejection backtest runner (pure replay; places no live orders).

Generates closed-candle rejection signals from a BTC M1 archive, feeds them
through the SAME generate -> format -> parse -> engine path the live runner will
use, and writes an Excel report. Refuses to run until btcusd_trading.strategy is
configured from mt5-info (assert_configured), so it can never replay placeholder
values.

PowerShell (conda env `trading`):
    python -m btcusd_trading.backtest --charts "data/BTCUSD_M1_*_ELEV8.csv" --output reports/btc_self
"""
from __future__ import annotations

import argparse
import glob
import tempfile
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


def run(chart_paths: list[str], output_dir: str) -> dict:
    assert_configured()  # never replay on placeholder strategy values

    # BTC spread->price uses BTC's tick size, not gold's 0.01 default.
    chart = CsvChartSource(_expand(chart_paths), point_value=BTC_SPEC.point_value)

    bars = list(chart.bars_between(chart.first_time(), chart.last_time()))
    generated = generate_rejection_signals(bars, BTC_REJECTION_CONFIG)

    # Round-trip GeneratedSignal -> canonical signal text -> Signal so the
    # backtest consumes exactly what the live generator emits (one parse path,
    # no second signal model).
    tmp = tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8")
    try:
        tmp.write(format_generated_signals(generated))
        tmp.close()
        signals = parse_signals_file(Path(tmp.name))
    finally:
        Path(tmp.name).unlink(missing_ok=True)

    result = run_backtest(
        signals, chart, BTC_STRATEGY_CONFIG,
        contract_size=BTC_SPEC.contract_size,
    )
    out_path = write_backtest_outputs(result, Path(output_dir))
    print(f"Generated {len(generated)} signal(s); backtested {len(signals)}. Report: {out_path}")
    return result


def main() -> None:
    ap = argparse.ArgumentParser(description="BTC self-rejection backtest (pure replay).")
    ap.add_argument("--charts", nargs="+", required=True, help="BTC M1 CSV path(s) or glob(s).")
    ap.add_argument("--output", default="reports/btc_self", help="Output stem/dir for the .xlsx.")
    args = ap.parse_args()
    run(args.charts, args.output)


if __name__ == "__main__":
    main()