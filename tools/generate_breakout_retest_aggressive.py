#!/usr/bin/env python3
"""Generate the aggressive breakout-retest signal set.

This preset is more active than ``generate_breakout_retest_balanced.py``.  It is
intended for research and sizing tests, not as the first live default.

Full uploaded Jan-2024 -> May-2026 validation snapshot with 10,000 initial
capital and 1% risk sizing:

- Net profit observed: about +6,999.73
- Max drawdown observed: about -20.92%
- Positive both before 2026 and from 2026 onward

Compared with the balanced preset, this keeps the same breakout/retest logic but
loosens same-level cooldown and spread:

- level-cooldown-minutes: 15 instead of 45
- max-spread-points: 60 instead of 40
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
        description="Generate aggressive breakout-retest XAUUSD signals.",
    )
    p.add_argument("--charts", required=True, nargs="+", help="MT5 M1 chart CSV files or globs.")
    p.add_argument("--output", default="generated/breakout_retest_aggressive_full2024.txt")
    p.add_argument("--diagnostics", default="generated/breakout_retest_aggressive_full2024.csv")
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
        "--cooldown-minutes", "3",
        "--level-cooldown-minutes", "15",
        "--max-spread-points", "60",
        "--breakout-buffer", "1.0",
        "--entry-buffer", "0.0",
        "--stop-distance", "3.0",
        "--rr3", "2.0",
        "--session-start", "7",
        "--session-end", "23",
        "--require-body",
        "--min-body-atr", "0.1",
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
