#!/usr/bin/env python3
"""Run live Auto with optional trailing-open / trailing-close distances.

Thin wrapper around ``python -m xauusd_trading.cli auto``. The main CLI now
accepts ``--trailing-open-distance`` / ``--trailing-close-distance`` (and
``--trend-runner``) directly, so this wrapper simply forwards those flags; it no
longer sets XAUUSD_* environment variables. You can also just call ``cli auto``
with the flags yourself.

Example PowerShell:

python tools/auto_trailing.py `
  --trailing-open-distance 2 `
  --trailing-close-distance 3 `
  --signals generated/live_provider_high_growth.txt `
  --positions-json positions_high_growth.json `
  --watch-interval 5 `
  --mt5-symbol XAUUSD `
  --mt5-server-offset 3 `
  --mt5-history-bars 5000 `
  --initial-capital 50000 `
  --risk 0.0281 `
  --entries 2 `
  --entry-ladder range_to_sl `
  --entry-sl-gap 0
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="auto_trailing",
        description="Run xauusd_trading.cli auto with trailing distances enabled.",
        add_help=True,
    )
    parser.add_argument(
        "--trailing-open-distance",
        type=float,
        default=0.0,
        help="Virtual entry trail distance. Example: BUY seed 4750, distance 2 opens only after rebound from the low by 2.",
    )
    parser.add_argument(
        "--trailing-close-distance",
        type=float,
        default=0.0,
        help="Protective trailing stop distance for open entries. 0 disables trailing close.",
    )
    known, rest = parser.parse_known_args(argv)

    from xauusd_trading.cli import main as cli_main

    return cli_main([
        "auto",
        "--trailing-open-distance", str(float(known.trailing_open_distance)),
        "--trailing-close-distance", str(float(known.trailing_close_distance)),
        *rest,
    ])


if __name__ == "__main__":
    raise SystemExit(main())