"""telegram_listener.py - VICTOR - GOLD PRIORITY signal listener.

Watches one Telegram channel for new XAUUSD signals, parses them, and
appends to the project's `signals.txt` in the format `xauusd_trading` expects.
Runs as a separate process from `xauusd_trading.cli auto`; the two communicate
only through `signals.txt` (atomically written, so `auto` can never read a
half-written file).

Project layout note
-------------------
This script lives at `<repo-root>/listener/telegram_listener.py`. All of
its runtime files (config, state, quarantine, session, and the shared
signals.txt) live at the project root next to `xauusd_trading/`. The
`REPO_ROOT` constant below resolves to that root by walking up two
levels from `__file__` (one for the file itself, one for the `listener/`
directory). Running the script from any CWD therefore works the same
way -- relative paths to data files are not assumed.

One-time set-up
---------------
1.  Activate the conda env you already use:
        conda activate xauusd
    Then install Telethon:
        pip install telethon

2.  Get API credentials:
        Open https://my.telegram.org -> "API development tools" -> create app.
        Note the `api_id` (integer) and `api_hash` (string).

3.  Copy the template, fill api_id + api_hash (paths are repo-root-relative):
        copy listener_config.example.json listener_config.json

4.  Find the VICTOR channel's numeric id (one-shot):
        python listener\\telegram_listener.py list-chats
    First run will prompt for your phone number, then SMS code, then 2FA
    password if you have one. A `.session` file is created so subsequent
    runs are silent.
    Find the row whose TITLE is "VICTOR - GOLD PRIORITY" (or whatever the
    channel is called now). Copy the ID into `listener_config.json` under
    `channel_id` (replace null).

5.  Run the listener:
        python listener\\telegram_listener.py
    From IntelliJ: open a second PowerShell terminal alongside the one
    running `xauusd_trading.cli auto`, both with the `xauusd` env active.

Holiday correction workflow
---------------------------
When the listener can't parse a VICTOR message, it sends a notification to
your own Saved Messages with the raw text and a pre-filled correction line.
On your phone:
  - Open Saved Messages.
  - Edit the suggested line until it's correct.
  - Send it.
The listener validates it against the engine's strict format, renumbers it
to the next free index in today's section of signals.txt, and appends.
You'll get a `✅ Injected` reply on success or `❌ <reason>` on failure.

You can also inject a brand-new signal at any time by sending a canonical
line to Saved Messages, even without a Victor failure to respond to.

Files (all at repo root, not in listener/)
------------------------------------------
  signals.txt              read + append (atomic via os.replace)
  telegram_state.json      message_id -> parsed status; for dedup + edits
  telegram_quarantine.txt  appended raw text of every unparseable message
  listener_config.json     api_id, api_hash, channel_id
  telegram.session         Telethon auth token (auto-created on first run)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

# Telethon is required. Give a helpful error if it's missing.
try:
    from telethon import TelegramClient, events
except ImportError:
    sys.stderr.write(
        "telethon not installed. In your xauusd env, run:\n"
        "    pip install telethon\n"
    )
    sys.exit(1)


# ---------------------------------------------------------------------------
# Paths and constants
# ---------------------------------------------------------------------------

# The listener script now lives at <repo-root>/listener/telegram_listener.py.
# Walk up TWO levels to reach the repo root where all runtime data files
# (signals.txt, state, quarantine, session, config) live. This matches the
# project's documented layout: listener-private files sit alongside the
# shared signals.txt at repo root, so `xauusd_trading.cli auto` and the
# listener read/write the same paths regardless of which CWD either was
# launched from.
#
# If this script is ever moved deeper (or shallower), update the .parent
# chain accordingly; the rest of the module uses REPO_ROOT exclusively.
REPO_ROOT = Path(__file__).resolve().parent.parent
SIGNALS_PATH = REPO_ROOT / "signals.txt"
STATE_PATH = REPO_ROOT / "telegram_state.json"
QUARANTINE_PATH = REPO_ROOT / "telegram_quarantine.txt"
CONFIG_PATH = REPO_ROOT / "listener_config.json"
SESSION_NAME = str(REPO_ROOT / "telegram")  # creates telegram.session in repo root

# VICTOR posts in GMT+7. This matches every existing section header in
# signals.txt. If the channel ever changes timezone, change this AND
# the existing signals.txt headers together.
SIGNAL_SOURCE_TZ_OFFSET = 7
SIGNAL_SOURCE_TZ = timezone(timedelta(hours=SIGNAL_SOURCE_TZ_OFFSET))

log = logging.getLogger("telegram_listener")


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

@dataclass
class Config:
    api_id: int
    api_hash: str
    channel_id: Optional[int] = None
    channel_title_pattern: str = "VICTOR"

    @classmethod
    def load(cls, path: Path) -> "Config":
        if not path.exists():
            sys.stderr.write(
                f"Missing {path}. Copy listener_config.example.json -> "
                f"{path.name} (in repo root) and fill api_id + api_hash.\n"
            )
            sys.exit(1)
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls(
            api_id=int(data["api_id"]),
            api_hash=str(data["api_hash"]),
            channel_id=data.get("channel_id"),
            channel_title_pattern=str(data.get("channel_title_pattern") or "VICTOR"),
        )


# ---------------------------------------------------------------------------
# Signal parsing (VICTOR's raw format -> structured fields)
# ---------------------------------------------------------------------------

# The single distinguishing marker for a NEW SIGNAL message in the VICTOR
# channel. Update messages, "Move SL to X", and commentary do NOT contain
# this combination. Confirmed across every sample in messages.html.
NEW_SIGNAL_MARKER = re.compile(
    r"\U0001F947\s*(?P<side>BUY|SELL)\s+XAUUSD",
    re.IGNORECASE | re.UNICODE,
    )

# Lenient field extractors. Run after _normalize_text(), so commas are still
# in place (they're handled by the num() converter).
_NUM = r"\d+(?:[.,]\d+)?"
RANGE_RE = re.compile(
    rf"(?:BUY|SELL)\s+XAUUSD\s+({_NUM})\s*-\s*({_NUM})",
    re.IGNORECASE,
)
SL_RE = re.compile(rf"SL\s+({_NUM})", re.IGNORECASE)
TP1_RE = re.compile(rf"TP\s*1\s+({_NUM})", re.IGNORECASE)
TP2_RE = re.compile(rf"TP\s*2\s+({_NUM})", re.IGNORECASE)
TP3_RE = re.compile(rf"TP\s*3\s+({_NUM})", re.IGNORECASE)

# Strict canonical signals.txt line, used to validate manually-injected
# corrections from Saved Messages. MUST match `signal._SIGNAL_RE` in
# xauusd_trading exactly, or the engine will reject injected lines.
STRICT_LINE_RE = re.compile(
    r"^\s*(?P<id>\d+)\.\s*"
    r"(?P<side>BUY|SELL)\s+XAUUSD\s+"
    r"(?P<r1>\d+(?:\.\d+)?)\s*-\s*(?P<r2>\d+(?:\.\d+)?)\s+"
    r"SL\s+(?P<sl>\d+(?:\.\d+)?)\s+"
    r"TP1\s+(?P<tp1>\d+(?:\.\d+)?)\s+"
    r"TP2\s+(?P<tp2>\d+(?:\.\d+)?)\s+"
    r"TP3\s+(?P<tp3>\d+(?:\.\d+)?)\s+"
    r"(?P<time>\d{1,2}:\d{2}\s*[AP]M)\s*$",
    re.IGNORECASE,
)


@dataclass
class ParsedSignal:
    side: str
    r1: float
    r2: float
    sl: float
    tp1: float
    tp2: float
    tp3: float

    def to_line(self, day_index: int, time_text: str) -> str:
        return (
            f"{day_index}. {self.side} XAUUSD "
            f"{_fmt_price(self.r1)} - {_fmt_price(self.r2)} "
            f"SL {_fmt_price(self.sl)} "
            f"TP1 {_fmt_price(self.tp1)} "
            f"TP2 {_fmt_price(self.tp2)} "
            f"TP3 {_fmt_price(self.tp3)} "
            f"{time_text}"
        )


def _fmt_price(x: float) -> str:
    """Render a price the way signals.txt does: '4700' for integers, '4707.50'
    for fractional. Two decimals are preserved (matches existing entries)."""
    if x == int(x):
        return str(int(x))
    return f"{x:.2f}"


def _normalize_text(text: str) -> str:
    """Safe normalizations: unicode dashes -> ASCII, NBSP -> space."""
    for dash in "\u2013\u2014\u2212":  # en-dash, em-dash, minus sign
        text = text.replace(dash, "-")
    text = text.replace("\u00a0", " ")
    return text


def parse_victor_signal(raw_text: str) -> Optional[ParsedSignal]:
    """Return a ParsedSignal if `raw_text` is a NEW VICTOR signal, else None.

    - None  -> the message isn't a new signal (update, commentary, etc.) and
              should be silently ignored.
    - Raises ValueError -> the message LOOKS like a new signal (has the 🥇
              marker) but at least one required field couldn't be extracted.
              Caller treats this as a parse failure: quarantine + notify.
    """
    if not NEW_SIGNAL_MARKER.search(raw_text):
        return None

    text = _normalize_text(raw_text)

    side_m = NEW_SIGNAL_MARKER.search(text)
    side = side_m.group("side").upper()  # type: ignore[union-attr]

    range_m = RANGE_RE.search(text)
    sl_m = SL_RE.search(text)
    tp1_m = TP1_RE.search(text)
    tp2_m = TP2_RE.search(text)
    tp3_m = TP3_RE.search(text)

    missing = []
    if not range_m: missing.append("entry range")
    if not sl_m: missing.append("SL")
    if not tp1_m: missing.append("TP1")
    if not tp2_m: missing.append("TP2")
    if not tp3_m: missing.append("TP3")
    if missing:
        raise ValueError(f"Missing fields: {', '.join(missing)}")

    def num(s: str) -> float:
        return float(s.replace(",", "."))

    return ParsedSignal(
        side=side,
        r1=num(range_m.group(1)),  # type: ignore[union-attr]
        r2=num(range_m.group(2)),  # type: ignore[union-attr]
        sl=num(sl_m.group(1)),     # type: ignore[union-attr]
        tp1=num(tp1_m.group(1)),   # type: ignore[union-attr]
        tp2=num(tp2_m.group(1)),   # type: ignore[union-attr]
        tp3=num(tp3_m.group(1)),   # type: ignore[union-attr]
    )


def _format_time(dt: datetime) -> str:
    """Render time the way signals.txt does: '10:44 AM', '1:01 PM' (no
    leading zero on single-digit hours)."""
    t = dt.strftime("%I:%M %p")
    if t.startswith("0"):
        t = t[1:]
    return t


# ---------------------------------------------------------------------------
# signals.txt I/O (with atomic writes)
# ---------------------------------------------------------------------------

DATE_HEADER_RE = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2})\s+GMT\s*(?P<sign>[+-])\s*(?P<offset>\d+)\s*$"
)
SIGNAL_LINE_RE = re.compile(r"^\s*(?P<id>\d+)\.\s+")


def _read_signals_lines() -> list[str]:
    if not SIGNALS_PATH.exists():
        return []
    return SIGNALS_PATH.read_text(encoding="utf-8").splitlines()


def _atomic_write_lines(lines: list[str]) -> None:
    """Write atomically via temp file + os.replace. Guarantees readers
    (xauusd auto) never see a half-written file."""
    tmp = SIGNALS_PATH.with_suffix(SIGNALS_PATH.suffix + ".tmp")
    content = "\n".join(lines).rstrip() + "\n"
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, SIGNALS_PATH)


def _find_section(lines: list[str], date_str: str) -> Optional[int]:
    """Index of the date-header line for `date_str`, or None if absent."""
    for i, line in enumerate(lines):
        m = DATE_HEADER_RE.match(line)
        if m and m.group("date") == date_str:
            return i
    return None


def _section_signal_count(lines: list[str], header_idx: int) -> int:
    """Count signal lines under the section starting at `header_idx`."""
    count = 0
    for line in lines[header_idx + 1:]:
        if DATE_HEADER_RE.match(line):
            break  # next section
        if SIGNAL_LINE_RE.match(line):
            count += 1
    return count


class DuplicateSignalError(ValueError):
    """Raised by append_manual_signal when the content already exists in
    today's section. Distinct from a format-validation ValueError so the
    Saved Messages reply can be a friendlier 'already there' instead of '❌'."""

    def __init__(self, existing_line: str, existing_index: int):
        super().__init__(
            f"Already present as #{existing_index}: {existing_line}"
        )
        self.existing_line = existing_line
        self.existing_index = existing_index


def _find_matching_signal_in_section(
        lines: list[str], header_idx: int,
        side: str, r1: float, r2: float, sl: float,
        tp1: float, tp2: float, tp3: float, time_text: str,
) -> Optional[tuple[str, int]]:
    """Content-based dedup. Walk the section starting at `header_idx` and
    return (existing_line, existing_index) if a signal with the SAME side,
    SAME prices, AND SAME time already exists. Returns None otherwise.

    Acts as a defensive backup to state-based dedup so that even if
    telegram_state.json is deleted, corrupted, or out of sync, the listener
    won't re-write a signal that's already in signals.txt.

    Time is part of the comparison so a legitimate same-price signal posted
    at a different time of day is NOT treated as a duplicate.
    """
    norm_time = time_text.upper().replace(" ", "")
    for line in lines[header_idx + 1:]:
        if DATE_HEADER_RE.match(line):
            break
        m = STRICT_LINE_RE.match(line)
        if not m:
            continue
        if (m.group("side").upper() == side
                and float(m.group("r1")) == r1
                and float(m.group("r2")) == r2
                and float(m.group("sl")) == sl
                and float(m.group("tp1")) == tp1
                and float(m.group("tp2")) == tp2
                and float(m.group("tp3")) == tp3
                and m.group("time").upper().replace(" ", "") == norm_time):
            return line.strip(), int(m.group("id"))
    return None


def _insert_into_section(
        lines: list[str], header_idx: int, signal_line: str,
) -> list[str]:
    """Insert `signal_line` at the end of the section starting at
    `header_idx`, before any trailing blank lines."""
    end = len(lines)
    for j in range(header_idx + 1, len(lines)):
        if DATE_HEADER_RE.match(lines[j]):
            end = j
            break
    # Trim trailing blanks within the section.
    insert_at = end
    while insert_at > header_idx + 1 and lines[insert_at - 1].strip() == "":
        insert_at -= 1
    return lines[:insert_at] + [signal_line] + lines[insert_at:]


def _append_new_section(
        lines: list[str], date_str: str, tz_offset: int, signal_line: str,
) -> list[str]:
    """Append a new date section at the end of the file."""
    while lines and lines[-1].strip() == "":
        lines.pop()
    if lines:
        lines.append("")  # blank line separator between sections
    tz_label = f"GMT+{tz_offset}" if tz_offset >= 0 else f"GMT{tz_offset}"
    lines.append(f"{date_str} {tz_label}")
    lines.append(signal_line)
    return lines


def write_signal_to_file(
        parsed: ParsedSignal, signal_dt_gmt7: datetime,
) -> tuple[str, int, bool]:
    """Append a parsed VICTOR signal to signals.txt.

    Returns (signal_line, day_index, was_duplicate).
    When was_duplicate is True, no write happened; (signal_line, day_index)
    refer to the existing entry that matched. The caller should still update
    state to 'written' for that message_id so future events skip it cleanly.
    """
    date_str = signal_dt_gmt7.strftime("%Y-%m-%d")
    time_text = _format_time(signal_dt_gmt7)

    lines = _read_signals_lines()
    header_idx = _find_section(lines, date_str)

    # Content-based dedup: defensive backup to state-based dedup. Catches
    # the case where state was lost/reset but the signal is already in file.
    if header_idx is not None:
        match = _find_matching_signal_in_section(
            lines, header_idx,
            parsed.side, parsed.r1, parsed.r2, parsed.sl,
            parsed.tp1, parsed.tp2, parsed.tp3, time_text,
        )
        if match is not None:
            existing_line, existing_idx = match
            return existing_line, existing_idx, True

    if header_idx is None:
        day_index = 1
        signal_line = parsed.to_line(day_index, time_text)
        lines = _append_new_section(
            lines, date_str, SIGNAL_SOURCE_TZ_OFFSET, signal_line,
        )
    else:
        day_index = _section_signal_count(lines, header_idx) + 1
        signal_line = parsed.to_line(day_index, time_text)
        lines = _insert_into_section(lines, header_idx, signal_line)

    _atomic_write_lines(lines)
    return signal_line, day_index, False


def append_manual_signal(line: str) -> tuple[str, int]:
    """Append a strict-canonical signal line (from Saved Messages) to
    signals.txt. The day-index in `line` is IGNORED and replaced with the
    next free index in today's section, so it's impossible to accidentally
    double-write `5.` when `5.` already exists.

    Returns (rendered_line, day_index_used).
    Raises:
      ValueError              -- the line doesn't match the canonical format.
      DuplicateSignalError    -- an identical signal (same side, prices, time)
                                 is already in today's section.
    """
    m = STRICT_LINE_RE.match(line.strip())
    if not m:
        raise ValueError(
            "Line does not match the canonical format. Expected: "
            "`N. BUY|SELL XAUUSD R1 - R2 SL S TP1 T1 TP2 T2 TP3 T3 HH:MM AM|PM`"
        )

    now_gmt7 = datetime.utcnow() + timedelta(hours=SIGNAL_SOURCE_TZ_OFFSET)
    date_str = now_gmt7.strftime("%Y-%m-%d")

    lines = _read_signals_lines()
    header_idx = _find_section(lines, date_str)

    # Content-based dedup. Same rule as auto-write path: if a signal with
    # the same side, prices, and time already exists, don't add a second.
    if header_idx is not None:
        match = _find_matching_signal_in_section(
            lines, header_idx,
            m.group("side").upper(),
            float(m.group("r1")), float(m.group("r2")),
            float(m.group("sl")),
            float(m.group("tp1")), float(m.group("tp2")), float(m.group("tp3")),
            m.group("time"),
        )
        if match is not None:
            raise DuplicateSignalError(match[0], match[1])

    new_index = (
        1 if header_idx is None
        else _section_signal_count(lines, header_idx) + 1
    )

    # Re-render the user's line with the corrected index. Keep everything
    # after the `N.` as-is so user's chosen prices/time stand.
    _, rest = line.split(".", 1)
    renumbered = f"{new_index}.{rest}".strip()
    if not STRICT_LINE_RE.match(renumbered):
        raise ValueError("Line is unparseable after renumbering -- nothing was written.")

    if header_idx is None:
        lines = _append_new_section(
            lines, date_str, SIGNAL_SOURCE_TZ_OFFSET, renumbered,
        )
    else:
        lines = _insert_into_section(lines, header_idx, renumbered)
    _atomic_write_lines(lines)
    return renumbered, new_index


def next_day_index(date_str: str) -> int:
    lines = _read_signals_lines()
    header_idx = _find_section(lines, date_str)
    if header_idx is None:
        return 1
    return _section_signal_count(lines, header_idx) + 1


# ---------------------------------------------------------------------------
# State (dedup + edit handling)
# ---------------------------------------------------------------------------

def load_state() -> dict:
    if not STATE_PATH.exists():
        return {"messages": {}, "last_processed_message_id": 0}
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        log.warning("State file corrupt; starting fresh.")
        return {"messages": {}, "last_processed_message_id": 0}


def save_state(state: dict) -> None:
    tmp = STATE_PATH.with_suffix(STATE_PATH.suffix + ".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    os.replace(tmp, STATE_PATH)


def state_get(state: dict, message_id: int) -> Optional[dict]:
    return state["messages"].get(str(message_id))


def state_set(state: dict, message_id: int, record: dict) -> None:
    state["messages"][str(message_id)] = record
    if message_id > state.get("last_processed_message_id", 0):
        state["last_processed_message_id"] = message_id


# ---------------------------------------------------------------------------
# Quarantine
# ---------------------------------------------------------------------------

def quarantine(message_id: int, raw_text: str, reason: str, dt_utc: datetime) -> None:
    """Append a parse-failure entry to telegram_quarantine.txt."""
    block = (
        f"=== {dt_utc.isoformat()}  message_id={message_id}  reason={reason} ===\n"
        f"{raw_text}\n\n"
    )
    with open(QUARANTINE_PATH, "a", encoding="utf-8") as f:
        f.write(block)


# ---------------------------------------------------------------------------
# Listener
# ---------------------------------------------------------------------------

class Listener:
    def __init__(self, cfg: Config, *, dry_run: bool = False):
        self.cfg = cfg
        self.dry_run = dry_run
        self.client = TelegramClient(SESSION_NAME, cfg.api_id, cfg.api_hash)
        self.state = load_state()
        self.channel_id: Optional[int] = cfg.channel_id
        self.saved_id: Optional[int] = None  # filled at setup time
        self._lock = asyncio.Lock()  # serialises file I/O between handlers

    async def _resolve_channel(self) -> int:
        """Find the channel id from config or title-substring match."""
        if self.channel_id is not None:
            try:
                entity = await self.client.get_entity(self.channel_id)
                title = getattr(entity, "title", "?")
                log.info(f"Listening on channel id={self.channel_id} title={title!r}")
                return self.channel_id
            except Exception as e:
                log.warning(f"channel_id={self.channel_id} couldn't be resolved ({e}); "
                            f"falling back to title-substring match.")

        pattern = self.cfg.channel_title_pattern.upper()
        matches = []
        async for dialog in self.client.iter_dialogs():
            title = (dialog.title or "").upper()
            if pattern in title:
                matches.append(dialog)
        if not matches:
            raise RuntimeError(
                f"No chat title contains {self.cfg.channel_title_pattern!r}. "
                f"Run `python listener\\telegram_listener.py list-chats` and "
                f"put the numeric id into listener_config.json (repo root) "
                f"under `channel_id`."
            )
        if len(matches) > 1:
            titles = ", ".join(f"{d.id}={d.title!r}" for d in matches)
            raise RuntimeError(
                f"Multiple chats match {self.cfg.channel_title_pattern!r}: {titles}. "
                f"Disambiguate by setting `channel_id` in listener_config.json."
            )
        d = matches[0]
        log.info(f"Matched channel by title: id={d.id} title={d.title!r}")
        return d.id

    async def setup(self) -> None:
        await self.client.start()
        me = await self.client.get_me()
        log.info(f"Logged in as {me.first_name!r} (id={me.id})")
        self.saved_id = me.id
        self.channel_id = await self._resolve_channel()

        # Event handlers: channel new, channel edit, Saved Messages new.
        self.client.add_event_handler(
            self._on_channel_new,
            events.NewMessage(chats=self.channel_id),
        )
        self.client.add_event_handler(
            self._on_channel_edit,
            events.MessageEdited(chats=self.channel_id),
        )
        # Saved Messages: chat is yourself, sender is yourself.
        self.client.add_event_handler(
            self._on_saved,
            events.NewMessage(chats=self.saved_id, from_users=self.saved_id),
        )

    async def catch_up(self, lookback_hours: int = 24) -> None:
        """On startup, process any channel messages that arrived while the
        listener was down. Walks back until we hit a message we've already
        seen or `lookback_hours` of history (whichever comes first)."""
        last_id = self.state.get("last_processed_message_id", 0)
        cutoff_dt = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
        log.info(f"Catch-up: looking for channel messages with id > {last_id} "
                 f"(or up to {lookback_hours}h ago)")
        new_msgs = []
        async for msg in self.client.iter_messages(self.channel_id, limit=500):
            if msg.id <= last_id:
                break
            if msg.date < cutoff_dt:
                break
            new_msgs.append(msg)
        # iter_messages returns newest first; flip to chronological.
        for msg in reversed(new_msgs):
            await self._process_message(msg, is_edit=False)
        log.info(f"Catch-up done ({len(new_msgs)} messages scanned)")

    async def _on_channel_new(self, event) -> None:
        await self._process_message(event.message, is_edit=False)

    async def _on_channel_edit(self, event) -> None:
        await self._process_message(event.message, is_edit=True)

    async def _on_saved(self, event) -> None:
        text = (event.message.message or "").strip()
        if not text:
            return
        # Only react to messages that look like canonical signal lines.
        # Everything else in Saved Messages (notes to self, the listener's
        # own warning messages, suggestion templates) is ignored.
        if not STRICT_LINE_RE.match(text):
            return
        async with self._lock:
            try:
                if self.dry_run:
                    log.info(f"[dry-run] would inject manual signal: {text}")
                    await self._reply_saved(f"✅ [dry-run] would inject: `{text}`")
                else:
                    rendered, idx = append_manual_signal(text)
                    log.info(f"Manual injection #{idx}: {rendered}")
                    await self._reply_saved(
                        f"✅ Injected as #{idx} in today's section:\n`{rendered}`"
                    )
            except DuplicateSignalError as e:
                log.info(
                    f"Manual injection is a duplicate: matches #{e.existing_index} "
                    f"({e.existing_line})"
                )
                await self._reply_saved(
                    f"ℹ️ Already in signals.txt as #{e.existing_index}:\n"
                    f"`{e.existing_line}`"
                )
            except ValueError as e:
                log.warning(f"Manual injection rejected: {e}")
                await self._reply_saved(f"❌ {e}")
            except Exception as e:
                log.error(f"Manual injection unexpected error: {e}")
                await self._reply_saved(f"❌ Unexpected error: {e}")

    async def _process_message(self, msg, *, is_edit: bool) -> None:
        message_id = msg.id
        raw = msg.message or ""

        # Telethon gives us tz-aware UTC datetimes.
        if msg.date.tzinfo is None:
            msg_dt_utc = msg.date.replace(tzinfo=timezone.utc)
        else:
            msg_dt_utc = msg.date.astimezone(timezone.utc)
        msg_dt_gmt7 = msg_dt_utc.astimezone(SIGNAL_SOURCE_TZ).replace(tzinfo=None)

        async with self._lock:
            existing = state_get(self.state, message_id)

            # ------------------------------------------------------------------
            # Universal dedup. We bypass all parsing if state shows this
            # message was already handled successfully. This covers:
            #   * catch_up re-processing the same message_ids after a restart
            #   * Telegram redelivering an event after a reconnect
            #   * the user accidentally starting two listener processes
            #   * any other path that double-fires for the same message_id
            # The previous version only ran this check for is_edit=True, which
            # let new-message events from catch_up sneak past and write
            # duplicates with fresh day-indices.
            # ------------------------------------------------------------------
            if existing and existing.get("status") == "written":
                log.debug(
                    f"Message {message_id} already written as "
                    f"{existing.get('signal_key')}; ignoring "
                    f"({'edit' if is_edit else 'new'} event)"
                )
                return

            # For previously-quarantined messages, only re-parse on EDIT
            # events (Victor might have fixed a typo). New-message events
            # are usually catch_up replays of the original; the user was
            # already notified once, so skip silently.
            if (existing
                    and existing.get("status") in ("quarantined", "write_failed")
                    and not is_edit):
                log.debug(
                    f"Catch-up saw previously-{existing.get('status')} "
                    f"message {message_id}; ignoring (user already notified)"
                )
                return

            if is_edit and existing:
                log.info(
                    f"Edit on previously-{existing.get('status')} "
                    f"message {message_id}: re-parsing"
                )

            try:
                parsed = parse_victor_signal(raw)
            except ValueError as e:
                log.warning(f"Parse failure on message {message_id}: {e}")
                state_set(self.state, message_id, {
                    "status": "quarantined",
                    "reason": str(e),
                    "raw": raw[:500],
                })
                save_state(self.state)
                if not self.dry_run:
                    quarantine(message_id, raw, str(e), msg_dt_utc)
                    await self._notify_failure(raw, str(e), msg_dt_gmt7)
                return

            if parsed is None:
                # Not a new signal -- update, "Move SL", commentary, etc.
                # Silent no-op; don't pollute state file.
                return

            try:
                if self.dry_run:
                    line_preview = parsed.to_line(
                        next_day_index(msg_dt_gmt7.strftime("%Y-%m-%d")),
                        _format_time(msg_dt_gmt7),
                    )
                    log.info(f"[dry-run] would append: {line_preview}")
                    state_set(self.state, message_id, {
                        "status": "dry-run", "line": line_preview,
                    })
                else:
                    signal_line, day_index, was_duplicate = write_signal_to_file(
                        parsed, msg_dt_gmt7,
                    )
                    signal_key = f"{msg_dt_gmt7:%Y-%m-%d}#{day_index:02d}"
                    if was_duplicate:
                        # Content-based dedup caught it. Record as 'written'
                        # pointing at the existing entry so we never look at
                        # this message_id again, and don't bother the user
                        # with a notification.
                        log.info(
                            f"Message {message_id} matches existing "
                            f"{signal_key}: {signal_line} -- skipping write"
                        )
                        state_set(self.state, message_id, {
                            "status": "written",
                            "signal_key": signal_key,
                            "line": signal_line,
                            "note": "matched existing entry by content",
                        })
                    else:
                        state_set(self.state, message_id, {
                            "status": "written",
                            "signal_key": signal_key,
                            "line": signal_line,
                        })
                        log.info(f"Wrote {signal_key}: {signal_line}")
                save_state(self.state)
            except Exception as e:
                log.error(f"Write failed on message {message_id}: {e}")
                state_set(self.state, message_id, {
                    "status": "write_failed",
                    "reason": str(e),
                    "raw": raw[:500],
                })
                save_state(self.state)
                if not self.dry_run:
                    await self._notify_failure(
                        raw, f"Write failed: {e}", msg_dt_gmt7,
                    )

    async def _notify_failure(
            self, raw: str, reason: str, msg_dt_gmt7: datetime,
    ) -> None:
        """Post a Saved Messages note with a pre-filled correction template."""
        date_str = msg_dt_gmt7.strftime("%Y-%m-%d")
        time_text = _format_time(msg_dt_gmt7)
        idx = next_day_index(date_str)
        suggestion = (
            f"{idx}. BUY XAUUSD R1 - R2 SL S TP1 T1 TP2 T2 TP3 T3 {time_text}"
        )
        truncated = raw[:800] + ("..." if len(raw) > 800 else "")
        notification = (
            f"⚠️ Couldn't parse a VICTOR message ({reason}).\n"
            f"\nRaw:\n```\n{truncated}\n```\n"
            f"\nReply with the corrected line in this format "
            f"(replace `BUY` with `SELL` if needed):\n"
            f"`{suggestion}`"
        )
        try:
            await self.client.send_message(self.saved_id, notification)
        except Exception as e:
            log.warning(f"Saved Messages notification failed: {e}")

    async def _reply_saved(self, text: str) -> None:
        try:
            await self.client.send_message(self.saved_id, text)
        except Exception as e:
            log.warning(f"Saved Messages reply failed: {e}")

    async def run(self) -> None:
        await self.setup()
        await self.catch_up()
        log.info("Listening. Press Ctrl+C to stop.")
        await self.client.run_until_disconnected()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

async def _cmd_list_chats(cfg: Config) -> int:
    client = TelegramClient(SESSION_NAME, cfg.api_id, cfg.api_hash)
    await client.start()
    print(f"{'ID':>20}  {'TYPE':<10} TITLE")
    print(f"{'-' * 20}  {'-' * 10} {'-' * 50}")
    async for dialog in client.iter_dialogs():
        if dialog.is_channel:
            kind = "channel"
        elif dialog.is_group:
            kind = "group"
        else:
            kind = "user"
        title = (dialog.title or "(no title)")
        print(f"{dialog.id:>20}  {kind:<10} {title!r}")
    await client.disconnect()
    print()
    print("Copy the ID of the VICTOR channel into listener_config.json "
          "(repo root) under `channel_id`, then run "
          "`python listener\\telegram_listener.py`.")
    return 0


async def _cmd_listen(cfg: Config, dry_run: bool) -> int:
    listener = Listener(cfg, dry_run=dry_run)
    try:
        await listener.run()
    except KeyboardInterrupt:
        log.info("Interrupted; exiting.")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Telegram listener for VICTOR - GOLD PRIORITY signals.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples (run from repo root):\n"
            "  python listener\\telegram_listener.py list-chats    # find channel id\n"
            "  python listener\\telegram_listener.py               # start listening\n"
            "  python listener\\telegram_listener.py --dry-run     # parse but don't write\n"
        ),
    )
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("list-chats", help="Print every chat with its numeric id.")
    sub.add_parser("listen", help="Start the listener (the default).")
    p.add_argument(
        "--dry-run", action="store_true",
        help="Parse messages but don't write signals.txt or send notifications.",
    )
    return p


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    args = _build_parser().parse_args()
    cfg = Config.load(CONFIG_PATH)

    if args.cmd == "list-chats":
        return asyncio.run(_cmd_list_chats(cfg))
    return asyncio.run(_cmd_listen(cfg, dry_run=args.dry_run))


if __name__ == "__main__":
    raise SystemExit(main())