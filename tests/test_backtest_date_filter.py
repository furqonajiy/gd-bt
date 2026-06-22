"""--start-date / --end-date select which signals a backtest replays.

By default dates are chart-time (EET/EEST, ~GMT+3); start is inclusive at 00:00,
end is inclusive of the whole day. ``--date-tz N`` reads the boundaries in GMT+N
instead (7 to line up with the GMT+7 signal-code dates like SQZ6-0623). Equity
then starts at --initial-capital on the first surviving signal. Deterministic
(no market data), so it runs everywhere.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

from trading.engine import parse_one_signal

ROOT = Path(__file__).resolve().parents[1]


def _load(module_name: str):
    spec = importlib.util.spec_from_file_location(module_name, ROOT / "tools" / f"{module_name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


backtest_explicit = _load("backtest_explicit")
_filter = backtest_explicit.filter_signals_by_date


def _sig(date: str):
    # GMT+3 source against a GMT+3 chart -> signal_time_chart date == source date
    return parse_one_signal(
        "1. BUY XAUUSD 100 - 99 SL 97 TP1 105 TP2 110 TP3 120 11:00 AM",
        source_date=date, source_offset=3,
    )


SIGNALS = [_sig("2026-05-10"), _sig("2026-05-12"), _sig("2026-05-15")]


def _dates(signals):
    return [s.signal_time_chart.date().isoformat() for s in signals]


def test_no_dates_keeps_all():
    assert _dates(_filter(SIGNALS, None, None)) == ["2026-05-10", "2026-05-12", "2026-05-15"]


def test_start_date_is_inclusive():
    assert _dates(_filter(SIGNALS, "2026-05-12", None)) == ["2026-05-12", "2026-05-15"]


def test_end_date_includes_whole_day():
    assert _dates(_filter(SIGNALS, None, "2026-05-12")) == ["2026-05-10", "2026-05-12"]


def test_start_and_end_bound_a_single_day():
    assert _dates(_filter(SIGNALS, "2026-05-12", "2026-05-12")) == ["2026-05-12"]


def test_window_excludes_outside_signals():
    assert _dates(_filter(SIGNALS, "2026-05-11", "2026-05-14")) == ["2026-05-12"]


# --- --date-tz: read the boundaries in the GMT+7 signal-code zone ------------

def _sig7(date: str, time_text: str):
    # GMT+7 source (Victor/SQZ6) against the EET/EEST chart -> the chart day is
    # up to 4h earlier, so a just-after-midnight GMT+7 signal lands the day before.
    return parse_one_signal(
        f"1. BUY XAUUSD 100 - 99 SL 97 TP1 105 TP2 110 TP3 120 {time_text}",
        source_date=date, source_offset=7,
    )


def test_date_tz_matches_gmt7_signal_code_dates():
    # 00:30 GMT+7 on 06-23 has chart time ~20:30 on 06-22. Legacy (chart-local)
    # --start-date 2026-06-23 wrongly drops it; --date-tz 7 reads the boundary in
    # the signal zone and keeps it (so the window matches SQZ6-0623).
    s = _sig7("2026-06-23", "12:30 AM")
    assert s.signal_time_chart.date().isoformat() == "2026-06-22"  # chart day is earlier
    assert _filter([s], "2026-06-23", None) == []                  # legacy: dropped
    assert _filter([s], "2026-06-23", None, 7) == [s]              # tz-aware: kept


def test_date_tz_end_includes_whole_gmt7_day_only():
    # Two signals on the SAME chart day 06-23 but different GMT+7 days: an
    # end-date of 06-23 in GMT+7 keeps the late 06-23 signal and drops the early
    # 06-24 one -- something the chart-day filter cannot distinguish.
    late = _sig7("2026-06-23", "11:30 PM")    # chart ~ 06-23 19:30
    nextday = _sig7("2026-06-24", "12:30 AM")  # chart ~ 06-23 20:30
    assert late.signal_time_chart.date() == nextday.signal_time_chart.date()
    assert _filter([late, nextday], None, "2026-06-23", 7) == [late]