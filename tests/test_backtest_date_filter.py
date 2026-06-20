"""--start-date / --end-date select which signals a backtest replays.

Dates are chart-time (GMT+3); start is inclusive at 00:00, end is inclusive of
the whole day. Equity then starts at --initial-capital on the first surviving
signal. Deterministic (no market data), so it runs everywhere.
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