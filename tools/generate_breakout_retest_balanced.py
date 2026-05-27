#!/usr/bin/env python3
"""Generate the balanced breakout-retest signal set.

This is the recommended preset from the full uploaded Jan-2024 -> May-2026
validation:

- Initial capital for validation: 10,000
- Sizing for validation: risk mode, 1% per signal
- Net profit observed: about +6,826.06
- Max drawdown observed: about -11.53%
- Positive both before 2026 and from 2026 onward

This wrapper delegates to ``generate_breakout_retest_signals.py`` but pins the
parameters that produced the balanced result, so local runs are less error-prone.

Example:

    python tools/generate_breakout_retest_balanced.py \
      --charts data/XAUUSD_M1_*.csv \
      --output generated/breakout_retest_balanced_full2024.txt \
      --diagnostics generated/breakout_retest_balanced_full2024.csv

Then backtest with:

    python tools/backtest_configurable.py \
      --signals generated/breakout_retest_balanced_full2024.txt \
      --charts data/XAUUSD_M1_*.csv \
      --output-dir reports/breakout_retest_balanced_full2024 \
      --initial-capital 10000 \
      --sizing-mode risk \
      --risk 0.01 \
      --max-drawdown-limit-pct 40
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
        description="Generate balanced breakout-retest XAUUSD signals.",
    )
    p.add_argument("--charts", required=True, nargs="+", help="MT5 M1 chart CSV files or globs.")
    p.add_argument("--output", default="generated/breakout_retest_balanced_full2024.txt")
    p.add_argument("--diagnostics", default="generated/breakout_retest_balanced_full2024.csv")
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
        "--level-cooldown-minutes", "45",
        "--max-spread-points", "40",
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
