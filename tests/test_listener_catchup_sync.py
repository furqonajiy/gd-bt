"""Catch-up reconciles VICTOR's edits/deletions made while the listener was down.

Telegram never replays edit/delete events from a downtime window, so the
listener's startup catch-up re-checks every tracked message in the lookback
window (an edit re-process that no-ops when values are unchanged) and infers
deletions from tracked ids the channel no longer returns. The feed and MT5
then follow the channel's latest state across restarts, same as the live
event handlers.
"""
from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "listener"))
import telegram_listener as tl  # noqa: E402


# ---------------------------------------------------------------------------
# plan_catchup_deletions: which tracked ids count as deleted
# ---------------------------------------------------------------------------

def _written(mid: int, key: str) -> tuple[str, dict]:
    return str(mid), {"status": "written", "signal_key": key, "line": f"line-{mid}"}


def test_plan_deletions_only_inside_scanned_window():
    state = {"messages": dict([_written(5, "a"), _written(8, "b"), _written(9, "c")])}
    # Window reached down to id 7; ids 8 and 9 are judgeable, 5 is not.
    deleted = tl.plan_catchup_deletions(state, seen_ids={7, 9}, window_min_id=7, last_id=10)
    assert deleted == [8]


def test_plan_deletions_ignores_ids_above_last_processed():
    # id 12 > last_id is new territory still being processed, never "deleted".
    state = {"messages": dict([_written(12, "a")])}
    assert tl.plan_catchup_deletions(state, seen_ids=set(), window_min_id=7, last_id=10) == []


def test_plan_deletions_empty_window_proves_nothing():
    state = {"messages": dict([_written(8, "a")])}
    assert tl.plan_catchup_deletions(state, seen_ids=set(), window_min_id=None, last_id=10) == []


def test_plan_deletions_skips_non_written_records():
    state = {"messages": {"8": {"status": "quarantined"}, "9": {"status": "dry-run"}}}
    assert tl.plan_catchup_deletions(state, seen_ids=set(), window_min_id=7, last_id=10) == []


# ---------------------------------------------------------------------------
# catch_up orchestration (fake client; recorded calls)
# ---------------------------------------------------------------------------

def _msg(mid: int, text: str = "x", age_minutes: int = 5) -> SimpleNamespace:
    return SimpleNamespace(
        id=mid,
        date=datetime.now(timezone.utc) - timedelta(minutes=age_minutes),
        message=text,
    )


class _FakeClient:
    def __init__(self, msgs_newest_first):
        self._msgs = msgs_newest_first

    def iter_messages(self, chat, limit):
        async def gen():
            for m in self._msgs[:limit]:
                yield m
        return gen()


def _run_catch_up(channel_msgs_newest_first, state):
    fake = SimpleNamespace(
        state=state,
        client=_FakeClient(channel_msgs_newest_first),
        channel_id=123,
    )
    processed: list[tuple[int, bool]] = []
    revoked: list[int] = []

    async def _process_message(msg, *, is_edit):
        processed.append((msg.id, is_edit))

    async def _revoke_tracked_message(mid):
        revoked.append(mid)

    fake._process_message = _process_message
    fake._revoke_tracked_message = _revoke_tracked_message
    asyncio.run(tl.Listener.catch_up(fake))
    return processed, revoked


def test_catch_up_processes_new_rechecks_tracked_and_revokes_vanished():
    state = {
        "messages": dict([_written(9, "2026-06-09#01"), _written(8, "2026-06-09#02"),
                          _written(5, "2026-06-08#01")]),
        "last_processed_message_id": 10,
    }
    # Channel now: 12, 11 (new), 9 (tracked, still present), 7 (untracked).
    # Tracked id 8 vanished inside the scanned window -> deleted.
    # Tracked id 5 is below the window floor (7) -> untouched.
    processed, revoked = _run_catch_up([_msg(12), _msg(11), _msg(9), _msg(7)], state)
    assert processed == [(11, False), (12, False), (9, True)]
    assert revoked == [8]


def test_catch_up_with_no_tracked_messages_matches_legacy_behavior():
    state = {"messages": {}, "last_processed_message_id": 10}
    processed, revoked = _run_catch_up([_msg(12), _msg(11)], state)
    assert processed == [(11, False), (12, False)]
    assert revoked == []


# ---------------------------------------------------------------------------
# _process_message / _revoke_tracked_message end-to-end against tmp files
# ---------------------------------------------------------------------------

RAW_SIGNAL = (
    "\U0001F947BUY XAUUSD 4483 - 4481\n\U0001F534SL 4476\n"
    "\U0001F7E2TP1 4491\n\U0001F7E2TP2 4501\n\U0001F7E2TP3 4521"
)
FEED_LINE = "1. BUY XAUUSD 4483 - 4481 SL 4476 TP1 4491 TP2 4501 TP3 4521 8:11 PM"


def _wire_tmp_paths(monkeypatch, tmp_path) -> Path:
    feed = tmp_path / "signals.txt"
    feed.write_text(f"2026-06-09 GMT+7\n{FEED_LINE}\n", encoding="utf-8")
    monkeypatch.setattr(tl, "SIGNALS_PATH", feed)
    monkeypatch.setattr(tl, "STATE_PATH", tmp_path / "state.json")
    monkeypatch.setattr(tl, "OVERRIDES_PATH", tmp_path / "overrides.jsonl")
    monkeypatch.setattr(tl, "QUARANTINE_PATH", tmp_path / "quarantine.txt")
    return feed


def _listener_stub(state) -> tuple[SimpleNamespace, list[str]]:
    replies: list[str] = []

    async def _record(text, *_args, **_kwargs):
        replies.append(text)

    stub = SimpleNamespace(
        state=state, dry_run=False, _lock=asyncio.Lock(),
        _reply_saved=_record, _notify_amend=_record, _notify_revoke=_record,
        _notify_correction=_record, _notify_low_rr=_record, _notify_failure=_record,
    )
    return stub, replies


def _written_state() -> dict:
    return {
        "messages": {
            "100": {"status": "written", "signal_key": "2026-06-09#01", "line": FEED_LINE},
        },
        "last_processed_message_id": 100,
    }


def test_unchanged_edit_recheck_is_a_complete_noop(monkeypatch, tmp_path):
    feed = _wire_tmp_paths(monkeypatch, tmp_path)
    before = feed.read_text(encoding="utf-8")
    state = _written_state()
    stub, replies = _listener_stub(state)

    asyncio.run(tl.Listener._process_message(stub, _msg(100, RAW_SIGNAL), is_edit=True))

    assert feed.read_text(encoding="utf-8") == before
    assert not (tmp_path / "overrides.jsonl").exists()
    assert not (tmp_path / "state.json").exists()  # save_state never called
    assert state["messages"]["100"].get("edited") is None
    assert replies == []


def test_downtime_edit_updates_feed_and_queues_amend(monkeypatch, tmp_path):
    feed = _wire_tmp_paths(monkeypatch, tmp_path)
    state = _written_state()
    stub, _replies = _listener_stub(state)
    edited = RAW_SIGNAL.replace("SL 4476", "SL 4470")

    asyncio.run(tl.Listener._process_message(stub, _msg(100, edited), is_edit=True))

    body = feed.read_text(encoding="utf-8")
    assert "SL 4470" in body and "SL 4476" not in body
    assert "8:11 PM" in body  # original post time is identity, not the edit time
    override = json.loads((tmp_path / "overrides.jsonl").read_text().strip())
    assert override["action"] == "amend"
    assert override["signal_key"] == "2026-06-09#01"
    assert override["new"]["sl"] == 4470.0
    assert state["messages"]["100"]["edited"] is True


def test_marker_removed_edit_keeps_line_and_warns_once(monkeypatch, tmp_path):
    feed = _wire_tmp_paths(monkeypatch, tmp_path)
    before = feed.read_text(encoding="utf-8")
    state = _written_state()
    stub, replies = _listener_stub(state)
    retraction = _msg(100, "closing this idea, well done everyone")

    asyncio.run(tl.Listener._process_message(stub, retraction, is_edit=True))
    asyncio.run(tl.Listener._process_message(stub, retraction, is_edit=True))

    assert feed.read_text(encoding="utf-8") == before  # never silently dropped
    assert not (tmp_path / "overrides.jsonl").exists()
    assert state["messages"]["100"]["review_marker_removed"] is True
    assert len(replies) == 1 and "without the \U0001F947" in replies[0]


def test_revoke_tracked_message_removes_line_and_queues_revoke(monkeypatch, tmp_path):
    feed = _wire_tmp_paths(monkeypatch, tmp_path)
    state = _written_state()
    stub, replies = _listener_stub(state)

    asyncio.run(tl.Listener._revoke_tracked_message(stub, 100))

    assert FEED_LINE not in feed.read_text(encoding="utf-8")
    override = json.loads((tmp_path / "overrides.jsonl").read_text().strip())
    assert override == {
        "ts": override["ts"], "message_id": 100,
        "signal_key": "2026-06-09#01", "action": "revoke",
    }
    assert state["messages"]["100"]["status"] == "deleted_by_source"
    assert len(replies) == 1


def test_revoke_unknown_or_untracked_id_is_ignored(monkeypatch, tmp_path):
    feed = _wire_tmp_paths(monkeypatch, tmp_path)
    before = feed.read_text(encoding="utf-8")
    state = {"messages": {"100": {"status": "quarantined"}}, "last_processed_message_id": 100}
    stub, replies = _listener_stub(state)

    asyncio.run(tl.Listener._revoke_tracked_message(stub, 100))
    asyncio.run(tl.Listener._revoke_tracked_message(stub, 999))

    assert feed.read_text(encoding="utf-8") == before
    assert not (tmp_path / "overrides.jsonl").exists()
    assert replies == []
