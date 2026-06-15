"""Regression guard for the signal-generator timezone shift (DST-aware).

Signals are detected in CHART time, which is Eastern European (EET +2 winter /
EEST +3 summer, EU last-Sunday rule) -- NOT a fixed GMT+3. When a feed is written
for display in another timezone (``--source-tz-offset 7`` to match the Victor /
local GMT+7 view), the generator MUST convert via ``chart_tz.from_chart_tz`` so
the written clock matches the header AND round-trips back to the exact source bar
when the engine parses it, in BOTH seasons (the offset to GMT+7 is +4h in summer
but +5h in winter).

Pins the round-trip + the exact display clock for all three generators that share
the ``write_signal_file`` writer (breakout / meanrev / adaptive-self).
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

# A SELL detected on the 10:15 chart bar, in winter (EET +2) and summer (EEST +3).
WINTER = datetime(2026, 1, 5, 10, 15)
SUMMER = datetime(2026, 7, 6, 10, 15)
_SIG_ARGS = ("SELL", 1793.13, 1794.03, 1795.37, 1790.44, 1788.35, 1785.06)


@pytest.mark.parametrize("modname", GEN_MODULES)
@pytest.mark.parametrize(
    "chart_time,offset,clock",
    [
        # GMT+7 display: winter chart 10:15 -> +5h = 3:15 PM; summer -> +4h = 2:15 PM.
        (WINTER, 7, "3:15 PM"),
        (SUMMER, 7, "2:15 PM"),
        # GMT+3 display: winter chart-local is +2, so a GMT+3 label is +1h = 11:15 AM;
        # summer chart-local is already +3 = 10:15 AM (no shift).
        (WINTER, 3, "11:15 AM"),
        (SUMMER, 3, "10:15 AM"),
    ],
)
def test_writer_display_clock_and_roundtrip(modname, chart_time, offset, clock, tmp_path):
    gen = importlib.import_module(modname)
    sig = gen.SignalRow(chart_time, *_SIG_ARGS)
    out = tmp_path / "feed.txt"

    gen.write_signal_file([sig], out, source_tz_offset=offset)
    text = out.read_text(encoding="utf-8")
    assert f"GMT+{offset}" in text
    assert clock in text, text

    parsed = parse_signals_file(out)
    assert len(parsed) == 1
    # The DST-aware round-trip must land on the exact source bar in both seasons.
    assert parsed[0].signal_time_chart == chart_time
