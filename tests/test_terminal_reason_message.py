"""The live 'signal no longer valid' log line names WHEN it fired, its entry
window, and WHEN the SL/target was reached (GMT+7), instead of a bare
'already resolved'. Pins _format_terminal_reason (the testable formatter behind
_auto_pass's _terminal_reason)."""
from __future__ import annotations

from datetime import datetime

from trading.engine.cli import _format_terminal_reason


def test_resolved_target_message_has_fired_window_and_hit_time():
    # BUY fired 07:25 chart (=11:25 GMT+7); 180-min window -> 10:25 chart (=14:25);
    # final target 4005 reached 08:03 chart (=12:03 GMT+7).
    msg = _format_terminal_reason(
        fired_chart=datetime(2026, 7, 1, 7, 25),
        expiry_chart=datetime(2026, 7, 1, 10, 25),
        hit_chart=datetime(2026, 7, 1, 8, 3),
        label="final target", level=4005.0, tag="resolved")
    assert "fired 2026-07-01 11:25 GMT+7" in msg
    assert "entry window until 2026-07-01 14:25 GMT+7" in msg
    assert "final target 4005 already reached at 2026-07-01 12:03 GMT+7" in msg
    assert "no longer valid (resolved)" in msg
    assert msg.startswith("not opened/re-armed -- ")


def test_terminal_sl_message_names_sl_and_time():
    msg = _format_terminal_reason(
        fired_chart=datetime(2026, 7, 1, 7, 25),
        expiry_chart=datetime(2026, 7, 1, 10, 25),
        hit_chart=datetime(2026, 7, 1, 7, 40),
        label="original SL", level=3968.0, tag="terminal_sl")
    assert "original SL 3968 already reached at 2026-07-01 11:40 GMT+7" in msg
    assert "no longer valid (terminal_sl)" in msg


def test_message_without_expiry_omits_window():
    msg = _format_terminal_reason(
        fired_chart=datetime(2026, 7, 1, 7, 25), expiry_chart=None,
        hit_chart=datetime(2026, 7, 1, 8, 3),
        label="final target", level=4005.0, tag="resolved")
    assert "entry window" not in msg
    assert "fired 2026-07-01 11:25 GMT+7;" in msg  # goes straight to the reason
