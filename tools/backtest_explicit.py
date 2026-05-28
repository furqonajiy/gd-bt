#!/usr/bin/env python3
"""Backtest runner that requires every strategy parameter explicitly.

This mirrors ``tools/auto_explicit.py``. Use it for research/backtest commands
when you want to guarantee that the run does not silently depend on
StrategyConfig defaults.
"""
from __future__ import annotations

import argparse
import glob
import json
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from xauusd_trading import CsvChartSource, StrategyConfig, parse_signals_file, run_backtest, write_backtest_outputs  # noqa: E402


def _positive_int(raw: str) -> int:
    value = int(raw)
    if value < 0:
        raise argparse.ArgumentTypeError("must be >= 0")
    return value


def _positive_float(raw: str) -> float:
    value = float(raw)
    if value < 0:
        raise argparse.ArgumentTypeError("must be >= 0")
    return value


def _bool_text(raw: str) -> bool:
    text = str(raw).strip().lower()
    if text not in {"true", "false"}:
        raise argparse.ArgumentTypeError("must be true or false")
    return text == "true"


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


def _fmt_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h:d}h {m:02d}m {s:02d}s"
    if m:
        return f"{m:d}m {s:02d}s"
    return f"{s:d}s"


class Heartbeat:
    def __init__(self, label: str, interval_seconds: float, *, enabled: bool = True):
        self.label = label
        self.interval_seconds = max(1.0, float(interval_seconds))
        self.enabled = enabled
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._start = 0.0

    def __enter__(self):
        if not self.enabled:
            return self
        self._start = time.time()
        print(f"[{self.label}] started", file=sys.stderr, flush=True)
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, exc_type, exc, tb):
        if not self.enabled:
            return False
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
        print(f"[{self.label}] finished after {_fmt_duration(time.time() - self._start)}", file=sys.stderr, flush=True)
        return False

    def _run(self) -> None:
        while not self._stop.wait(self.interval_seconds):
            print(f"[{self.label}] still running... elapsed {_fmt_duration(time.time() - self._start)}", file=sys.stderr, flush=True)


def add_required_strategy_args(p: argparse.ArgumentParser) -> None:
    strategy = p.add_argument_group("required strategy contract")
    strategy.add_argument("--initial-capital", type=_positive_float, required=True)
    strategy.add_argument("--sizing-mode", choices=["fixed", "risk"], required=True)
    strategy.add_argument("--lot", type=_positive_float, required=True)
    strategy.add_argument("--risk", type=_positive_float, required=True)
    strategy.add_argument("--minimum-lot", type=_positive_float, required=True)
    strategy.add_argument("--lot-step", type=_positive_float, required=True)
    strategy.add_argument("--bonus-per-closed-lot", type=_positive_float, required=True)
    strategy.add_argument("--entries", type=int, required=True)
    strategy.add_argument("--entry-ladder", choices=["signal_range_3", "range_uniform", "range_to_sl"], required=True)
    strategy.add_argument("--entry-sl-gap", type=_positive_float, required=True)
    strategy.add_argument("--activation-delay", type=_positive_int, required=True)
    strategy.add_argument("--pending-expiry", type=_positive_int, required=True)
    strategy.add_argument("--max-hold", type=_positive_int, required=True)
    strategy.add_argument("--sl-multiplier", type=_positive_float, required=True)
    strategy.add_argument("--final-target", choices=["TP1", "TP2", "TP3"], required=True)
    strategy.add_argument("--lock-after-tp1", type=_bool_text, required=True)
    strategy.add_argument("--lock-after-tp2", type=_bool_text, required=True)
    strategy.add_argument("--tp1-lock-delay-minutes", type=_positive_int, required=True)
    strategy.add_argument("--tp2-lock-delay-minutes", type=_positive_int, required=True)
    strategy.add_argument("--profit-lock-mode", choices=["tp_levels", "bep_plus_half_tp1"], required=True)
    strategy.add_argument("--bep-trigger-distance", type=_positive_float, required=True)
    strategy.add_argument("--tp1-lock-fraction", type=float, required=True)
    strategy.add_argument("--tp2-lock-target", choices=["TP1", "TP2"], required=True)
    strategy.add_argument("--runner-after-tp3", type=_bool_text, required=True)
    strategy.add_argument("--tp3-lock-target", choices=["TP2"], required=True)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Run backtest with no hidden strategy defaults.")
    p.add_argument("--signals", required=True)
    p.add_argument("--charts", required=True, nargs="+")
    p.add_argument("--output-dir", required=True)
    p.add_argument("--exclude-structural-anomalies", action="store_true")
    p.add_argument("--max-drawdown-limit-pct", type=float, required=True)
    p.add_argument("--fail-on-drawdown-limit", action="store_true")
    p.add_argument("--progress-interval-seconds", type=float, required=True)
    add_required_strategy_args(p)
    return p


def config_from_args(args: argparse.Namespace) -> StrategyConfig:
    if args.entries < 1:
        raise SystemExit("--entries must be >= 1")
    if args.tp1_lock_fraction < 0 or args.tp1_lock_fraction > 1:
        raise SystemExit("--tp1-lock-fraction must be between 0 and 1")
    if args.sizing_mode == "risk" and args.risk <= 0:
        raise SystemExit("--risk must be > 0 when --sizing-mode risk")
    if args.sizing_mode == "fixed" and args.lot <= 0:
        raise SystemExit("--lot must be > 0 when --sizing-mode fixed")

    return StrategyConfig(
        initial_capital=args.initial_capital,
        sizing_mode=args.sizing_mode,
        lot_per_entry=args.lot,
        risk_per_signal=args.risk,
        minimum_lot=args.minimum_lot,
        lot_step=args.lot_step,
        bonus_per_closed_lot=args.bonus_per_closed_lot,
        entry_count=args.entries,
        entry_ladder=args.entry_ladder,
        entry_sl_gap=args.entry_sl_gap,
        activation_delay_minutes=args.activation_delay,
        pending_expiry_minutes=args.pending_expiry,
        max_hold_minutes=args.max_hold,
        sl_multiplier=args.sl_multiplier,
        final_target=args.final_target,
        lock_after_tp1=args.lock_after_tp1,
        lock_after_tp2=args.lock_after_tp2,
        tp1_lock_delay_minutes=args.tp1_lock_delay_minutes,
        tp2_lock_delay_minutes=args.tp2_lock_delay_minutes,
        profit_lock_mode=args.profit_lock_mode,
        bep_trigger_distance=args.bep_trigger_distance,
        tp1_lock_fraction=args.tp1_lock_fraction,
        tp2_lock_target=args.tp2_lock_target,
        runner_after_tp3=args.runner_after_tp3,
        tp3_lock_target=args.tp3_lock_target,
    )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = config_from_args(args)
    progress_enabled = args.progress_interval_seconds > 0

    signals = parse_signals_file(Path(args.signals))
    with Heartbeat("chart load", args.progress_interval_seconds, enabled=progress_enabled):
        chart = CsvChartSource(_expand_chart_paths(args.charts))
    with Heartbeat("backtest", args.progress_interval_seconds, enabled=progress_enabled):
        result = run_backtest(
            signals,
            chart,
            config,
            exclude_structural_anomalies=args.exclude_structural_anomalies,
        )

    summary = {k: v for k, v in result.items() if k not in {"rows", "entry_rows"}}
    dd_abs = abs(min(0.0, float(result.get("max_drawdown_pct", 0.0) or 0.0)))
    summary["max_drawdown_limit_pct"] = args.max_drawdown_limit_pct
    summary["passes_drawdown_limit"] = dd_abs <= args.max_drawdown_limit_pct
    print(json.dumps(summary, indent=2, default=str))

    with Heartbeat("Excel write", args.progress_interval_seconds, enabled=progress_enabled):
        path = write_backtest_outputs(result, Path(args.output_dir))
    print(f"\nWrote Excel output to {path.resolve()}", file=sys.stderr)

    if args.fail_on_drawdown_limit and not summary["passes_drawdown_limit"]:
        print(
            f"Max drawdown {result.get('max_drawdown_pct', 0.0):.2f}% exceeds limit "
            f"-{args.max_drawdown_limit_pct:.2f}%.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
