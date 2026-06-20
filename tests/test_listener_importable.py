"""Regression: the listener must import without telethon installed.

`listener` historically called ``sys.exit(1)`` at import when telethon
was missing. Because `test_notifications_forward.py` imports it at module scope,
that turned a missing optional dependency into a ``SystemExit`` during pytest
collection -> INTERNALERROR -> the ENTIRE suite refused to run on any env without
telethon (exactly the documented sandbox/clean-clone case). The module now defers
the telethon requirement to the live client, so importing it -- and calling its
pure notification helpers -- must work unconditionally.
"""
import importlib
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "listeners" / "telegram"))


def test_listener_imports_without_telethon():
    tl = importlib.import_module("listener")
    # Pure helpers stay importable/callable regardless of telethon availability.
    assert callable(tl.read_new_notification_events)
    assert callable(tl.read_notification_offset)
    assert callable(tl.write_notification_offset)


def test_require_telethon_raises_clearly_when_absent():
    tl = importlib.import_module("listener")
    if tl.TelegramClient is not None:
        pytest.skip("telethon installed in this env; runtime guard is a no-op by design")
    with pytest.raises(RuntimeError, match="telethon"):
        tl._require_telethon()