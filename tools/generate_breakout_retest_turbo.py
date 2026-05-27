#!/usr/bin/env python3
"""Generate the high-frequency turbo breakout-retest signal set.

This preset is intentionally aggressive.  It is meant for research around very
high daily return targets, not as the safe default and not as a live-trading
recommendation by itself.

Goal:
- create many more breakout-retest candidates than the balanced preset
- allow testing 3% to 5% risk per signal in backtests
- evaluate whether any configuration can approach 1% to 10% daily growth while
  keeping drawdown under the user's guardrail

Compared with the balanced preset:
- cooldown is reduced from 3 minutes to 1 minute
- same-level cooldown is reduced from 45 minutes to 5 minutes
- max spread is increased from 40 to 80 points
- body threshold is reduced from 0.10 ATR to 0.03 ATR
- max body threshold is increased to allow stronger breakout candles

Because this preset takes many more trades, always test with the full backtest
engine before using any generated signal file.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"
for path in (ROOT, TOOLS):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from generate_breakout_retest_signals import main as generate_main  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Generate turbo/high-frequency breakout-retest XAUUSD signals.",
    )
    p.add_argument("--charts", required=True, nargs="+", help="MT5 M1 chart CSV files or globs.")
    p.add_argument("--output", default="generated/breakout_retest_turbo_full2024.txt")
    p.add_argument("--diagnostics", default="generated/breakout_retest_turbo_full2024.csv")
    p.add_argument("--start", default=None)
    p.add_argument("--end", default=None)
    p.add_argument("--progress-every-rows", type=int, default=100_000)
    p.add_argument("--progress-interval-seconds", type=float, default=15.0)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    delegated = [
        "--charts", *args.charts,
        "--output", args.output,
        "--diagnostics", args.diagnostics,
        "--cooldown-minutes", "1",
        "--level-cooldown-minutes", "5",
        "--max-spread-points", "80",
        "--breakout-buffer", "0.5",
        "--entry-buffer", "0.0",
        "--stop-distance", "3.0",
        "--rr3", "2.0",
        "--session-start", "7",
        "--session-end", "23",
        "--require-body",
        "--min-body-atr", "0.03",
        "--max-body-atr", "3.0",
        "--progress-every-rows", str(args.progress_every_rows),
        "--progress-interval-seconds", str(args.progress_interval_seconds),
    ]
    if args.start:
        delegated.extend(["--start", args.start])
    if args.end:
        delegated.extend(["--end", args.end])
    return generate_main(delegated)


if __name__ == "__main__":
    raise SystemExit(main())
