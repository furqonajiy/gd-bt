"""Edit/delete propagation in the listener (Step 2).

Covers the pure file helpers that rewrite or remove a signals.txt line by
signal_key, keep numbering stable across deletions, and emit the amend/revoke
control records `auto` consumes. The Telethon-driven handlers themselves run
live; these pin the file behaviour that touches the live feed.
"""
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "listeners" / "telegram"))
import telegram_listener as tl  # noqa: E402

_SECTION = (
    "2026-06-05 GMT+7\n"
    "1. BUY XAUUSD 4440 - 4438 SL 4433 TP1 4448 TP2 4458 TP3 4478 6:24 PM\n"
    "2. SELL XAUUSD 4500 - 4502 SL 4507 TP1 4493 TP2 4485 TP3 4470 7:10 PM\n"
)


@pytest.fixture
def feed(tmp_path, monkeypatch):
    sig = tmp_path / "signals.txt"
    monkeypatch.setattr(tl, "SIGNALS_PATH", sig)
    monkeypatch.setattr(tl, "OVERRIDES_PATH", tmp_path / "signal_overrides.jsonl")
    return sig


def test_update_signal_in_file_preserves_number_and_time(feed):
    feed.write_text(_SECTION, encoding="utf-8")
    corrected = tl.ParsedSignal(side="BUY", r1=4440, r2=4438, sl=4430,
                                tp1=4450, tp2=4458, tp3=4478)
    result = tl.update_signal_in_file("2026-06-05#01", corrected)
    assert result is not None
    old_line, new_line = result
    assert old_line.startswith("1. BUY XAUUSD 4440 - 4438 SL 4433")
    # Same number (1) and same original post-time (6:24 PM); only SL/TP1 change.
    assert new_line == "1. BUY XAUUSD 4440 - 4438 SL 4430 TP1 4450 TP2 4458 TP3 4478 6:24 PM"
    text = feed.read_text(encoding="utf-8")
    assert "SL 4430 TP1 4450" in text
    assert "2. SELL XAUUSD 4500 - 4502" in text          # neighbour untouched


def test_update_signal_no_change_is_noop(feed):
    feed.write_text(_SECTION, encoding="utf-8")
    same = tl.ParsedSignal(side="BUY", r1=4440, r2=4438, sl=4433,
                           tp1=4448, tp2=4458, tp3=4478)
    before = feed.read_text(encoding="utf-8")
    old_line, new_line = tl.update_signal_in_file("2026-06-05#01", same)
    assert old_line == new_line
    assert feed.read_text(encoding="utf-8") == before     # file not rewritten differently


def test_update_missing_key_returns_none(feed):
    feed.write_text(_SECTION, encoding="utf-8")
    p = tl.ParsedSignal(side="BUY", r1=1, r2=2, sl=3, tp1=4, tp2=5, tp3=6)
    assert tl.update_signal_in_file("2026-06-05#09", p) is None     # no such number
    assert tl.update_signal_in_file("2026-06-04#01", p) is None     # no such day


def test_remove_signal_from_file(feed):
    feed.write_text(_SECTION, encoding="utf-8")
    removed = tl.remove_signal_from_file("2026-06-05#01")
    assert removed.startswith("1. BUY XAUUSD 4440 - 4438")
    text = feed.read_text(encoding="utf-8")
    assert "1. BUY XAUUSD 4440" not in text
    assert "2. SELL XAUUSD 4500 - 4502" in text           # other number preserved
    assert "2026-06-05 GMT+7" in text                      # header stays


def test_next_index_skips_a_deleted_number(feed):
    feed.write_text(
        "2026-06-05 GMT+7\n"
        "1. BUY XAUUSD 4440 - 4438 SL 4433 TP1 4448 TP2 4458 TP3 4478 6:24 PM\n"
        "2. SELL XAUUSD 4500 - 4502 SL 4507 TP1 4493 TP2 4485 TP3 4470 7:10 PM\n"
        "3. BUY XAUUSD 4460 - 4458 SL 4453 TP1 4468 TP2 4478 TP3 4498 8:00 PM\n",
        encoding="utf-8",
    )
    tl.remove_signal_from_file("2026-06-05#02")
    lines = tl._read_signals_lines()
    header = tl._find_section(lines, "2026-06-05")
    # max id is 3 -> next must be 4, never 3 (which would collide with the live #3).
    assert tl._next_index_in_section(lines, header) == 4

    from datetime import datetime
    new = tl.ParsedSignal(side="SELL", r1=4490, r2=4492, sl=4497,
                          tp1=4483, tp2=4475, tp3=4460)
    line, idx, dup = tl.write_signal_to_file(new, datetime(2026, 6, 5, 21, 30))
    assert (idx, dup) is not None and idx == 4 and dup is False
    assert line.startswith("4. SELL XAUUSD 4490 - 4492")


def test_emit_override_appends_jsonl(feed):
    tl.emit_override({"ts": "t1", "message_id": 10, "signal_key": "2026-06-05#01",
                      "action": "amend",
                      "new": {"side": "BUY", "r1": 4440, "r2": 4438, "sl": 4430,
                              "tp1": 4450, "tp2": 4458, "tp3": 4478}})
    tl.emit_override({"ts": "t2", "message_id": 11, "signal_key": "2026-06-05#02",
                      "action": "revoke"})
    rows = [json.loads(x) for x in tl.OVERRIDES_PATH.read_text(encoding="utf-8").splitlines()]
    assert [r["action"] for r in rows] == ["amend", "revoke"]
    assert rows[0]["new"]["sl"] == 4430
    assert rows[1]["signal_key"] == "2026-06-05#02"
    assert "new" not in rows[1]


def test_parse_signal_key():
    assert tl._parse_signal_key("2026-06-05#03") == ("2026-06-05", 3)
    assert tl._parse_signal_key("2026-06-05#12") == ("2026-06-05", 12)
    assert tl._parse_signal_key("garbage") is None
    assert tl._parse_signal_key("2026-06-05#x") is None