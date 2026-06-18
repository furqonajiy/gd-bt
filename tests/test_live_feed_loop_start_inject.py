"""live_feed_loop._effective_gen_argv start-flag handling.

--gen-start-days must REWRITE an existing --start/--start-date and INJECT the
family's start flag when the pass-through omits one -- otherwise it is a silent
no-op and the live feed emits the whole loaded chart window (cold-start bars
included), the root cause of the 2026-06-18 live-vs-backtest signal drift.
"""
from __future__ import annotations

import importlib
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "tools") not in sys.path:
    sys.path.insert(0, str(ROOT / "tools"))

lfl = importlib.import_module("tools.live_feed_loop")

TODAY = datetime(2026, 6, 18)
EXPECTED_START = "2026-06-15"  # today - 3 days


def test_rewrites_existing_start_flag():
    argv = ["--charts", "data/x.csv", "--start", "2025-01-01", "--output", "o.txt"]
    out = lfl._effective_gen_argv(argv, start_days=3, recent_months=None,
                                  today=TODAY, start_flag="--start")
    assert out[out.index("--start") + 1] == EXPECTED_START
    assert "--start-date" not in out


def test_rewrites_existing_start_date_flag():
    argv = ["--charts", "data/x.csv", "--start-date", "2025-01-01"]
    out = lfl._effective_gen_argv(argv, start_days=3, recent_months=None,
                                  today=TODAY, start_flag="--start-date")
    assert out[out.index("--start-date") + 1] == EXPECTED_START


def test_injects_start_flag_when_absent():
    # The user's exact failing case: scalper pass-through with no --start.
    argv = ["--charts", "data/x.csv", "--output", "o.txt",
            "--session-start", "0", "--session-end", "0", "--rsi-buy-max", "70"]
    out = lfl._effective_gen_argv(argv, start_days=3, recent_months=None,
                                  today=TODAY, start_flag="--start")
    assert "--start" in out
    assert out[out.index("--start") + 1] == EXPECTED_START


def test_no_injection_for_family_without_start_flag():
    argv = ["--charts", "data/x.csv", "--output", "o.txt"]
    out = lfl._effective_gen_argv(argv, start_days=3, recent_months=None,
                                  today=TODAY, start_flag=None)
    assert "--start" not in out and "--start-date" not in out


def test_no_start_change_when_start_days_none():
    argv = ["--charts", "data/x.csv", "--output", "o.txt"]
    out = lfl._effective_gen_argv(argv, start_days=None, recent_months=None,
                                  today=TODAY, start_flag="--start")
    assert "--start" not in out


def test_family_start_flag_map_matches_modules():
    # Every generator family has an explicit start-flag entry (None allowed).
    assert set(lfl.GENERATOR_START_FLAG) == set(lfl.GENERATOR_MODULES)
    assert lfl.GENERATOR_START_FLAG["scalper"] == "--start"
    assert lfl.GENERATOR_START_FLAG["risk02"] == "--start-date"
    assert lfl.GENERATOR_START_FLAG["adaptive"] == "--start-date"
    assert lfl.GENERATOR_START_FLAG["breakout"] == "--start-date"
    assert lfl.GENERATOR_START_FLAG["meanrev"] == "--start-date"


def test_atr_families_inject_start_date_when_absent():
    argv = ["--m1-charts", "data/x.csv", "--output", "o.txt"]
    out = lfl._effective_gen_argv(argv, start_days=3, recent_months=None,
                                  today=TODAY, start_flag="--start-date")
    assert "--start-date" in out
    assert out[out.index("--start-date") + 1] == EXPECTED_START
