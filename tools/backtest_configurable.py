#!/usr/bin/env python3
"""Configurable local backtest runner for generated XAUUSD signals.

This is intentionally separate from ``xauusd_trading.cli`` so signal-generation
experiments can vary every StrategyConfig field without touching live MT5
execution commands.

Example:

    python tools/backtest_configurable.py \
      --signals generated_scalper_pullback_v1.txt \
      --charts data/XAUUSD_M1_*.csv \
      --output-dir reports/scalper_pullback_v1 \
      --entry-ladder signal_range_3 \
      --activation-delay 2 \
      --pending-expiry 5 \
      --max-hold 90 \
      --sl-multiplier 1.5 \
      --final-target TP3 \
      --max-drawdown-limit-pct 40
"""
from __future__ import annotations

import argparse
import glob
import json
import sys
from pathlib import Path

# Allow running as ``python tools/backtest_configurable.py`` from repo root.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from xauusd_trading import (  # noqa: E402
    CsvChartSource,
    DEFAULT_CONFIG,
    StrategyConfig,
    parse_signals_file,
    run_backtest,
    write_backtest_outputs,
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
    if not out:
        raise SystemExit("No chart files provided")
    return out


def _config_from_args(args: argparse.Namespace) -> StrategyConfig:
    return StrategyConfig(
        initial_capital=args.initial_capital,
        sizing_mode=args.sizing_mode,
        lot_per_entry=args.lot,
        risk_per_signal=args.risk,
        minimum_lot=args.minimum_lot,
        lot_step=args.lot_step,
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
    )


def _summary_without_rows(result: dict) -> dict:
    return {k: v for k, v in result.items() if k not in {"rows", "entry_rows"}}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="backtest_configurable",
        description="Run a local backtest with full StrategyConfig control.",
    )
    p.add_argument("--signals", required=True, help="Signal text file.")
    p.add_argument("--charts", required=True, nargs="+", help="One or more MT5 M1 chart CSV files/globs.")
    p.add_argument("--output-dir", default=None, help="Optional directory for Excel output.")
    p.add_argument("--exclude-structural-anomalies", action="store_true")

    p.add_argument("--initial-capital", type=float, default=DEFAULT_CONFIG.initial_capital)
    p.add_argument("--sizing-mode", default=DEFAULT_CONFIG.sizing_mode, choices=["fixed", "risk"])
    p.add_argument("--lot", type=float, default=DEFAULT_CONFIG.lot_per_entry)
    p.add_argument("--risk", type=float, default=DEFAULT_CONFIG.risk_per_signal)
    p.add_argument("--minimum-lot", type=float, default=DEFAULT_CONFIG.minimum_lot)
    p.add_argument("--lot-step", type=float, default=DEFAULT_CONFIG.lot_step)

    p.add_argument("--entries", type=int, default=DEFAULT_CONFIG.entry_count)
    p.add_argument(
        "--entry-ladder",
        default=DEFAULT_CONFIG.entry_ladder,
        choices=["signal_range_3", "range_uniform", "range_to_sl"],
        help="Entry spacing rule. signal_range_3 is the provider-native H/H-1/L or L/L+1/H ladder.",
    )
    p.add_argument("--entry-sl-gap", type=float, default=DEFAULT_CONFIG.entry_sl_gap)

    p.add_argument("--activation-delay", type=int, default=DEFAULT_CONFIG.activation_delay_minutes)
    p.add_argument("--pending-expiry", type=int, default=DEFAULT_CONFIG.pending_expiry_minutes)
    p.add_argument("--max-hold", type=int, default=DEFAULT_CONFIG.max_hold_minutes)
    p.add_argument("--sl-multiplier", type=float, default=DEFAULT_CONFIG.sl_multiplier)
    p.add_argument("--final-target", default=DEFAULT_CONFIG.final_target, choices=["TP1", "TP2", "TP3"])
    p.add_argument("--no-lock-after-tp1", action="store_true")
    p.add_argument("--no-lock-after-tp2", action="store_true")

    p.add_argument(
        "--max-drawdown-limit-pct",
        type=float,
        default=40.0,
        help="Drawdown guardrail for generated strategies. Default: 40%%.",
    )
    p.add_argument(
        "--fail-on-drawdown-limit",
        action="store_true",
        help="Return exit code 1 when abs(max_drawdown_pct) exceeds the configured limit.",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = _config_from_args(args)

    signals = parse_signals_file(Path(args.signals))
    chart = CsvChartSource(_expand_chart_paths(args.charts))

    result = run_backtest(
        signals,
        chart,
        config,
        exclude_structural_anomalies=args.exclude_structural_anomalies,
    )

    summary = _summary_without_rows(result)
    max_dd_pct = float(result.get("max_drawdown_pct", 0.0) or 0.0)
    dd_abs = abs(min(0.0, max_dd_pct))
    summary["max_drawdown_limit_pct"] = args.max_drawdown_limit_pct
    summary["passes_drawdown_limit"] = dd_abs <= args.max_drawdown_limit_pct

    print(json.dumps(summary, indent=2, default=str))

    if args.output_dir:
        path = write_backtest_outputs(result, Path(args.output_dir))
        print(f"\nWrote Excel output to {path.resolve()}", file=sys.stderr)

    if args.fail_on_drawdown_limit and not summary["passes_drawdown_limit"]:
        print(
            f"Max drawdown {max_dd_pct:.2f}% exceeds limit "
            f"-{args.max_drawdown_limit_pct:.2f}%.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
