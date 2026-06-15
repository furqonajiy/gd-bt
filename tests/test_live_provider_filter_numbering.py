"""The live provider filter must keep each signal's original signals.txt number
so signal_key (date#day_id) matches the Telegram channel for cross-reference.
A day's 1..N with only some kept must show the kept numbers, not a fresh 1..N.
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.live_provider_signal_filter import ProviderSignal, write_filtered
from xauusd_trading import parse_signals_file


def _sig(source_id: int, hour: int) -> ProviderSignal:
    dt = datetime(2026, 6, 4, hour, 0)
    return ProviderSignal(
        source_date="2026-06-04", source_tz=3, source_id=source_id,
        source_time=dt, chart_time=dt, side="BUY",
        r1="4518", r2="4516", sl="4511", tp1="4526", tp2="4536", tp3="4551",
        filter_reason="kept",
    )


def test_write_filtered_preserves_provider_numbering(tmp_path):
    # Of a day's 1,2,3,4 only #2 and #3 survive the filter.
    out = tmp_path / "filtered.txt"
    write_filtered([_sig(2, 8), _sig(3, 9)], out)

    body = "\n" + out.read_text(encoding="utf-8")
    assert "\n2. BUY XAUUSD" in body
    assert "\n3. BUY XAUUSD" in body
    assert "\n1. BUY XAUUSD" not in body  # not re-numbered from 1

    # signal_key is derived from the printed prefix, so it tracks signals.txt.
    keys = [s.signal_key for s in parse_signals_file(out)]
    assert keys == ["2026-06-04#02", "2026-06-04#03"]


def test_write_filtered_single_kept_keeps_its_number(tmp_path):
    # Only #4 of the day is kept -> file shows 4., signal_key #04 (not #01).
    out = tmp_path / "filtered.txt"
    write_filtered([_sig(4, 17)], out)

    body = "\n" + out.read_text(encoding="utf-8")
    assert "\n4. BUY XAUUSD" in body
    assert "\n1. BUY XAUUSD" not in body
    keys = [s.signal_key for s in parse_signals_file(out)]
    assert keys == ["2026-06-04#04"]

def test_watch_log_line_leads_with_signal_key():
    # The [NEW KEPT] console line must say WHICH signal was added (#10 vs #11):
    # it leads with the engine-style key (chart date + the provider day-id that
    # is emitted as `N.` in the filtered feed), matching positions.json / MT5
    # order comments.
    from tools.live_provider_signal_filter import _describe_signal

    line = _describe_signal(_sig(11, 17))
    assert line.startswith("2026-06-04#11 GMT+3 BUY XAUUSD ")
    assert " at 5:00 PM" in line
