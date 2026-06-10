#!/usr/bin/env python3
"""Convert a Telegram Desktop HTML export into the canonical signals format.

Backfill path for days when the live listener wasn't running: export the
VICTOR - GOLD PRIORITY channel from Telegram Desktop (ChatExport_*/
messages*.html) and run this tool over the HTML to regenerate the exact
`signals.txt` / `victor_signals.txt` sections for those days.

This deliberately reuses the live listener's pipeline
(`listener/telegram_listener.py`): the same 🥇 new-signal marker, the same
lenient field extraction (comma decimals like `SL 4515,50`), the same
logic-only typo corrections, the same `N. SIDE XAUUSD R1 - R2 SL S TP1 ..`
rendering, and the same content+time dedup. A backfilled section is therefore
what the listener would have appended had it been up — including reposts of
the same signal at a later time, which become their own numbered entries.

Message timestamps come from each message's `title="DD.MM.YYYY HH:MM:SS
UTC+HH:MM"` attribute; whatever zone the export was rendered in, times are
converted to the GMT+7 the feed headers promise. Edited messages appear in an
export only in their final form, so corrections VICTOR made by editing are
picked up automatically; messages he deleted are absent entirely.

Usage (from repo root):
    python tools/telegram_export_to_signals.py "ChatExport_2026-06-10/messages*.html"
    python tools/telegram_export_to_signals.py export/messages.html --out victor_june.txt
    python tools/telegram_export_to_signals.py export/messages.html --merge-into victor_signals.txt

`--merge-into` brings the feed to the channel's latest state for the exported
days: each covered date section is replaced wholesale (VICTOR's edits applied,
signals he deleted dropped), other dates stay untouched, new dates insert in
order. Re-running with the same export is a no-op.

Output goes to stdout unless --out is given. 🥇-marked messages that fail to
parse are reported on stderr (the live listener would have quarantined them);
re-check those days by hand.
"""
from __future__ import annotations

import argparse
import glob
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "listener"))

from telegram_listener import (  # noqa: E402
    DATE_HEADER_RE,
    SIGNAL_SOURCE_TZ,
    SIGNAL_SOURCE_TZ_OFFSET,
    ParsedSignal,
    _format_time,
    apply_signal_corrections,
    parse_victor_signal,
)

# `title` attribute of each message's date element, e.g.
# "01.06.2026 09:07:57 UTC+07:00". The offset is whatever zone Telegram
# Desktop rendered the export in, so parse it rather than assuming GMT+7.
_TITLE_DT_RE = re.compile(
    r"(?P<d>\d{2})\.(?P<mo>\d{2})\.(?P<y>\d{4})\s+"
    r"(?P<h>\d{2}):(?P<mi>\d{2}):(?P<s>\d{2})\s+"
    r"UTC(?P<sign>[+-])(?P<oh>\d{2}):(?P<om>\d{2})"
)


def _parse_title_dt(title: str) -> datetime | None:
    """`01.06.2026 09:07:57 UTC+07:00` -> naive GMT+7 datetime, or None."""
    m = _TITLE_DT_RE.match(title.strip())
    if not m:
        return None
    offset = timedelta(hours=int(m.group("oh")), minutes=int(m.group("om")))
    if m.group("sign") == "-":
        offset = -offset
    dt = datetime(
        int(m.group("y")), int(m.group("mo")), int(m.group("d")),
        int(m.group("h")), int(m.group("mi")), int(m.group("s")),
        tzinfo=timezone(offset),
    )
    return dt.astimezone(SIGNAL_SOURCE_TZ).replace(tzinfo=None)


class _ExportParser(HTMLParser):
    """Pull (message_id, timestamp, body text) out of a Telegram HTML export.

    A message is a `div.message.default` (the `joined` variant included); its
    timestamp is the first `div.date.details` title inside it and its body is
    the `div.text`. `<br>` and block boundaries become newlines so the body
    matches what Telethon would have delivered as `msg.message`.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.messages: list[tuple[str, datetime, str]] = []
        self.skipped_no_timestamp = 0
        self._div_depth = 0
        self._msg_depth: int | None = None
        self._msg_id = ""
        self._msg_dt: datetime | None = None
        self._text_depth: int | None = None
        self._buf: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "div":
            self._div_depth += 1
            a = dict(attrs)
            cls = (a.get("class") or "").split()
            if "message" in cls and "default" in cls:
                self._msg_depth = self._div_depth
                self._msg_id = a.get("id") or ""
                self._msg_dt = None
            elif (
                self._msg_depth is not None
                and self._msg_dt is None
                and "date" in cls
                and "details" in cls
                and a.get("title")
            ):
                self._msg_dt = _parse_title_dt(a["title"] or "")
            elif self._msg_depth is not None and cls == ["text"] and self._text_depth is None:
                self._text_depth = self._div_depth
                self._buf = []
        elif self._text_depth is not None and tag in ("br", "blockquote", "p"):
            self._buf.append("\n")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        if tag == "div":
            if self._text_depth == self._div_depth:
                self._emit()
                self._text_depth = None
            if self._msg_depth == self._div_depth:
                self._msg_depth = None
            self._div_depth -= 1
        elif self._text_depth is not None and tag == "blockquote":
            self._buf.append("\n")

    def handle_data(self, data: str) -> None:
        if self._text_depth is not None:
            self._buf.append(data)

    def _emit(self) -> None:
        text = "".join(self._buf).strip()
        if not text:
            return
        if self._msg_dt is None:
            self.skipped_no_timestamp += 1
            return
        self.messages.append((self._msg_id, self._msg_dt, text))


def extract_messages(paths: list[Path]) -> list[tuple[str, datetime, str]]:
    """All (id, GMT+7 datetime, text) messages across the export files, in time order."""
    parser = _ExportParser()
    for path in paths:
        parser.feed(path.read_text(encoding="utf-8"))
    parser.close()
    if parser.skipped_no_timestamp:
        print(
            f"WARNING: skipped {parser.skipped_no_timestamp} message(s) with no "
            "parseable timestamp",
            file=sys.stderr,
        )
    return sorted(parser.messages, key=lambda m: m[1])


def _dedup_key(parsed: ParsedSignal, time_text: str) -> tuple:
    # Mirrors the listener's _find_matching_signal_in_section: identical
    # side+prices+time is one signal, the same content at a later time is a
    # repost and keeps its own entry.
    return (
        parsed.side,
        parsed.r1, parsed.r2, parsed.sl, parsed.tp1, parsed.tp2, parsed.tp3,
        time_text.upper().replace(" ", ""),
    )


def build_sections(
    messages: list[tuple[str, datetime, str]],
) -> tuple[dict[str, list[str]], list[str], list[str]]:
    """(date -> signal lines, correction notes, parse failures)."""
    sections: dict[str, list[str]] = {}
    seen: dict[str, set[tuple]] = {}
    corrections: list[str] = []
    failures: list[str] = []

    for msg_id, dt_gmt7, text in messages:
        try:
            parsed_raw = parse_victor_signal(text)
        except ValueError as e:
            failures.append(f"{dt_gmt7:%Y-%m-%d %H:%M} {msg_id}: {e}")
            continue
        if parsed_raw is None:
            continue

        fix = apply_signal_corrections(parsed_raw)
        parsed = fix.corrected
        date_str = dt_gmt7.strftime("%Y-%m-%d")
        time_text = _format_time(dt_gmt7)

        key = _dedup_key(parsed, time_text)
        if key in seen.setdefault(date_str, set()):
            continue
        seen[date_str].add(key)

        lines = sections.setdefault(date_str, [])
        line = parsed.to_line(len(lines) + 1, time_text)
        lines.append(line)
        if fix.changed:
            corrections.append(f"{date_str} #{len(lines)}: {', '.join(fix.changes)}")

    return sections, corrections, failures


def _tz_label() -> str:
    return f"GMT+{SIGNAL_SOURCE_TZ_OFFSET}" if SIGNAL_SOURCE_TZ_OFFSET >= 0 \
        else f"GMT{SIGNAL_SOURCE_TZ_OFFSET}"


def render(sections: dict[str, list[str]]) -> str:
    tz_label = _tz_label()
    blocks = [
        "\n".join([f"{date} {tz_label}"] + sections[date])
        for date in sorted(sections)
    ]
    return "\n\n".join(blocks) + "\n"


def _split_feed_blocks(lines: list[str]) -> tuple[list[str], list[tuple[str, list[str]]]]:
    """Split a feed into (preamble lines, [(date, section lines incl. header)]).

    Trailing blank lines are stripped from each section; rendering re-inserts a
    single blank line between sections, so untouched days survive a merge
    byte-identical in content.
    """
    preamble: list[str] = []
    blocks: list[tuple[str, list[str]]] = []
    current_date: str | None = None
    current: list[str] = []

    def flush() -> None:
        while current and not current[-1].strip():
            current.pop()
        if current_date is not None:
            blocks.append((current_date, list(current)))

    for line in lines:
        m = DATE_HEADER_RE.match(line)
        if m:
            flush()
            current_date = m.group("date")
            current = [line.rstrip()]
        elif current_date is None:
            preamble.append(line)
        else:
            current.append(line.rstrip())
    flush()
    while preamble and not preamble[-1].strip():
        preamble.pop()
    return preamble, blocks


def merge_sections_into_feed(feed_path: Path, sections: dict[str, list[str]]) -> dict[str, str]:
    """Bring `feed_path` to the channel's latest state for the exported days.

    The export reflects the channel as it is *now* — edits in their final form,
    deleted messages absent — so each date the export covers replaces that
    date's section wholesale: changed signals are corrected, signals VICTOR
    removed disappear. Days the export doesn't cover are preserved untouched,
    and new days are inserted in date order. The write is atomic (tmp +
    os.replace), matching the live listener. Returns {date: 'replaced' |
    'unchanged' | 'added'}.
    """
    old_lines = (
        feed_path.read_text(encoding="utf-8").splitlines() if feed_path.exists() else []
    )
    preamble, blocks = _split_feed_blocks(old_lines)
    tz_label = _tz_label()
    remaining = dict(sections)
    summary: dict[str, str] = {}

    merged: list[tuple[str, list[str]]] = []
    for date_str, section in blocks:
        if date_str in remaining:
            new_section = [f"{date_str} {tz_label}"] + remaining.pop(date_str)
            summary[date_str] = "unchanged" if new_section == section else "replaced"
            merged.append((date_str, new_section))
        else:
            merged.append((date_str, section))
    for date_str in sorted(remaining):
        new_section = [f"{date_str} {tz_label}"] + remaining[date_str]
        at = next((i for i, (d, _) in enumerate(merged) if d > date_str), len(merged))
        merged.insert(at, (date_str, new_section))
        summary[date_str] = "added"

    parts = ["\n".join(preamble)] if preamble else []
    parts.extend("\n".join(section) for _, section in merged)
    content = "\n\n".join(parts) + "\n"

    tmp = feed_path.with_suffix(feed_path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, feed_path)
    return summary


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "export_html",
        help="Telegram Desktop export HTML file or glob, e.g. 'ChatExport_*/messages*.html'",
    )
    ap.add_argument("--out", help="Write sections here instead of stdout.")
    ap.add_argument(
        "--merge-into",
        help="Feed file (e.g. victor_signals.txt) to bring up to the channel's "
             "latest state: every date the export covers is replaced wholesale "
             "(edits applied, deleted signals dropped), other dates untouched, "
             "new dates inserted in order.",
    )
    args = ap.parse_args(argv)

    paths = sorted(Path(p) for p in glob.glob(args.export_html))
    if not paths:
        print(f"No files match {args.export_html!r}", file=sys.stderr)
        return 1

    messages = extract_messages(paths)
    sections, corrections, failures = build_sections(messages)
    if not sections:
        print("No 🥇 signal messages found in the export.", file=sys.stderr)
        return 1

    for note in corrections:
        print(f"AUTO-CORRECTED {note}", file=sys.stderr)
    for note in failures:
        print(f"PARSE FAILURE (would be quarantined) {note}", file=sys.stderr)

    if args.merge_into:
        summary = merge_sections_into_feed(Path(args.merge_into), sections)
        for date_str in sorted(summary):
            print(f"{summary[date_str].upper():9s} {date_str} "
                  f"({len(sections[date_str])} signal(s))", file=sys.stderr)
        changed = sum(1 for v in summary.values() if v != "unchanged")
        print(f"Merged into {args.merge_into}: {changed} day(s) updated, "
              f"{len(summary) - changed} already at latest state", file=sys.stderr)
        if not args.out:
            return 0

    text = render(sections)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        n = sum(len(v) for v in sections.values())
        print(f"Wrote {n} signals across {len(sections)} day(s) to {args.out}", file=sys.stderr)
    elif not args.merge_into:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
