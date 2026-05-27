"""CLI helper to count TP/SL path combinations for XAUUSD signals.

Example:

    python tools/analyze_signal_paths.py \
      --signals data/signals.txt \
      --charts data/XAUUSD_M1_*.csv \
      --output-dir reports/path_analysis \
      --activation-delay 2 \
      --pending-expiry 5 \
      --max-hold 90 \
      --sl-multiplier 1.5 \
      --final-target TP3 \
      --entry-ladder signal_range_3 \
      --near-tp1-dollars 1.0
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import glob
import sys

# Allow running as `python tools/analyze_signal_paths.py` from repo root.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from xauusd_trading import CsvChartSource, DEFAULT_CONFIG, StrategyConfig, parse_signals_file
from xauusd_trading.strategy.path_analysis import (
    run_path_analysis,
    write_path_analysis_outputs,
)


def _expand_chart_paths(patterns: list[str]) -> list[Path]:
    out: list[Path] = []
    for pat in patterns:
        if any(ch in pat for ch in "*?["):
            matches = sorted(glob.glob(pat))
            if not matches:
                raise SystemExit(f"No files match pattern: {pat}")
            out.extend(Path(m) for m in matches)
        else:
            path = Path(pat)
            if not path.exists():
                raise SystemExit(f"Chart file not found: {pat}")
            out.append(path)
    return out


def _config_from_args(args: argparse.Namespace) -> StrategyConfig:
    return StrategyConfig(
        initial_capital=args.initial_capital,
        sizing_mode=args.sizing_mode,
        lot_per_entry=args.lot,
        risk_per_signal=args.risk,
        entry_count=args.entries,
        entry_ladder=args.entry_ladder,
        entry_sl_gap=args.entry_sl_gap,
        activation_delay_minutes=args.activation_delay,
        pending_expiry_minutes=args.pending_expiry,
        max_hold_minutes=args.max_hold,
        sl_multiplier=args.sl_multiplier,
        final_target=args.final_target,
        lock_after_tp1=not args.no_lock_after_tp1,
        lock_after_tp2=not args.no_lock_after_tp2,
        minimum_lot=args.minimum_lot,
        lot_step=args.lot_step,
    )


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="analyze_signal_paths",
        description="Count TP/SL path combinations such as NEAR_TP1->SL and TP1->TP2->TP3.",
    )
    p.add_argument("--signals", required=True, help="Signal text file.")
    p.add_argument("--charts", required=True, nargs="+", help="One or more MT5 M1 chart CSV files.")
    p.add_argument("--output-dir", default=None, help="Optional directory for CSV outputs.")
    p.add_argument("--exclude-structural-anomalies", action="store_true")
    p.add_argument(
        "--near-tp1-dollars", type=float, default=1.0,
        help="Classify a signal as 'almost TP1' if price comes within this many dollars of TP1.",
    )

    p.add_argument("--initial-capital", type=float, default=DEFAULT_CONFIG.initial_capital)
    p.add_argument("--sizing-mode", default=DEFAULT_CONFIG.sizing_mode, choices=["fixed", "risk"])
    p.add_argument("--lot", type=float, default=DEFAULT_CONFIG.lot_per_entry)
    p.add_argument("--risk", type=float, default=DEFAULT_CONFIG.risk_per_signal)
    p.add_argument("--entries", type=int, default=DEFAULT_CONFIG.entry_count)
    p.add_argument("--entry-ladder", default=DEFAULT_CONFIG.entry_ladder,
                   choices=["signal_range_3", "range_uniform", "range_to_sl"])
    p.add_argument("--entry-sl-gap", type=float, default=DEFAULT_CONFIG.entry_sl_gap)
    p.add_argument("--activation-delay", type=int, default=DEFAULT_CONFIG.activation_delay_minutes)
    p.add_argument("--pending-expiry", type=int, default=DEFAULT_CONFIG.pending_expiry_minutes)
    p.add_argument("--max-hold", type=int, default=DEFAULT_CONFIG.max_hold_minutes)
    p.add_argument("--sl-multiplier", type=float, default=DEFAULT_CONFIG.sl_multiplier)
    p.add_argument("--final-target", default=DEFAULT_CONFIG.final_target,
                   choices=["TP1", "TP2", "TP3"])
    p.add_argument("--no-lock-after-tp1", action="store_true")
    p.add_argument("--no-lock-after-tp2", action="store_true")
    p.add_argument("--minimum-lot", type=float, default=DEFAULT_CONFIG.minimum_lot)
    p.add_argument("--lot-step", type=float, default=DEFAULT_CONFIG.lot_step)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = _config_from_args(args)

    signals = parse_signals_file(Path(args.signals))
    chart = CsvChartSource(_expand_chart_paths(args.charts))

    result = run_path_analysis(
        signals,
        chart,
        config,
        exclude_structural_anomalies=args.exclude_structural_anomalies,
        near_tp1_dollars=args.near_tp1_dollars,
    )

    print(json.dumps(result["summary"], indent=2, default=str))

    if args.output_dir:
        paths = write_path_analysis_outputs(result, Path(args.output_dir))
        print("\nWrote:")
        for label, path in paths.items():
            print(f"  {label}: {path.resolve()}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
