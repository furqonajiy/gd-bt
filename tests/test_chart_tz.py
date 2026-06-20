"""Pin the EET/EEST chart-timezone schedule.

The MT5 chart clock is Eastern European Time: EET (+2) winter, EEST (+3) summer,
switching on the EU rule (last Sunday of March / October at 01:00 UTC). These
dates were confirmed empirically from the ELEV8 archive (the weekly close shifts
to 22:59 only during the US-vs-EU DST mismatch windows, every year).
"""
from __future__ import annotations

from datetime import datetime

from trading.xauusd.core import chart_tz


def test_last_sunday_matches_eu_dst_dates():
    # Last Sunday of March / October for the archive's span.
    assert chart_tz._last_sunday(2024, 3) == 31 and chart_tz._last_sunday(2024, 10) == 27
    assert chart_tz._last_sunday(2025, 3) == 30 and chart_tz._last_sunday(2025, 10) == 26
    assert chart_tz._last_sunday(2026, 3) == 29 and chart_tz._last_sunday(2026, 10) == 25


def test_offsets_winter_summer_and_boundaries():
    # Deep winter / summer.
    assert chart_tz.offset_at_chart_local(datetime(2024, 1, 15, 12)) == 2
    assert chart_tz.offset_at_chart_local(datetime(2024, 7, 15, 12)) == 3
    # Just before EU spring (Mar 31, 2024) is still EET; well after is EEST.
    assert chart_tz.offset_at_chart_local(datetime(2024, 3, 28, 12)) == 2
    assert chart_tz.offset_at_chart_local(datetime(2024, 4, 2, 12)) == 3
    # Just after EU fall-back (Oct 27, 2024) is EET again.
    assert chart_tz.offset_at_chart_local(datetime(2024, 10, 26, 12)) == 3
    assert chart_tz.offset_at_chart_local(datetime(2024, 10, 28, 12)) == 2


def test_victor_gmt7_conversion_is_dst_aware():
    # Summer: GMT+7 -> chart is -4h; winter: -5h (the 1h that the fix corrects).
    assert chart_tz.to_chart_tz(datetime(2024, 7, 2, 14, 15), 7) == datetime(2024, 7, 2, 10, 15)
    assert chart_tz.to_chart_tz(datetime(2024, 1, 2, 14, 15), 7) == datetime(2024, 1, 2, 9, 15)


def test_chart_display_roundtrip_both_seasons():
    for local in (datetime(2026, 1, 5, 10, 15), datetime(2026, 7, 6, 10, 15)):
        for off in (3, 7, 0):
            disp = chart_tz.from_chart_tz(local, off)
            assert chart_tz.to_chart_tz(disp, off) == local
