"""Regression guard for the signal-generator timezone shift.

Signals are detected in CHART time (GMT+3, the MT5 server clock). When a feed is
written for display in another timezone (``--source-tz-offset 7`` to match the
Victor / local GMT+7 view), the generator MUST shift the clock by (offset - 3)
so the written time matches the header AND round-trips back to the exact source
bar when the engine parses it. A prior bug stamped GMT+3 clock values under a
GMT+7 header without shifting, so the backtest relocated every signal 4h.

These tests pin the round-trip for all three generators that share the
``write_signal_file`` writer (breakout / meanrev / adaptive-self); the scalper
generator already had the shift.
"""
from __future__ import annotations

import importlib
from datetime import datetime

import pytest

from xauusd_trading import parse_signals_file

GEN_MODULES = [
    "tools.generate_breakout_signals",
    "tools.generate_meanrev_signals",
    "tools.generate_adaptive_self_signals",
]

# A SELL detected on the 10:15 GMT+3 chart bar.
CHART_TIME = datetime(2026, 1, 5, 10, 15)
_SIG_ARGS = ("SELL", 1793.13, 1794.03, 1795.37, 1790.44, 1788.35, 1785.06)


@pytest.mark.parametrize("modname", GEN_MODULES)
def test_writer_shifts_to_display_tz_and_roundtrips(modname, tmp_path):
    gen = importlib.import_module(modname)
    sig = gen.SignalRow(CHART_TIME, *_SIG_ARGS)
    out = tmp_path / "feed.txt"

    # GMT+7 display: 10:15 GMT+3 must be written as 14:15 (2:15 PM) GMT+7 and
    # parse back to the original 10:15 GMT+3 bar.
    gen.write_signal_file([sig], out, source_tz_offset=7)
    text = out.read_text(encoding="utf-8")
    assert "GMT+7" in text
    assert "2:15 PM" in text
    parsed = parse_signals_file(out)
    assert len(parsed) == 1
    assert parsed[0].signal_time_chart == CHART_TIME


@pytest.mark.parametrize("modname", GEN_MODULES)
def test_writer_chart_tz_is_unshifted(modname, tmp_path):
    gen = importlib.import_module(modname)
    sig = gen.SignalRow(CHART_TIME, *_SIG_ARGS)
    out = tmp_path / "feed.txt"

    # GMT+3 display (== chart tz): no shift, and still round-trips to the bar.
    gen.write_signal_file([sig], out, source_tz_offset=3)
    text = out.read_text(encoding="utf-8")
    assert "GMT+3" in text
    assert "10:15 AM" in text
    assert parse_signals_file(out)[0].signal_time_chart == CHART_TIME
