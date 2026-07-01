"""The live 'entry window closed' (SKIP_EXPIRED) log line names WHEN the signal
armed, HOW LONG its entry window stayed open, WHEN it closed, and HOW LONG ago
(GMT+7), instead of a bare 'pending window already closed at ...'. Pins
_format_expired_reason / _fmt_duration (the testable formatter behind
_auto_pass's SKIP_EXPIRED branch)."""
from __future__ import annotations

from datetime import datetime, timedelta

from trading.engine.cli import _format_expired_reason, _fmt_duration


def test_expired_message_names_arm_window_and_expiry():
    # V017-style: armed 09:34 chart (=13:34 GMT+7), 180-min window -> 12:34 chart
    # (=16:34 GMT+7); now 12:40 chart (=16:40 GMT+7), so expired 6m ago.
    msg = _format_expired_reason(
        fired_chart=datetime(2026, 7, 1, 9, 34),
        expiry_chart=datetime(2026, 7, 1, 12, 34),
        now_chart=datetime(2026, 7, 1, 12, 40))
    assert "armed 2026-07-01 13:34 GMT+7" in msg
    assert "only active for 3h" in msg
    assert "entry window closed 2026-07-01 16:34 GMT+7" in msg
    assert "expired 6m ago" in msg
    assert "now 2026-07-01 16:40 GMT+7" in msg
    assert msg.endswith("Skipped.")


def test_expired_message_without_expiry_still_shows_arm_time():
    msg = _format_expired_reason(
        fired_chart=datetime(2026, 7, 1, 9, 34),
        expiry_chart=None,
        now_chart=datetime(2026, 7, 1, 12, 40))
    assert "armed 2026-07-01 13:34 GMT+7" in msg
    assert "entry window already closed" in msg
    assert "now 2026-07-01 16:40 GMT+7" in msg
    assert "only active for" not in msg  # unknown window length
    assert msg.endswith("Skipped.")


def test_fmt_duration_renders_compact_units():
    assert _fmt_duration(timedelta(hours=3, minutes=5)) == "3h 5m"
    assert _fmt_duration(timedelta(hours=3)) == "3h"
    assert _fmt_duration(timedelta(minutes=45)) == "45m"
    assert _fmt_duration(timedelta(seconds=30)) == "30s"
    assert _fmt_duration(timedelta(seconds=-10)) == "0s"  # clamped non-negative
