"""telegram_listener.py — VICTOR - GOLD PRIORITY signal listener.

Watches one Telegram channel for new XAUUSD signals, parses them, and
appends to `signals.txt` in the format `xauusd_trading` expects. Runs as
a separate process from `xauusd_trading.cli auto`; the two communicate
only through `signals.txt` (atomically written so `auto` never reads a
half-written file).

Files (all at repo root, not in listener/):
    signals.txt              read + append (atomic via os.replace)
    telegram_state.json      message_id -> parsed status; dedup + edit handling
    telegram_quarantine.txt  raw text of every unparseable message
    listener_config.json     api_id, api_hash, channel_id
    telegram.session         Telethon auth token (created on first run)

Setup and corrections workflow live in docs/MT5_SETUP.md.
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
from typing import Callable, Optional

try:
    from telethon import TelegramClient, events
except ImportError:
    sys.stderr.write(
        "telethon not installed. In your xauusd env, run:\n"
        "    pip install telethon\n"
    )
    sys.exit(1)

# ---------------------------------------------------------------------------
# paths and constants
# ---------------------------------------------------------------------------

# Script lives at <repo-root>/listener/telegram_listener.py — walk up two
# levels to find the repo root where all runtime files live. The rest of
# the module uses REPO_ROOT exclusively, so CWD doesn't matter.
REPO_ROOT = Path(__file__).resolve().parent.parent
SIGNALS_PATH = REPO_ROOT / "signals.txt"
STATE_PATH = REPO_ROOT / "telegram_state.json"
QUARANTINE_PATH = REPO_ROOT / "telegram_quarantine.txt"
CONFIG_PATH = REPO_ROOT / "listener_config.json"
SESSION_NAME = str(REPO_ROOT / "telegram")

# Engine -> listener event bridge. `xauusd_trading.cli auto` appends one JSON
# object per line to this file (see xauusd_trading/notifications.py); the
# listener tails it and forwards each event's pre-rendered `text` to Saved
# Messages. The offset sidecar lets a restart resume mid-file without
# replaying the backlog or skipping events.
NOTIFICATIONS_PATH = REPO_ROOT / "notifications.jsonl"
NOTIFICATIONS_POLL_SECONDS = 2.0
# Space out a burst of events so a flurry of closures can't trip Telegram's
# per-chat flood limit.
NOTIFICATIONS_SEND_GAP_SECONDS = 0.5

# VICTOR posts in GMT+7. Must match the existing section headers in
# signals.txt; if the channel ever changes timezone, change both together.
SIGNAL_SOURCE_TZ_OFFSET = 7
SIGNAL_SOURCE_TZ = timezone(timedelta(hours=SIGNAL_SOURCE_TZ_OFFSET))

log = logging.getLogger("telegram_listener")


# ---------------------------------------------------------------------------
# configuration
# ---------------------------------------------------------------------------

@dataclass
class Config:
    api_id: int
    api_hash: str
    channel_id: Optional[int] = None
    channel_title_pattern: str = "VICTOR"
    notifications_path: Optional[str] = None

    @classmethod
    def load(cls, path: Path) -> "Config":
        if not path.exists():
            sys.stderr.write(
                f"Missing {path}. Copy listener_config.example.json -> "
                f"{path.name} (in repo root) and fill api_id + api_hash.\n"
            )
            sys.exit(1)
        data = json.loads(path.read_text(encoding="utf-8"))
        raw_channel_id = data.get("channel_id")
        channel_id = int(raw_channel_id) if raw_channel_id is not None else None
        return cls(
            api_id=int(data["api_id"]),
            api_hash=str(data["api_hash"]),
            channel_id=channel_id,
            channel_title_pattern=str(data.get("channel_title_pattern") or "VICTOR"),
            notifications_path=(str(data["notifications"]) if data.get("notifications") else None),
        )


# ---------------------------------------------------------------------------
# signal parsing (VICTOR's raw format -> structured fields)
# ---------------------------------------------------------------------------

# Distinguishing marker for a NEW SIGNAL message. Update messages,
# "Move SL to X", and commentary do NOT contain this combination.
NEW_SIGNAL_MARKER = re.compile(
    r"\U0001F947\s*(?P<side>BUY|SELL)\s+XAUUSD",
    re.IGNORECASE | re.UNICODE,
)

# Lenient field extractors. Run after _normalize_text(), so commas are
# still in place — handled by the num() converter.
_NUM = r"\d+(?:[.,]\d+)?"
RANGE_RE = re.compile(
    rf"(?:BUY|SELL)\s+XAUUSD\s+({_NUM})\s*-\s*({_NUM})",
    re.IGNORECASE,
)
SL_RE = re.compile(rf"SL\s+({_NUM})", re.IGNORECASE)
TP1_RE = re.compile(rf"TP\s*1\s+({_NUM})", re.IGNORECASE)
TP2_RE = re.compile(rf"TP\s*2\s+({_NUM})", re.IGNORECASE)
TP3_RE = re.compile(rf"TP\s*3\s+({_NUM})", re.IGNORECASE)

# Strict canonical signals.txt line — validates manually-injected corrections
# from Saved Messages. MUST match `signal._SIGNAL_RE` in xauusd_trading exactly.
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
    """Match signals.txt formatting: '4700' for integers, '4707.50' for fractional."""
    if x == int(x):
        return str(int(x))
    return f"{x:.2f}"


def _normalize_text(text: str) -> str:
    """Unicode dashes -> ASCII, NBSP -> space."""
    for dash in "\u2013\u2014\u2212":  # en-dash, em-dash, minus sign
        text = text.replace(dash, "-")
    text = text.replace("\u00a0", " ")
    return text


def parse_victor_signal(raw_text: str) -> Optional[ParsedSignal]:
    """Return a ParsedSignal if `raw_text` is a NEW VICTOR signal, else None.

    - None  -> not a new signal (update, commentary, etc); silently ignore.
    - Raises ValueError -> message looks like a new signal (has the 🥇
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
    if not range_m:
        missing.append("entry range")
    if not sl_m:
        missing.append("SL")
    if not tp1_m:
        missing.append("TP1")
    if not tp2_m:
        missing.append("TP2")
    if not tp3_m:
        missing.append("TP3")
    if missing:
        raise ValueError(f"Missing fields: {', '.join(missing)}")

    def num(s: str) -> float:
        return float(s.replace(",", "."))

    return ParsedSignal(
        side=side,
        r1=num(range_m.group(1)),  # type: ignore[union-attr]
        r2=num(range_m.group(2)),  # type: ignore[union-attr]
        sl=num(sl_m.group(1)),  # type: ignore[union-attr]
        tp1=num(tp1_m.group(1)),  # type: ignore[union-attr]
        tp2=num(tp2_m.group(1)),  # type: ignore[union-attr]
        tp3=num(tp3_m.group(1)),  # type: ignore[union-attr]
    )


# ---------------------------------------------------------------------------
# signal sanity auto-correction + RR classification
# ---------------------------------------------------------------------------
#
# The listener must NOT rewrite signals just to improve risk:reward.
#
# Correct only impossible/obvious typo cases:
#   - SL on the wrong side of the range
#   - TP on the wrong side of the range
#   - TP order inconsistent with BUY/SELL direction
#   - extra-zero / wrong-hundreds typo, e.g. 47802 -> 4802
#   - range typo only when it makes SL/TP structurally impossible
#
# After correction, classify the signal using the BEST laddered entry:
#   BUY  best entry = lowest entry
#   SELL best entry = highest entry
#
#   GOOD_TP1_RR if TP1 reward from best entry >= risk to SL
#   LOW_TP1_RR  otherwise
#
# LOW_TP1_RR is a warning/filter for backtesting. It is not an auto-correction.

_CORRECTION_TOL = 1e-6
_EXPECTED_RANGE_WIDTH = 2.0
_MIN_TP1_RR_BEST_ENTRY = 1.0

_LEVEL_STEPS = (
    -500.0, -400.0, -300.0, -200.0, -100.0,
    -50.0, -40.0, -30.0, -20.0, -15.0, -10.0,
    -7.5, -5.0,
    5.0, 7.5, 10.0, 15.0, 20.0, 30.0, 40.0, 50.0,
    100.0, 200.0, 300.0, 400.0, 500.0,
)
_RANGE_SHIFT_STEPS = (-500.0, -400.0, -300.0, -200.0, -100.0, 0.0, 100.0, 200.0, 300.0, 400.0, 500.0)


@dataclass
class GeometryFix:
    """Result of signal sanity correction and RR classification."""
    corrected: ParsedSignal
    changes: list[str]
    rr_bucket: str = "UNKNOWN_RR"
    tp1_rr_best_entry: Optional[float] = None

    @property
    def changed(self) -> bool:
        return bool(self.changes)

    @property
    def is_low_tp1_rr(self) -> bool:
        return self.rr_bucket == "LOW_TP1_RR"


def _price_key(x: float) -> str:
    return f"{round(float(x), 2):.2f}"


def _dedupe_prices(values: list[float]) -> list[float]:
    out: list[float] = []
    seen: set[str] = set()
    for value in values:
        value = round(float(value), 2)
        if value <= 0:
            continue
        key = _price_key(value)
        if key not in seen:
            seen.add(key)
            out.append(value)
    return out


def _range_bounds(r1: float, r2: float) -> tuple[float, float]:
    return max(r1, r2), min(r1, r2)


def _range_width_ok(r1: float, r2: float) -> bool:
    return abs(abs(r1 - r2) - _EXPECTED_RANGE_WIDTH) <= _CORRECTION_TOL


def _entry_levels(side: str, r1: float, r2: float) -> list[float]:
    high, low = _range_bounds(r1, r2)
    if side == "BUY":
        return [high, high - 1.0, low]
    return [low, low + 1.0, high]


def _best_ladder_entry(side: str, r1: float, r2: float) -> float:
    entries = _entry_levels(side, r1, r2)
    if side == "BUY":
        return min(entries)
    return max(entries)


def _tp_order_ok(side: str, r1: float, r2: float, tp1: float, tp2: float, tp3: float) -> bool:
    high, low = _range_bounds(r1, r2)
    if side == "BUY":
        return tp1 > high and tp2 > tp1 and tp3 > tp2
    return tp1 < low and tp2 < tp1 and tp3 < tp2


def _sl_side_ok(side: str, r1: float, r2: float, sl: float) -> bool:
    high, low = _range_bounds(r1, r2)
    if side == "BUY":
        return sl < low
    return sl > high


def _structural_ok(side: str, r1: float, r2: float, sl: float, tp1: float, tp2: float, tp3: float) -> bool:
    return _sl_side_ok(side, r1, r2, sl) and _tp_order_ok(side, r1, r2, tp1, tp2, tp3)


def _logic_error_count(side: str, r1: float, r2: float, sl: float, tp1: float, tp2: float, tp3: float) -> int:
    """Lower is better. RR is intentionally NOT part of correction scoring."""
    errors = 0
    if not _range_width_ok(r1, r2):
        # Width 3 may be valid historically, so keep it as a weak penalty only.
        errors += 1 if abs(abs(r1 - r2) - 3.0) <= _CORRECTION_TOL else 20
    if not _sl_side_ok(side, r1, r2, sl):
        errors += 30
    if not _tp_order_ok(side, r1, r2, tp1, tp2, tp3):
        errors += 30
    return errors


def _candidate_prices(current: float, anchor: float) -> list[float]:
    """Generate likely typo repairs around a field."""
    values: list[float] = [current]
    values.extend(current + step for step in _LEVEL_STEPS)

    if abs(current - round(current)) <= _CORRECTION_TOL:
        s = str(int(round(abs(current))))
        if len(s) >= 5:
            for i in range(len(s)):
                repaired = s[:i] + s[i + 1:]
                if repaired:
                    values.append(float(repaired))

        suffix = int(round(abs(current))) % 100
        base = int(anchor // 100) * 100
        for b in range(base - 600, base + 601, 100):
            values.append(float(b + suffix))

    return _dedupe_prices(values)


def _choose_price(current: float, candidates: list[float], predicate: Callable[[float], bool], anchor: float) -> float:
    valid = [c for c in candidates if predicate(c)]
    if not valid:
        return current
    return min(valid, key=lambda c: (abs(c - current), abs(c - anchor)))


def _choose_range(parsed: ParsedSignal) -> tuple[float, float]:
    """Correct range only when the original makes SL/TP structurally impossible.

    Historical VICTOR signals sometimes have width 3. Do not force those to
    width 2 if SL/TP direction is already valid.
    """
    side = parsed.side.upper()
    if _structural_ok(side, parsed.r1, parsed.r2, parsed.sl, parsed.tp1, parsed.tp2, parsed.tp3):
        return parsed.r1, parsed.r2

    candidates: list[tuple[float, float]] = []

    def add(r1: float, r2: float) -> None:
        pair = (round(r1, 2), round(r2, 2))
        if pair not in candidates:
            candidates.append(pair)

    add(parsed.r1, parsed.r2)
    for shift in _RANGE_SHIFT_STEPS:
        sr1 = parsed.r1 + shift
        sr2 = parsed.r2 + shift
        add(sr1, sr2)
        if side == "BUY":
            add(sr1, sr1 - 2.0)
            add(sr1, sr1 + 2.0)
        else:
            add(sr1, sr1 + 2.0)
            add(sr1, sr1 - 2.0)

    def score(pair: tuple[float, float]) -> tuple[int, float]:
        r1, r2 = pair
        errors = _logic_error_count(side, r1, r2, parsed.sl, parsed.tp1, parsed.tp2, parsed.tp3)
        movement = abs(r1 - parsed.r1) + abs(r2 - parsed.r2)
        return errors, movement

    return min(candidates, key=score)


def _tp3_outlier(side: str, tp1: float, tp2: float, tp3: float) -> bool:
    """Flag likely wrong-hundreds TP3 typos that are directionally valid."""
    normal_step = max(abs(tp2 - tp1), 1.0)
    max_expected_step = max(50.0, normal_step * 4.0)
    if side == "BUY":
        return (tp3 - tp2) > max_expected_step
    return (tp2 - tp3) > max_expected_step


def _fix_tp_levels(side: str, r1: float, r2: float, sl: float, tp1: float, tp2: float, tp3: float) -> tuple[float, float, float]:
    high, low = _range_bounds(r1, r2)
    anchor = (r1 + r2) / 2.0

    if side == "BUY":
        if not (tp1 > high and tp1 < tp2):
            tp1 = _choose_price(tp1, _candidate_prices(tp1, anchor), lambda c: c > high and c < tp2, anchor)
        if not (tp2 > tp1 and tp2 < tp3):
            tp2 = _choose_price(tp2, _candidate_prices(tp2, anchor), lambda c: c > tp1 and c < tp3, anchor)
        if not (tp3 > tp2) or _tp3_outlier(side, tp1, tp2, tp3):
            tp3_candidates = _candidate_prices(tp3, anchor)
            if _tp3_outlier(side, tp1, tp2, tp3):
                tp3_candidates = [c for c in tp3_candidates if abs(c - tp3) > _CORRECTION_TOL]
            tp3 = _choose_price(tp3, tp3_candidates, lambda c: c > tp2, anchor)
    else:
        if not (tp1 < low and tp1 > tp2):
            tp1 = _choose_price(tp1, _candidate_prices(tp1, anchor), lambda c: c < low and c > tp2, anchor)
        if not (tp2 < tp1 and tp2 > tp3):
            tp2 = _choose_price(tp2, _candidate_prices(tp2, anchor), lambda c: c < tp1 and c > tp3, anchor)
        if not (tp3 < tp2) or _tp3_outlier(side, tp1, tp2, tp3):
            tp3_candidates = _candidate_prices(tp3, anchor)
            if _tp3_outlier(side, tp1, tp2, tp3):
                tp3_candidates = [c for c in tp3_candidates if abs(c - tp3) > _CORRECTION_TOL]
            tp3 = _choose_price(tp3, tp3_candidates, lambda c: c < tp2, anchor)

    return tp1, tp2, tp3


def _fix_sl_level(side: str, r1: float, r2: float, sl: float) -> float:
    """Fix SL only when it is on the wrong side. RR never moves SL."""
    high, low = _range_bounds(r1, r2)
    anchor = (r1 + r2) / 2.0
    candidates = _candidate_prices(sl, anchor)

    if side == "BUY":
        return _choose_price(sl, candidates, lambda c: c < low, anchor)
    return _choose_price(sl, candidates, lambda c: c > high, anchor)


def _classify_tp1_rr(parsed: ParsedSignal) -> tuple[str, Optional[float]]:
    side = parsed.side.upper()
    entry = _best_ladder_entry(side, parsed.r1, parsed.r2)

    if side == "BUY":
        risk = entry - parsed.sl
        reward = parsed.tp1 - entry
    else:
        risk = parsed.sl - entry
        reward = entry - parsed.tp1

    if risk <= 0 or reward <= 0:
        return "INVALID_RR", None

    rr = reward / risk
    if rr + _CORRECTION_TOL >= _MIN_TP1_RR_BEST_ENTRY:
        return "GOOD_TP1_RR", rr
    return "LOW_TP1_RR", rr


def _add_change(changes: list[str], field: str, old: float, new: float) -> None:
    if abs(old - new) > _CORRECTION_TOL:
        changes.append(f"{field}: {_fmt_price(old)} -> {_fmt_price(new)}")


def apply_signal_corrections(parsed: ParsedSignal) -> GeometryFix:
    """Apply logic-only correction, then classify RR.

    This intentionally does NOT tighten SL or change TP values just to improve
    risk:reward. LOW_TP1_RR is only a warning/filter for backtesting.
    """
    side = parsed.side.upper()
    changes: list[str] = []

    if side not in ("BUY", "SELL"):
        return GeometryFix(corrected=parsed, changes=[], rr_bucket="UNKNOWN_RR", tp1_rr_best_entry=None)

    new_r1, new_r2 = _choose_range(parsed)
    if abs(new_r1 - parsed.r1) > _CORRECTION_TOL or abs(new_r2 - parsed.r2) > _CORRECTION_TOL:
        changes.append(
            "Range: "
            f"{_fmt_price(parsed.r1)} - {_fmt_price(parsed.r2)} -> "
            f"{_fmt_price(new_r1)} - {_fmt_price(new_r2)}"
        )

    new_tp1, new_tp2, new_tp3 = _fix_tp_levels(side, new_r1, new_r2, parsed.sl, parsed.tp1, parsed.tp2, parsed.tp3)
    _add_change(changes, "TP1", parsed.tp1, new_tp1)
    _add_change(changes, "TP2", parsed.tp2, new_tp2)
    _add_change(changes, "TP3", parsed.tp3, new_tp3)

    new_sl = parsed.sl
    if not _sl_side_ok(side, new_r1, new_r2, new_sl):
        new_sl = _fix_sl_level(side, new_r1, new_r2, new_sl)
    _add_change(changes, "SL", parsed.sl, new_sl)

    corrected = ParsedSignal(side=side, r1=new_r1, r2=new_r2, sl=new_sl, tp1=new_tp1, tp2=new_tp2, tp3=new_tp3)
    rr_bucket, tp1_rr = _classify_tp1_rr(corrected)

    final_errors = _logic_error_count(
        side,
        corrected.r1,
        corrected.r2,
        corrected.sl,
        corrected.tp1,
        corrected.tp2,
        corrected.tp3,
    )
    if final_errors >= 30:
        changes.append("REVIEW: auto-correction could not fully validate range/SL/TP logic")

    return GeometryFix(corrected=corrected, changes=changes, rr_bucket=rr_bucket, tp1_rr_best_entry=tp1_rr)


def _format_time(dt: datetime) -> str:
    """Render time the way signals.txt does: '10:44 AM', '1:01 PM'."""
    t = dt.strftime("%I:%M %p")
    if t.startswith("0"):
        t = t[1:]
    return t


# ---------------------------------------------------------------------------
# signals.txt I/O (atomic writes)
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
    """Atomic via temp file + os.replace. Readers never see a half-written file."""
    tmp = SIGNALS_PATH.with_suffix(SIGNALS_PATH.suffix + ".tmp")
    content = "\n".join(lines).rstrip() + "\n"
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, SIGNALS_PATH)


def _find_section(lines: list[str], date_str: str) -> Optional[int]:
    """Index of the date-header line for `date_str`, or None."""
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
            break
        if SIGNAL_LINE_RE.match(line):
            count += 1
    return count


class DuplicateSignalError(ValueError):
    """Raised by append_manual_signal when the content already exists."""

    def __init__(self, existing_line: str, existing_index: int):
        super().__init__(f"Already present as #{existing_index}: {existing_line}")
        self.existing_line = existing_line
        self.existing_index = existing_index


def _find_matching_signal_in_section(
        lines: list[str], header_idx: int,
        side: str, r1: float, r2: float, sl: float,
        tp1: float, tp2: float, tp3: float, time_text: str,
) -> Optional[tuple[str, int]]:
    """Return existing signal with same side, prices, and time."""
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


def _insert_into_section(lines: list[str], header_idx: int, signal_line: str) -> list[str]:
    """Insert `signal_line` at the end of the section, before trailing blanks."""
    end = len(lines)
    for j in range(header_idx + 1, len(lines)):
        if DATE_HEADER_RE.match(lines[j]):
            end = j
            break
    insert_at = end
    while insert_at > header_idx + 1 and lines[insert_at - 1].strip() == "":
        insert_at -= 1
    return lines[:insert_at] + [signal_line] + lines[insert_at:]


def _append_new_section(lines: list[str], date_str: str, tz_offset: int, signal_line: str) -> list[str]:
    while lines and lines[-1].strip() == "":
        lines.pop()
    if lines:
        lines.append("")
    tz_label = f"GMT+{tz_offset}" if tz_offset >= 0 else f"GMT{tz_offset}"
    lines.append(f"{date_str} {tz_label}")
    lines.append(signal_line)
    return lines


def write_signal_to_file(parsed: ParsedSignal, signal_dt_gmt7: datetime) -> tuple[str, int, bool]:
    """Append a parsed VICTOR signal to signals.txt.

    Returns (signal_line, day_index, was_duplicate).
    """
    date_str = signal_dt_gmt7.strftime("%Y-%m-%d")
    time_text = _format_time(signal_dt_gmt7)

    lines = _read_signals_lines()
    header_idx = _find_section(lines, date_str)

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
        lines = _append_new_section(lines, date_str, SIGNAL_SOURCE_TZ_OFFSET, signal_line)
    else:
        day_index = _section_signal_count(lines, header_idx) + 1
        signal_line = parsed.to_line(day_index, time_text)
        lines = _insert_into_section(lines, header_idx, signal_line)

    _atomic_write_lines(lines)
    return signal_line, day_index, False


def append_manual_signal(line: str) -> tuple[str, int, list[str], str, Optional[float]]:
    """Append a strict-canonical signal line from Saved Messages.

    The day-index in `line` is replaced with the next free index in today's
    section. Logic-only sanity corrections are applied before writing.
    """
    m = STRICT_LINE_RE.match(line.strip())
    if not m:
        raise ValueError(
            "Line does not match the canonical format. Expected: "
            "`N. BUY|SELL XAUUSD R1 - R2 SL S TP1 T1 TP2 T2 TP3 T3 HH:MM AM|PM`"
        )

    raw_parsed = ParsedSignal(
        side=m.group("side").upper(),
        r1=float(m.group("r1")), r2=float(m.group("r2")),
        sl=float(m.group("sl")),
        tp1=float(m.group("tp1")), tp2=float(m.group("tp2")), tp3=float(m.group("tp3")),
    )
    fix = apply_signal_corrections(raw_parsed)
    parsed = fix.corrected
    time_text = m.group("time")

    now_gmt7 = datetime.utcnow() + timedelta(hours=SIGNAL_SOURCE_TZ_OFFSET)
    date_str = now_gmt7.strftime("%Y-%m-%d")

    lines = _read_signals_lines()
    header_idx = _find_section(lines, date_str)

    if header_idx is not None:
        match = _find_matching_signal_in_section(
            lines, header_idx,
            parsed.side, parsed.r1, parsed.r2, parsed.sl,
            parsed.tp1, parsed.tp2, parsed.tp3, time_text,
        )
        if match is not None:
            raise DuplicateSignalError(match[0], match[1])

    new_index = 1 if header_idx is None else _section_signal_count(lines, header_idx) + 1
    rendered = parsed.to_line(new_index, time_text)
    if not STRICT_LINE_RE.match(rendered):
        raise ValueError("Line is unparseable after renumbering -- nothing was written.")

    if header_idx is None:
        lines = _append_new_section(lines, date_str, SIGNAL_SOURCE_TZ_OFFSET, rendered)
    else:
        lines = _insert_into_section(lines, header_idx, rendered)
    _atomic_write_lines(lines)
    return rendered, new_index, fix.changes, fix.rr_bucket, fix.tp1_rr_best_entry


def next_day_index(date_str: str) -> int:
    lines = _read_signals_lines()
    header_idx = _find_section(lines, date_str)
    if header_idx is None:
        return 1
    return _section_signal_count(lines, header_idx) + 1


# ---------------------------------------------------------------------------
# state (dedup + edit handling)
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
# engine notifications bridge (tail notifications.jsonl -> Saved Messages)
# ---------------------------------------------------------------------------

def read_notification_offset(offset_path: Path) -> int:
    """Return the persisted byte offset, or -1 when no offset file exists yet
    (the caller then starts at end-of-file so it doesn't replay the backlog).
    """
    try:
        return int((offset_path.read_text(encoding="utf-8").strip() or "0"))
    except FileNotFoundError:
        return -1
    except Exception:
        log.warning("Notifications offset file unreadable; resuming from end of file.")
        return -1


def write_notification_offset(offset_path: Path, offset: int) -> None:
    tmp = offset_path.with_suffix(offset_path.suffix + ".tmp")
    tmp.write_text(str(int(offset)), encoding="utf-8")
    os.replace(tmp, offset_path)


def read_new_notification_events(path: Path, offset: int) -> tuple[list[dict], int]:
    """Return (events, new_offset) for complete JSONL lines after `offset`.

    Offsets are BYTE positions (the file holds multi-byte emoji), so reads use
    binary mode to stay consistent with ``st_size``. Only data up to the last
    newline is consumed, so a half-written final line is re-read next poll. A
    file that shrank below `offset` (rotated/truncated) resets to 0. A malformed
    line is skipped but still advances the offset, so one bad line can't wedge
    the stream.
    """
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        return [], offset
    if offset < 0 or offset > size:
        offset = 0
    if offset == size:
        return [], offset
    with path.open("rb") as f:
        f.seek(offset)
        raw = f.read()
    nl = raw.rfind(b"\n")
    if nl == -1:
        return [], offset
    complete = raw[: nl + 1]
    new_offset = offset + len(complete)
    events: list[dict] = []
    for bline in complete.split(b"\n"):
        line = bline.decode("utf-8", "replace").strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except Exception:
            log.warning("Skipping malformed notifications line.")
    return events, new_offset


# ---------------------------------------------------------------------------
# quarantine
# ---------------------------------------------------------------------------

def quarantine(message_id: int, raw_text: str, reason: str, dt_utc: datetime) -> None:
    """Append a parse-failure entry to telegram_quarantine.txt."""
    block = f"=== {dt_utc.isoformat()}  message_id={message_id}  reason={reason} ===\n{raw_text}\n\n"
    with open(QUARANTINE_PATH, "a", encoding="utf-8") as f:
        f.write(block)


# ---------------------------------------------------------------------------
# listener
# ---------------------------------------------------------------------------

class Listener:
    def __init__(self, cfg: Config, *, dry_run: bool = False):
        self.cfg = cfg
        self.dry_run = dry_run
        self.client = TelegramClient(SESSION_NAME, cfg.api_id, cfg.api_hash)
        self.state = load_state()
        self.channel_id: Optional[int] = cfg.channel_id
        self.saved_id: Optional[int] = None
        self._lock = asyncio.Lock()
        self._notifications_path = (
            Path(cfg.notifications_path) if cfg.notifications_path else NOTIFICATIONS_PATH
        )
        self._notifications_offset_path = self._notifications_path.with_suffix(
            self._notifications_path.suffix + ".offset"
        )
        self._last_forwarded_text: Optional[str] = None

    async def _resolve_channel(self) -> int:
        """Find the channel id from config or title-substring match."""
        if self.channel_id is not None:
            try:
                entity = await self.client.get_entity(self.channel_id)
                title = getattr(entity, "title", "?")
                log.info(f"Listening on channel id={self.channel_id} title={title!r}")
                return self.channel_id
            except Exception as e:
                log.warning(
                    f"channel_id={self.channel_id} couldn't be resolved ({e}); "
                    "falling back to title-substring match."
                )

        pattern = self.cfg.channel_title_pattern.upper()
        matches = []
        async for dialog in self.client.iter_dialogs():
            title = (dialog.title or "").upper()
            if pattern in title:
                matches.append(dialog)
        if not matches:
            raise RuntimeError(
                f"No chat title contains {self.cfg.channel_title_pattern!r}. "
                "Run `python listener\\telegram_listener.py list-chats` and "
                "put the numeric id into listener_config.json (repo root) "
                "under `channel_id`."
            )
        if len(matches) > 1:
            titles = ", ".join(f"{d.id}={d.title!r}" for d in matches)
            raise RuntimeError(
                f"Multiple chats match {self.cfg.channel_title_pattern!r}: {titles}. "
                "Disambiguate by setting `channel_id` in listener_config.json."
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

        self.client.add_event_handler(self._on_channel_new, events.NewMessage(chats=self.channel_id))
        self.client.add_event_handler(self._on_channel_edit, events.MessageEdited(chats=self.channel_id))
        self.client.add_event_handler(self._on_saved, events.NewMessage(chats=self.saved_id, from_users=self.saved_id))

    async def catch_up(self, lookback_hours: int = 24) -> None:
        """Process channel messages that arrived while the listener was down."""
        last_id = self.state.get("last_processed_message_id", 0)
        cutoff_dt = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
        log.info(f"Catch-up: looking for channel messages with id > {last_id} (or up to {lookback_hours}h ago)")
        new_msgs = []
        async for msg in self.client.iter_messages(self.channel_id, limit=500):
            if msg.id <= last_id:
                break
            if msg.date < cutoff_dt:
                break
            new_msgs.append(msg)
        for msg in reversed(new_msgs):
            await self._process_message(msg, is_edit=False)
        log.info(f"Catch-up done ({len(new_msgs)} messages scanned)")

    async def _on_channel_new(self, event) -> None:
        await self._process_message(event.message, is_edit=False)

    async def _on_channel_edit(self, event) -> None:
        await self._process_message(event.message, is_edit=True)

    async def _on_saved(self, event) -> None:
        text = (event.message.message or "").strip()
        if not text or not STRICT_LINE_RE.match(text):
            return
        async with self._lock:
            try:
                if self.dry_run:
                    log.info(f"[dry-run] would inject manual signal: {text}")
                    await self._reply_saved(f"✅ [dry-run] would inject: `{text}`")
                else:
                    rendered, idx, changes, rr_bucket, tp1_rr = append_manual_signal(text)
                    rr_text = f"{rr_bucket}" + (f" TP1_RR={tp1_rr:.2f}" if tp1_rr is not None else "")
                    if changes:
                        log.info(f"Manual injection #{idx} (auto-corrected {', '.join(changes)}; {rr_text}): {rendered}")
                        await self._reply_saved(
                            f"✅ Injected as #{idx} (auto-corrected: {', '.join(changes)}; {rr_text}):\n`{rendered}`"
                        )
                    elif rr_bucket == "LOW_TP1_RR":
                        log.info(f"Manual injection #{idx} ({rr_text}): {rendered}")
                        await self._reply_saved(f"⚠️ Injected as #{idx}, but classified {rr_text}:\n`{rendered}`")
                    else:
                        log.info(f"Manual injection #{idx} ({rr_text}): {rendered}")
                        await self._reply_saved(f"✅ Injected as #{idx} in today's section ({rr_text}):\n`{rendered}`")
            except DuplicateSignalError as e:
                log.info(f"Manual injection is a duplicate: matches #{e.existing_index} ({e.existing_line})")
                await self._reply_saved(f"ℹ️ Already in signals.txt as #{e.existing_index}:\n`{e.existing_line}`")
            except ValueError as e:
                log.warning(f"Manual injection rejected: {e}")
                await self._reply_saved(f"❌ {e}")
            except Exception as e:
                log.error(f"Manual injection unexpected error: {e}")
                await self._reply_saved(f"❌ Unexpected error: {e}")

    async def _process_message(self, msg, *, is_edit: bool) -> None:
        message_id = msg.id
        raw = msg.message or ""

        if msg.date.tzinfo is None:
            msg_dt_utc = msg.date.replace(tzinfo=timezone.utc)
        else:
            msg_dt_utc = msg.date.astimezone(timezone.utc)
        msg_dt_gmt7 = msg_dt_utc.astimezone(SIGNAL_SOURCE_TZ).replace(tzinfo=None)

        async with self._lock:
            existing = state_get(self.state, message_id)

            if existing and existing.get("status") == "written":
                log.debug(
                    f"Message {message_id} already written as {existing.get('signal_key')}; ignoring "
                    f"({'edit' if is_edit else 'new'} event)"
                )
                return

            if existing and existing.get("status") in ("quarantined", "write_failed") and not is_edit:
                log.debug(
                    f"Catch-up saw previously-{existing.get('status')} message {message_id}; "
                    "ignoring (user already notified)"
                )
                return

            if is_edit and existing:
                log.info(f"Edit on previously-{existing.get('status')} message {message_id}: re-parsing")

            try:
                parsed_raw = parse_victor_signal(raw)
            except ValueError as e:
                log.warning(f"Parse failure on message {message_id}: {e}")
                state_set(self.state, message_id, {"status": "quarantined", "reason": str(e), "raw": raw[:500]})
                save_state(self.state)
                if not self.dry_run:
                    quarantine(message_id, raw, str(e), msg_dt_utc)
                    await self._notify_failure(raw, str(e), msg_dt_gmt7)
                return

            if parsed_raw is None:
                return

            # Correct only impossible SL/TP mistakes. RR is only classified.
            fix = apply_signal_corrections(parsed_raw)
            if fix.changed:
                log.warning(f"Auto-corrected message {message_id}: {', '.join(fix.changes)}")
            if fix.is_low_tp1_rr:
                rr_display = f"{fix.tp1_rr_best_entry:.2f}" if fix.tp1_rr_best_entry is not None else "n/a"
                log.warning(f"Message {message_id} classified LOW_TP1_RR (best-entry TP1 RR={rr_display})")
            parsed = fix.corrected

            try:
                if self.dry_run:
                    line_preview = parsed.to_line(
                        next_day_index(msg_dt_gmt7.strftime("%Y-%m-%d")),
                        _format_time(msg_dt_gmt7),
                    )
                    log.info(f"[dry-run] would append: {line_preview} [{fix.rr_bucket}]")
                    state_set(self.state, message_id, {"status": "dry-run", "line": line_preview, "rr_bucket": fix.rr_bucket})
                else:
                    signal_line, day_index, was_duplicate = write_signal_to_file(parsed, msg_dt_gmt7)
                    signal_key = f"{msg_dt_gmt7:%Y-%m-%d}#{day_index:02d}"
                    state_payload = {
                        "status": "written",
                        "signal_key": signal_key,
                        "line": signal_line,
                        "rr_bucket": fix.rr_bucket,
                        "tp1_rr_best_entry": fix.tp1_rr_best_entry,
                        **({"auto_corrected": fix.changes} if fix.changed else {}),
                    }
                    if was_duplicate:
                        log.info(f"Message {message_id} matches existing {signal_key}: {signal_line} -- skipping write")
                        state_payload["note"] = "matched existing entry by content"
                        state_set(self.state, message_id, state_payload)
                    else:
                        state_set(self.state, message_id, state_payload)
                        log.info(f"Wrote {signal_key}: {signal_line} [{fix.rr_bucket}]")
                        if fix.changed:
                            await self._notify_correction(raw, signal_key, signal_line, fix.changes)
                        if fix.is_low_tp1_rr:
                            await self._notify_low_rr(raw, signal_key, signal_line, fix.tp1_rr_best_entry)
                save_state(self.state)
            except Exception as e:
                log.error(f"Write failed on message {message_id}: {e}")
                state_set(self.state, message_id, {"status": "write_failed", "reason": str(e), "raw": raw[:500]})
                save_state(self.state)
                if not self.dry_run:
                    await self._notify_failure(raw, f"Write failed: {e}", msg_dt_gmt7)

    async def _notify_failure(self, raw: str, reason: str, msg_dt_gmt7: datetime) -> None:
        """Post a Saved Messages note with a pre-filled correction template."""
        date_str = msg_dt_gmt7.strftime("%Y-%m-%d")
        time_text = _format_time(msg_dt_gmt7)
        idx = next_day_index(date_str)
        suggestion = f"{idx}. BUY XAUUSD R1 - R2 SL S TP1 T1 TP2 T2 TP3 T3 {time_text}"
        truncated = raw[:800] + ("..." if len(raw) > 800 else "")
        notification = (
            f"⚠️ Couldn't parse a VICTOR message ({reason}).\n"
            f"\nRaw:\n```\n{truncated}\n```\n"
            f"\nReply with the corrected line in this format (replace `BUY` with `SELL` if needed):\n"
            f"`{suggestion}`"
        )
        try:
            await self.client.send_message(self.saved_id, notification)
        except Exception as e:
            log.warning(f"Saved Messages notification failed: {e}")

    async def _notify_correction(self, raw: str, signal_key: str, signal_line: str, changes: list[str]) -> None:
        """Tell the user we auto-corrected a VICTOR mistype.

        Best-effort: failures only log, never raise.
        """
        truncated = raw[:600] + ("..." if len(raw) > 600 else "")
        body = (
            f"⚠️ Auto-corrected {signal_key} (VICTOR mistype):\n"
            f"`{signal_line}`\n"
            f"Changes: {', '.join(changes)}\n"
            f"\nOriginal raw:\n```\n{truncated}\n```"
        )
        try:
            await self.client.send_message(self.saved_id, body)
        except Exception as e:
            log.warning(f"Saved Messages correction notice failed: {e}")

    async def _notify_low_rr(self, raw: str, signal_key: str, signal_line: str, tp1_rr: Optional[float]) -> None:
        """Warn that the signal was saved but has LOW_TP1_RR.

        This is not a correction. It is only a backtest/filter classification.
        """
        rr_display = f"{tp1_rr:.2f}" if tp1_rr is not None else "n/a"
        truncated = raw[:600] + ("..." if len(raw) > 600 else "")
        body = (
            f"⚠️ LOW_TP1_RR saved for {signal_key}:\n"
            f"`{signal_line}`\n"
            f"Best laddered entry TP1 RR = {rr_display}\n"
            f"Signal was saved unchanged except any impossible SL/TP typo correction.\n"
            f"\nOriginal raw:\n```\n{truncated}\n```"
        )
        try:
            await self.client.send_message(self.saved_id, body)
        except Exception as e:
            log.warning(f"Saved Messages LOW_TP1_RR notice failed: {e}")

    async def _reply_saved(self, text: str) -> None:
        try:
            await self.client.send_message(self.saved_id, text)
        except Exception as e:
            log.warning(f"Saved Messages reply failed: {e}")

    async def _forward_notifications(self) -> None:
        """Tail the engine notifications JSONL and forward each event's `text`
        to Saved Messages. Best-effort: any failure here is logged and retried,
        never propagated -- a notification problem must not take down the
        channel listener.
        """
        path = self._notifications_path
        offset_path = self._notifications_offset_path
        offset = read_notification_offset(offset_path)
        if offset < 0:
            # First run against this file: start at the end so the listener
            # doesn't replay events that predate it coming up.
            try:
                offset = path.stat().st_size
            except FileNotFoundError:
                offset = 0
            write_notification_offset(offset_path, offset)
        log.info(f"Forwarding engine notifications from {path} (offset={offset}).")
        while True:
            try:
                events, new_offset = read_new_notification_events(path, offset)
                for event in events:
                    text = event.get("text")
                    if not text:
                        continue
                    # Collapse a machine retry loop emitting the identical line
                    # every cycle (e.g. a TP1 lock the broker keeps rejecting).
                    if text == self._last_forwarded_text:
                        continue
                    if self.dry_run:
                        log.info(f"[dry-run] would forward to Saved Messages:\n{text}")
                    else:
                        await self._reply_saved(text)
                    self._last_forwarded_text = text
                    await asyncio.sleep(NOTIFICATIONS_SEND_GAP_SECONDS)
                # Persist only after a batch is sent: at-least-once delivery is
                # the right bias for monitoring (a crash re-sends, never drops).
                if new_offset != offset:
                    offset = new_offset
                    write_notification_offset(offset_path, offset)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning(f"Notification forwarder error (continuing): {e}")
            await asyncio.sleep(NOTIFICATIONS_POLL_SECONDS)

    async def run(self) -> None:
        await self.setup()
        await self.catch_up()
        log.info("Listening. Press Ctrl+C to stop.")
        forwarder = asyncio.create_task(self._forward_notifications())
        try:
            await self.client.run_until_disconnected()
        finally:
            forwarder.cancel()
            try:
                await forwarder
            except asyncio.CancelledError:
                pass


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
        title = dialog.title or "(no title)"
        print(f"{dialog.id:>20}  {kind:<10} {title!r}")
    await client.disconnect()
    print()
    print(
        "Copy the ID of the VICTOR channel into listener_config.json "
        "(repo root) under `channel_id`, then run "
        "`python listener\\telegram_listener.py`."
    )
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
