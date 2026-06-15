"""Per-executor strategy tag: two live executors on one MT5 account must get
DISJOINT magic numbers + order comments so they never manage each other's orders.

The tag is stamped onto ``Signal.signal_key``; every magic/comment is derived from
that key, so a non-empty tag namespaces the whole executor. Empty (the default)
keeps backtests byte-identical.
"""
from __future__ import annotations

from xauusd_trading import parse_one_signal, parse_signals_file
from xauusd_trading.execution.mt5_executor import signal_to_magic, mt5_entry_comment

LINE = "3. BUY XAUUSD 2030 - 2028 SL 2025 TP1 2035 TP2 2040 TP3 2050 2:00 PM"


def test_tag_changes_signal_key_magic_and_comment():
    base = parse_one_signal(LINE, "2026-06-15", 7)            # tag defaults ""
    vic = parse_one_signal(LINE, "2026-06-15", 7); vic.tag = "VIC"
    sw = parse_one_signal(LINE, "2026-06-15", 7); sw.tag = "R4SW"

    # signal_key carries the tag (and the untagged default is unchanged).
    assert base.signal_key == "2026-06-15#03"
    assert vic.signal_key == "VIC2026-06-15#03"
    assert sw.signal_key == "R4SW2026-06-15#03"

    # Distinct magics across the two executors AND vs the untagged default.
    m_base = signal_to_magic(base.signal_key)
    m_vic = signal_to_magic(vic.signal_key)
    m_sw = signal_to_magic(sw.signal_key)
    assert len({m_base, m_vic, m_sw}) == 3

    # Comments carry the tag and stay within MT5's 31-char limit.
    c_vic = mt5_entry_comment(vic.signal_key, 7)   # entry #8
    c_sw = mt5_entry_comment(sw.signal_key, 7)
    assert c_vic.startswith("VIC") and len(c_vic) <= 31
    assert c_sw.startswith("R4SW") and c_vic != c_sw


def test_parse_signals_file_applies_tag(tmp_path):
    feed = tmp_path / "f.txt"
    feed.write_text("2026-06-15 GMT+7\n" + LINE + "\n", encoding="utf-8")

    untagged = parse_signals_file(feed)
    tagged = parse_signals_file(feed, tag="R4SW")
    assert untagged[0].signal_key == "2026-06-15#03"
    assert tagged[0].signal_key == "R4SW2026-06-15#03"
    # Same market signal, different magic namespace.
    assert signal_to_magic(untagged[0].signal_key) != signal_to_magic(tagged[0].signal_key)
