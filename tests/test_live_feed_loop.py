"""live_feed_loop: the new-closed-bar gate and the fetch --months flag.

The loop's efficiency contract: NO fetch and NO generation unless the last
CLOSED M1 bar advanced. No MT5 needed -- the gate is a pure function and the
flag is plain argparse.
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for p in (str(ROOT), str(ROOT / "tools")):
    if p not in sys.path:
        sys.path.insert(0, p)

from live_feed_loop import GENERATOR_MODULES, _should_regenerate, build_parser  # noqa: E402


def test_first_pass_regenerates():
    assert _should_regenerate(datetime(2026, 6, 12, 10, 0), None) is True


def test_same_bar_skips():
    t = datetime(2026, 6, 12, 10, 0)
    assert _should_regenerate(t, t) is False


def test_new_bar_regenerates():
    assert _should_regenerate(datetime(2026, 6, 12, 10, 1),
                              datetime(2026, 6, 12, 10, 0)) is True


def test_no_terminal_data_never_regenerates():
    # Disconnected terminal (None) must not wipe/replace the feed.
    assert _should_regenerate(None, None) is False
    assert _should_regenerate(None, datetime(2026, 6, 12, 10, 0)) is False


def test_parser_forwards_generator_args_and_validates():
    p = build_parser()
    args = p.parse_args(["--family", "scalper", "--interval", "60",
                         "--", "--charts", "x.csv", "--output", "o.txt"])
    assert args.family == "scalper"
    assert [a for a in args.gen_args if a != "--"] == [
        "--charts", "x.csv", "--output", "o.txt"]


def test_all_generator_modules_importable():
    import importlib
    for mod in GENERATOR_MODULES.values():
        importlib.import_module(mod)


def test_fetch_months_flag():
    from trading.engine.cli import build_parser as cli_parser
    args = cli_parser().parse_args(["fetch", "--months", "1"])
    assert args.months == 1
    args = cli_parser().parse_args(["fetch"])
    assert args.months == 2  # ARCHIVE_MONTHS default unchanged
