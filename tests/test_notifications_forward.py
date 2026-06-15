"""Tail/offset logic for the listener's engine-notifications bridge.

Covers the pure functions that decide which JSONL events to forward to Saved
Messages. The Telethon-driven forwarder coroutine itself is exercised live;
these tests pin the parsing/offset behaviour that must never silently drop or
duplicate an event.
"""
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "listener"))
import telegram_listener as tl  # noqa: E402


def _append(path: Path, *objs) -> None:
    with path.open("ab") as f:
        for o in objs:
            f.write((json.dumps(o) + "\n").encode("utf-8"))


def test_reads_complete_lines_and_advances(tmp_path):
    p = tmp_path / "n.jsonl"
    _append(p, {"text": "a"}, {"text": "b"})
    events, off = tl.read_new_notification_events(p, 0)
    assert [e["text"] for e in events] == ["a", "b"]
    assert off == p.stat().st_size
    # A second read with the advanced offset yields nothing new.
    events2, off2 = tl.read_new_notification_events(p, off)
    assert events2 == []
    assert off2 == off


def test_partial_final_line_not_consumed(tmp_path):
    p = tmp_path / "n.jsonl"
    _append(p, {"text": "a"})
    with p.open("ab") as f:
        f.write(b'{"text": "partial"')  # no trailing newline yet
    events, off = tl.read_new_notification_events(p, 0)
    assert [e["text"] for e in events] == ["a"]
    with p.open("rb") as f:
        f.seek(off)
        assert f.read() == b'{"text": "partial"'  # offset stops before partial
    # Once the line is completed it is read on the next poll.
    with p.open("ab") as f:
        f.write(b', "x": 1}\n')
    events2, off2 = tl.read_new_notification_events(p, off)
    assert [e["text"] for e in events2] == ["partial"]
    assert off2 == p.stat().st_size


def test_multibyte_offset_is_byte_accurate(tmp_path):
    p = tmp_path / "n.jsonl"
    _append(p, {"text": "\U0001F3AF TP3 hit"}, {"text": "\U0001F6D1 SL"})
    events, off = tl.read_new_notification_events(p, 0)
    assert [e["text"] for e in events] == ["\U0001F3AF TP3 hit", "\U0001F6D1 SL"]
    # Offset is a byte count: it must equal the file's byte size, which is
    # larger than the character count because the emoji are multi-byte.
    assert off == p.stat().st_size
    assert off > len("\U0001F3AF TP3 hit\U0001F6D1 SL")


def test_truncation_resets_offset(tmp_path):
    p = tmp_path / "n.jsonl"
    _append(p, {"text": "a"}, {"text": "b"})
    _, off = tl.read_new_notification_events(p, 0)
    p.write_bytes(b'{"text": "c"}\n')  # rotated to a smaller file
    events, off2 = tl.read_new_notification_events(p, off)
    assert [e["text"] for e in events] == ["c"]
    assert off2 == p.stat().st_size


def test_malformed_line_skipped_but_offset_advances(tmp_path):
    p = tmp_path / "n.jsonl"
    with p.open("ab") as f:
        f.write(b"not json at all\n")
        f.write((json.dumps({"text": "ok"}) + "\n").encode("utf-8"))
    events, off = tl.read_new_notification_events(p, 0)
    assert [e["text"] for e in events] == ["ok"]
    assert off == p.stat().st_size


def test_missing_file_keeps_offset(tmp_path):
    p = tmp_path / "absent.jsonl"
    events, off = tl.read_new_notification_events(p, 0)
    assert events == []
    assert off == 0


def test_offset_sidecar_roundtrip(tmp_path):
    op = tmp_path / "n.jsonl.offset"
    assert tl.read_notification_offset(op) == -1  # absent -> start-at-EOF sentinel
    tl.write_notification_offset(op, 4096)
    assert tl.read_notification_offset(op) == 4096
