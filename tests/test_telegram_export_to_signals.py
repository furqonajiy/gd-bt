"""Telegram HTML export -> canonical signal sections, and feed sync.

The converter must produce exactly what the live listener would have written
(same 🥇 marker filter, comma-decimal handling, content+time dedup, repost
numbering), and `--merge-into` must bring a feed to the channel's latest
state for the exported days — replacing covered sections (edits applied,
deleted signals dropped) while preserving other days verbatim, idempotently.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.telegram_export_to_signals import (  # noqa: E402
    build_sections,
    extract_messages,
    merge_sections_into_feed,
    render,
)


def _message_div(mid: int, title: str, text_html: str) -> str:
    return (
        f'<div class="message default clearfix" id="message{mid}">\n'
        f' <div class="body">\n'
        f'  <div class="pull_right date details" title="{title}">09:00</div>\n'
        f'  <div class="from_name">VICTOR</div>\n'
        f'  <div class="text">{text_html}</div>\n'
        f' </div>\n'
        f'</div>\n'
    )


SIGNAL_HTML = (
    "<blockquote>\U0001F947BUY XAUUSD 4483 - 4481<br>"
    "\U0001F534SL 4476,50<br>\U0001F7E2TP1 4491<br>"
    "\U0001F7E2TP2 4501<br>\U0001F7E2TP3 4521</blockquote>"
)


def _write_export(tmp_path: Path, *divs: str) -> Path:
    path = tmp_path / "messages.html"
    path.write_text(
        '<html><body><div class="history">\n' + "".join(divs) + "</div></body></html>",
        encoding="utf-8",
    )
    return path


def test_signal_extraction_comma_decimals_and_marker_filter(tmp_path):
    export = _write_export(
        tmp_path,
        _message_div(1, "09.06.2026 20:11:00 UTC+07:00", SIGNAL_HTML),
        _message_div(2, "09.06.2026 21:00:00 UTC+07:00", "TP1 hit, move SL to entry"),
    )
    sections, corrections, failures = build_sections(extract_messages([export]))
    assert failures == [] and corrections == []
    assert sections == {
        "2026-06-09": [
            "1. BUY XAUUSD 4483 - 4481 SL 4476.50 TP1 4491 TP2 4501 TP3 4521 8:11 PM",
        ]
    }


def test_repost_at_new_time_kept_exact_duplicate_dropped(tmp_path):
    export = _write_export(
        tmp_path,
        _message_div(1, "09.06.2026 15:11:00 UTC+07:00", SIGNAL_HTML),
        _message_div(2, "09.06.2026 15:11:00 UTC+07:00", SIGNAL_HTML),  # same minute: dup
        _message_div(3, "09.06.2026 20:11:00 UTC+07:00", SIGNAL_HTML),  # repost: kept
    )
    sections, _corrections, _failures = build_sections(extract_messages([export]))
    lines = sections["2026-06-09"]
    assert len(lines) == 2
    assert lines[0].endswith("3:11 PM") and lines[0].startswith("1.")
    assert lines[1].endswith("8:11 PM") and lines[1].startswith("2.")


def test_export_timezone_is_converted_to_gmt7(tmp_path):
    # Same instant rendered by Telegram Desktop in UTC+03:00.
    export = _write_export(
        tmp_path,
        _message_div(1, "09.06.2026 16:11:00 UTC+03:00", SIGNAL_HTML),
    )
    sections, _c, _f = build_sections(extract_messages([export]))
    assert sections["2026-06-09"][0].endswith("8:11 PM")
    assert render(sections).startswith("2026-06-09 GMT+7\n")


# ---------------------------------------------------------------------------
# merge_sections_into_feed
# ---------------------------------------------------------------------------

DAY8 = "1. SELL XAUUSD 4325 - 4327 SL 4332.50 TP1 4317 TP2 4307 TP3 4287 11:16 AM"
DAY9_OLD = [
    "1. BUY XAUUSD 4330 - 4328 SL 4323 TP1 4338 TP2 4348 TP3 4368 1:32 PM",
    "2. BUY XAUUSD 4323 - 4321 SL 4316 TP1 4331 TP2 4341 TP3 4361 2:16 PM",
]
DAY9_NEW = [
    # VICTOR edited #1's SL and deleted #2 -> the export's latest state.
    "1. BUY XAUUSD 4330 - 4328 SL 4325 TP1 4338 TP2 4348 TP3 4368 1:32 PM",
]
DAY10 = ["1. SELL XAUUSD 4220 - 4222 SL 4227.50 TP1 4212 TP2 4202 TP3 4182 1:36 PM"]


def _feed(tmp_path: Path) -> Path:
    feed = tmp_path / "victor_signals.txt"
    feed.write_text(
        "2026-06-08 GMT+7\n" + DAY8 + "\n\n"
        "2026-06-09 GMT+7\n" + "\n".join(DAY9_OLD) + "\n",
        encoding="utf-8",
    )
    return feed


def test_merge_applies_edits_and_deletions_keeps_other_days(tmp_path):
    feed = _feed(tmp_path)
    summary = merge_sections_into_feed(feed, {"2026-06-09": DAY9_NEW, "2026-06-10": DAY10})

    assert summary == {"2026-06-09": "replaced", "2026-06-10": "added"}
    assert feed.read_text(encoding="utf-8") == (
        "2026-06-08 GMT+7\n" + DAY8 + "\n\n"
        "2026-06-09 GMT+7\n" + DAY9_NEW[0] + "\n\n"
        "2026-06-10 GMT+7\n" + DAY10[0] + "\n"
    )


def test_merge_is_idempotent(tmp_path):
    feed = _feed(tmp_path)
    merge_sections_into_feed(feed, {"2026-06-09": DAY9_NEW, "2026-06-10": DAY10})
    after_first = feed.read_text(encoding="utf-8")

    summary = merge_sections_into_feed(feed, {"2026-06-09": DAY9_NEW, "2026-06-10": DAY10})
    assert summary == {"2026-06-09": "unchanged", "2026-06-10": "unchanged"}
    assert feed.read_text(encoding="utf-8") == after_first


def test_merge_inserts_new_day_in_date_order(tmp_path):
    feed = _feed(tmp_path)
    day7 = ["1. BUY XAUUSD 4400 - 4398 SL 4393 TP1 4408 TP2 4418 TP3 4438 9:00 AM"]
    merge_sections_into_feed(feed, {"2026-06-07": day7})

    body = feed.read_text(encoding="utf-8")
    assert body.index("2026-06-07") < body.index("2026-06-08") < body.index("2026-06-09")


def test_merge_into_missing_feed_creates_it(tmp_path):
    feed = tmp_path / "new_feed.txt"
    summary = merge_sections_into_feed(feed, {"2026-06-10": DAY10})
    assert summary == {"2026-06-10": "added"}
    assert feed.read_text(encoding="utf-8") == "2026-06-10 GMT+7\n" + DAY10[0] + "\n"
