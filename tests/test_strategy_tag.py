"""Per-executor strategy tag: two live executors on one MT5 account must get
DISJOINT magic numbers + order comments so they never manage each other's orders.

The tag is stamped onto ``Signal.signal_key``; every magic/comment is derived from
that key, so a non-empty tag namespaces the whole executor. Empty (the default)
keeps backtests byte-identical.
"""
from __future__ import annotations

from trading.engine import parse_one_signal, parse_signals_file
from trading.engine.execution.mt5_executor import signal_to_magic, mt5_entry_comment

LINE = "3. BUY XAUUSD 2030 - 2028 SL 2025 TP1 2035 TP2 2040 TP3 2050 2:00 PM"


def test_tag_changes_signal_key_magic_and_comment():
    base = parse_one_signal(LINE, "2026-06-15", 7)            # tag defaults ""
    vic = parse_one_signal(LINE, "2026-06-15", 7); vic.tag = "VIC"
    sw = parse_one_signal(LINE, "2026-06-15", 7); sw.tag = "R4SW"

    # signal_key carries the tag (and the untagged default is unchanged).
    assert base.signal_key == "2026-06-15#03"
    assert vic.signal_key == "VIC-2026-06-15#03"
    assert sw.signal_key == "R4SW-2026-06-15#03"

    # Distinct magics across the two executors AND vs the untagged default.
    m_base = signal_to_magic(base.signal_key)
    m_vic = signal_to_magic(vic.signal_key)
    m_sw = signal_to_magic(sw.signal_key)
    assert len({m_base, m_vic, m_sw}) == 3

    # Comments carry the tag and stay within MT5's 31-char limit (and the
    # tighter ~16-char broker truncation cap the compact format targets).
    c_vic = mt5_entry_comment(vic.signal_key, 7)   # entry #8
    c_sw = mt5_entry_comment(sw.signal_key, 7)
    assert c_vic.startswith("VIC") and len(c_vic) <= 16
    assert c_sw.startswith("R4SW") and c_vic != c_sw and len(c_sw) <= 16


def test_tag_is_capped_at_four_chars():
    # A tag longer than 4 chars keeps only the first 4 so the compact comment
    # `[TAG-]MMDD#DD.N` always fits the broker truncation limit (~16 chars).
    long = parse_one_signal(LINE, "2026-06-15", 7); long.tag = "R4S24"   # 5 chars
    assert long.signal_key == "R4S2-2026-06-15#03"
    c = mt5_entry_comment(long.signal_key, 9)      # entry #10 (2-digit)
    assert c == "R4S2-0615#03.10" and len(c) <= 16


def test_replay_tracked_signal_recovers_tag_from_registry_key():
    # The manage/reopen path rebuilds a tracked signal from its registry entry by
    # re-parsing the raw text -- which drops the tag unless recovered from the
    # stored signal_key. Without recovery the replayed Position computes the
    # UNTAGGED magic + comment, orphaning reopened legs (the 0615#48.1 bug).
    from trading.engine.cli_orig import _tag_from_signal_key

    assert _tag_from_signal_key("SC24-2026-06-15#48") == "SC24"
    assert _tag_from_signal_key("VIC-2026-06-15#04") == "VIC"
    assert _tag_from_signal_key("R4S2-2026-06-15#01") == "R4S2"
    assert _tag_from_signal_key("2026-06-15#48") == ""      # untagged/legacy
    assert _tag_from_signal_key("") == ""

    line = "48. BUY XAUUSD 2030 - 2028 SL 2025 TP1 2035 TP2 2040 TP3 2050 5:47 PM"
    item = {"signal": line, "date": "2026-06-15", "tz": 7,
            "signal_key": "SC24-2026-06-15#48"}
    psig = parse_one_signal(item["signal"], item["date"], int(item["tz"]))
    psig.tag = _tag_from_signal_key(item["signal_key"])
    # Replayed identity matches what place_signal stamped: same key, magic, comment.
    assert psig.signal_key == item["signal_key"]
    assert signal_to_magic(psig.signal_key) == signal_to_magic(item["signal_key"])
    assert mt5_entry_comment(psig.signal_key, 0) == "SC24-0615#48.1"


def test_parse_signals_file_applies_tag(tmp_path):
    feed = tmp_path / "f.txt"
    feed.write_text("2026-06-15 GMT+7\n" + LINE + "\n", encoding="utf-8")

    untagged = parse_signals_file(feed)
    tagged = parse_signals_file(feed, tag="R4SW")
    assert untagged[0].signal_key == "2026-06-15#03"
    assert tagged[0].signal_key == "R4SW-2026-06-15#03"
    # Same market signal, different magic namespace.
    assert signal_to_magic(untagged[0].signal_key) != signal_to_magic(tagged[0].signal_key)
