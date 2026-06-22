"""Engine -> listener event sink (file-based, JSONL).

The trading engine writes events here; the Telegram listener tails the
file and forwards each event's pre-rendered `text` to Saved Messages.
Decoupled by design: the engine never imports Telethon, and the listener
doesn't import any strategy code. The file is the contract -- same
pattern as signals.txt.

Disabling: pass path=None (the Notifier becomes a no-op).
"""
from __future__ import annotations
import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Optional

from .core.rotating_jsonl import DEFAULT_NOTIFICATIONS_MAX_BYTES, append_jsonl_line


DEFAULT_NOTIFICATIONS_PATH = "notifications.jsonl"

# Emoji per dominant terminal status for closure summaries.
_STATUS_EMOJI = {
    "TP3":       "🎯",
    "TP2":       "🎯",
    "TP1":       "🎯",
    "SL":        "🛑",
    "LOCK_TP1":  "🔒",
    "TIME_EXIT": "⏰",
    "NO_FILL":   "⚪",
    "MIXED":     "📊",
}

_REPLAY_RESOLVED_RE = re.compile(
    r"^Signal (?P<signal_key>\S+): every entry has already played out in backtest replay "
    r"-- no orders placed \((?P<count>\d+) entr(?:y|ies) resolved\)\. "
    r"Backtest realized so far: \$(?P<pnl>[+-]?\d+(?:\.\d+)?)\.$"
)

_TRAILING_LABELS = {
    "trailing_open_distance": "open",
    "trailing_close_distance": "close",
}


def _utc_now_naive() -> datetime:
    """Return UTC wall-clock time as naive datetime for JSONL compatibility."""
    return datetime.now(UTC).replace(tzinfo=None)


def _fmt_time_gmt7(dt: datetime | None) -> str:
    if dt is None:
        return "n/a"
    return f"{dt + timedelta(hours=4):%Y-%m-%d %H:%M} GMT+7"


def _fmt_time_gmt7_compact(dt: datetime | None) -> str:
    if dt is None:
        return "n/a"
    return f"{dt + timedelta(hours=4):%Y-%m-%d %H:%M}"


def _fmt_active_window_gmt7(activation_at: datetime | None, expiry_at: datetime | None) -> str:
    if activation_at is None and expiry_at is None:
        return "n/a"
    return f"{_fmt_time_gmt7_compact(activation_at)} → {_fmt_time_gmt7_compact(expiry_at)} GMT+7"


def _fmt_number(value: Any) -> str:
    return f"{float(value):g}"


def _shared_tp_line(entries: list[dict[str, Any]]) -> str | None:
    if not entries:
        return None
    first = tuple(_fmt_number(entries[0].get(k)) for k in ("tp1", "tp2", "tp3"))
    for entry in entries[1:]:
        current = tuple(_fmt_number(entry.get(k)) for k in ("tp1", "tp2", "tp3"))
        if current != first:
            return None
    return "🎯 TP " + " / ".join(first)


def _fmt_entry_line(entry: dict[str, Any], *, include_tp: bool) -> str:
    parts = [
        f"#{entry.get('entry_index')}",
        str(entry.get("entry_type", "LIMIT")),
        _fmt_number(entry.get("entry_price")),
        "·",
        f"{_fmt_number(entry.get('lot'))} lot",
        "·",
        f"SL {_fmt_number(entry.get('sl'))}",
    ]
    if include_tp:
        parts.extend([
            "·",
            "TP " + "/".join(_fmt_number(entry.get(k)) for k in ("tp1", "tp2", "tp3")),
        ])
    return " ".join(parts)


def _fmt_trailing_line(trailing: dict[str, Any] | None) -> str | None:
    if not trailing:
        return None
    enabled = [(k, v) for k, v in trailing.items() if v not in (None, False, 0, 0.0, "")]
    if not enabled:
        return None
    ordered: list[tuple[str, Any]] = []
    for key in _TRAILING_LABELS:
        for candidate_key, value in enabled:
            if candidate_key == key:
                ordered.append((candidate_key, value))
    ordered.extend((k, v) for k, v in enabled if k not in _TRAILING_LABELS)
    chunks = [f"{_TRAILING_LABELS.get(k, k)} {_fmt_number(v)}" for k, v in ordered]
    return "↕ Trail " + " / ".join(chunks)


def _fmt_replay_resolved_reason(reason: str) -> str | None:
    match = _REPLAY_RESOLVED_RE.match(reason.strip())
    if not match:
        return None
    count = int(match.group("count"))
    pnl = float(match.group("pnl"))
    if count == 1:
        replay_line = "Replay already resolved 1 entry; no live orders."
    else:
        replay_line = f"Replay already resolved all {count} entries; no live orders."
    return f"{replay_line}\nRealized so far: ${pnl:+.2f}"


class Notifier:
    """Append-only JSONL event sink. All emit_* methods are best-effort
    and swallow errors -- notifications never break trading logic.
    """

    def __init__(self, path: Optional[Path | str] = None,
                 max_bytes: int | None = DEFAULT_NOTIFICATIONS_MAX_BYTES):
        self.path: Optional[Path] = Path(path) if path else None
        # Cap on-disk size: rotate to a single `.1` backup at this many bytes so
        # a long-running live executor can't fill the disk. None/<=0 = unbounded.
        self.max_bytes = max_bytes

    def _emit(self, kind: str, signal_key: str, text: str, **details: Any) -> None:
        if self.path is None:
            return
        event = {
            "ts": _utc_now_naive().isoformat(timespec="seconds"),
            "kind": kind,
            "signal_key": signal_key,
            "text": text,
        }
        if details:
            event["details"] = details
        try:
            line = json.dumps(event, default=str) + "\n"
            append_jsonl_line(self.path, line, self.max_bytes)
        except Exception:
            # Never let a notification failure break trading.
            pass

    # ---- Signal lifecycle actions -------------------------------------

    def signal_detected(self, *, signal_key: str, side: str,
                        entries: list[dict[str, Any]], activation_at: datetime | None,
                        expiry_at: datetime | None, trailing: dict[str, Any] | None = None) -> None:
        shared_tp = _shared_tp_line(entries)
        parts = [
            f"🟢 Accepted {signal_key} {side}",
            f"⏱ Active {_fmt_active_window_gmt7(activation_at, expiry_at)}",
        ]
        if shared_tp:
            parts.append(shared_tp)
        if entries:
            parts.append("📍 Entries")
            parts.extend(_fmt_entry_line(e, include_tp=shared_tp is None) for e in entries)
        trailing_line = _fmt_trailing_line(trailing)
        if trailing_line:
            parts.append(trailing_line)
        self._emit(
            "signal_detected", signal_key, text="\n".join(parts),
            side=side, entries=entries, activation_at=activation_at,
            expiry_at=expiry_at, trailing=trailing or {},
        )

    def signal_skipped(self, *, signal_key: str, side: str, reason: str) -> None:
        readable_reason = _fmt_replay_resolved_reason(reason) or f"Reason: {reason}"
        text = f"⚪ Skipped {signal_key} {side}\n{readable_reason}"
        self._emit("signal_skipped", signal_key, text=text, side=side, reason=reason)

    def order_placed(self, *, signal_key: str, side: str, order_kind: str,
                     placed: list[dict[str, Any]]) -> None:
        if not placed:
            return
        parts = [f"✅ {order_kind} orders placed {signal_key} ({side})"]
        for p in placed:
            parts.append(
                f"  #{p.get('entry_index')} ticket={p.get('ticket')} "
                f"@ {float(p.get('price')):g} lot={float(p.get('lot')):g} "
                f"SL={float(p.get('sl')):g} TP={float(p.get('tp')):g}"
            )
        self._emit("order_placed", signal_key, text="\n".join(parts),
                   side=side, order_kind=order_kind, placed=placed)

    def trailing_open_armed(self, *, signal_key: str, side: str, entry_index: int,
                            ticket: int, stop_price: float, sl: float, tp: float) -> None:
        text = (
            f"🟡 Trailing-open STOP armed {signal_key} ({side})\n"
            f"  #{entry_index} ticket={ticket} STOP={stop_price:g} SL={sl:g} TP={tp:g}"
        )
        self._emit("trailing_open_armed", signal_key, text=text, side=side,
                   entry_index=entry_index, ticket=ticket, stop_price=stop_price, sl=sl, tp=tp)

    def trailing_open_trailed(self, *, signal_key: str, side: str, entry_index: int,
                              ticket: int, old_price: float, new_price: float) -> None:
        text = (
            f"↕️ Trailing-open STOP moved {signal_key} ({side})\n"
            f"  #{entry_index} ticket={ticket} {old_price:g} → {new_price:g}"
        )
        self._emit("trailing_open_trailed", signal_key, text=text, side=side,
                   entry_index=entry_index, ticket=ticket, old_price=old_price, new_price=new_price)

    def trailing_open_filled(self, *, signal_key: str, side: str, entry_index: int,
                             ticket: int, fill_price: float) -> None:
        text = (
            f"✅ Trailing-open STOP filled {signal_key} ({side})\n"
            f"  #{entry_index} ticket={ticket} fill={fill_price:g}"
        )
        self._emit("trailing_open_filled", signal_key, text=text, side=side,
                   entry_index=entry_index, ticket=ticket, fill_price=fill_price)

    def sl_moved(self, *, signal_key: str, side: str, entry_index: int,
                 old_sl: float, new_sl: float, reason: str) -> None:
        text = (
            f"🔧 SL moved {signal_key} ({side})\n"
            f"  #{entry_index} {old_sl:g} → {new_sl:g} ({reason})"
        )
        self._emit("sl_moved", signal_key, text=text, side=side, entry_index=entry_index,
                   old_sl=old_sl, new_sl=new_sl, reason=reason)

    def tp_moved(self, *, signal_key: str, side: str, entry_index: int,
                 old_tp: float, new_tp: float, reason: str) -> None:
        text = (
            f"🔧 TP moved {signal_key} ({side})\n"
            f"  #{entry_index} {old_tp:g} → {new_tp:g} ({reason})"
        )
        self._emit("tp_moved", signal_key, text=text, side=side, entry_index=entry_index,
                   old_tp=old_tp, new_tp=new_tp, reason=reason)

    def pending_cancelled(self, *, signal_key: str, side: str,
                          cancelled: list[dict[str, Any]]) -> None:
        if not cancelled:
            return
        parts = [f"🧹 Pending cancelled {signal_key} ({side})"]
        for c in cancelled:
            parts.append(f"  ticket={c.get('ticket')} ({c.get('reason')})")
        self._emit("pending_cancelled", signal_key, text="\n".join(parts),
                   side=side, cancelled=cancelled)

    def entry_filled(self, *, signal_key: str, side: str, entry_index: int,
                     fill_price: float, source: str, ticket: int | None = None) -> None:
        ticket_text = f" ticket={ticket}" if ticket is not None else ""
        text = (
            f"✅ Entry filled {signal_key} ({side})\n"
            f"  #{entry_index}{ticket_text} fill={fill_price:g} ({source})"
        )
        self._emit("entry_filled", signal_key, text=text, side=side,
                   entry_index=entry_index, fill_price=fill_price, source=source, ticket=ticket)

    def position_closed(self, *, signal_key: str, side: str, entry_index: Any,
                        ticket: int, close_price: float, profit: float,
                        reason: str, close_time: datetime | None = None) -> None:
        """One per entry leg as its MT5 position actually closes at the broker
        (TP/SL/manual), with the broker's real close price and realized P&L.
        Distinct from `signal_closed`, which summarizes the whole signal once
        its entire footprint is gone.
        """
        emoji = "🎯" if reason == "TP" else ("🛑" if reason in ("SL", "SO") else "✅")
        when = f" at {_fmt_time_gmt7(close_time)}" if close_time is not None else ""
        text = (
            f"{emoji} Position closed {signal_key} ({side})\n"
            f"  #{entry_index} ticket={ticket} {reason} @ {close_price:g} "
            f"P&L ${profit:+.2f}{when}"
        )
        self._emit("position_closed", signal_key, text=text, side=side,
                   entry_index=entry_index, ticket=ticket,
                   close_price=close_price, profit=profit, reason=reason)

    # ---- TP1 SL-lock -------------------------------------------------

    def tp1_lock(self, *, signal_key: str, side: str,
                 locked: list[int], failed: list[tuple[int, str]],
                 sl: float) -> None:
        """One per manage cycle that touched any SL. Summarizes outcome
        across all positions of this signal.
        """
        if not locked and not failed:
            return
        parts: list[str] = []
        if locked:
            parts.append(
                f"  ✅ SL → {sl:g} on " + ", ".join(f"#{t}" for t in locked)
            )
        if failed:
            fails = "; ".join(f"#{t} ({r})" for t, r in failed)
            parts.append(f"  ❌ FAILED on {fails}")
        if not failed:
            emoji, kind = "✅", "tp1_lock_success"
        elif not locked:
            emoji, kind = "❌", "tp1_lock_failed"
        else:
            emoji, kind = "⚠️", "tp1_lock_partial"
        text = f"{emoji} TP1 lock on {signal_key} ({side})\n" + "\n".join(parts)
        self._emit(kind, signal_key, text=text,
                   side=side, locked=locked, failed=failed, sl=sl)

    # ---- Late TP1 catch-up -------------------------------------------

    def late_tp1_catchup(self, *, signal_key: str, side: str,
                         closed: list[tuple[int, float]],
                         failed: list[tuple[int, str]],
                         backtest_pnl: float) -> None:
        if not closed and not failed:
            return
        parts: list[str] = []
        for ticket, price in closed:
            parts.append(f"  ⚠️ Closed #{ticket} @ {price:g}")
        for ticket, reason in failed:
            parts.append(f"  ❌ FAILED #{ticket} ({reason})")
        emoji = "⚠️" if not failed else ("❌" if not closed else "⚠️")
        kind = (
            "late_tp1_catchup_closed" if not failed else
            ("late_tp1_catchup_failed" if not closed else "late_tp1_catchup_partial")
        )
        text = (
            f"{emoji} Late TP1 catch-up on {signal_key} ({side})\n"
            + "\n".join(parts)
            + f"\n  (backtest LOCK_TP1 would have realized ${backtest_pnl:+.2f})"
        )
        self._emit(kind, signal_key, text=text,
                   side=side, closed=closed, failed=failed,
                   backtest_pnl=backtest_pnl)

    # ---- Time-exit ---------------------------------------------------

    def time_exit(self, *, signal_key: str, side: str,
                  closed: list[tuple[int, float]],
                  failed: list[tuple[int, str]]) -> None:
        if not closed and not failed:
            return
        parts: list[str] = []
        for ticket, price in closed:
            parts.append(f"  ⏰ Closed #{ticket} @ {price:g}")
        for ticket, reason in failed:
            parts.append(f"  ❌ FAILED #{ticket} ({reason})")
        if not failed:
            emoji, kind = "⏰", "time_exit_closed"
        else:
            emoji = "❌" if not closed else "⚠️"
            kind = "time_exit_failed" if not closed else "time_exit_partial"
        text = (
            f"{emoji} Time-exit on {signal_key} ({side}, 90-min max hold)\n"
            + "\n".join(parts)
        )
        self._emit(kind, signal_key, text=text,
                   side=side, closed=closed, failed=failed)

    # ---- Order-send errors -------------------------------------------

    def place_failed(self, *, signal_key: str, side: str,
                     failures: list[tuple[int, float, str]]) -> None:
        """failures: list of (entry_index, entry_price, reason)."""
        if not failures:
            return
        parts = [f"  ❌ #{i} @ {p:g}: {r}" for i, p, r in failures]
        text = (
            f"❌ Placement failures on {signal_key} ({side})\n"
            + "\n".join(parts)
        )
        self._emit("place_failed", signal_key, text=text,
                   side=side, failures=failures)

    def cancel_failed(self, *, signal_key: str, side: str,
                      failures: list[tuple[int, str]]) -> None:
        """failures: list of (ticket, reason)."""
        if not failures:
            return
        parts = [f"  ❌ #{t}: {r}" for t, r in failures]
        text = (
            f"❌ Pending-cancel failures on {signal_key} ({side})\n"
            + "\n".join(parts)
        )
        self._emit("cancel_failed", signal_key, text=text,
                   side=side, failures=failures)

    # ---- Signal closure ----------------------------------------------

    def signal_closed(self, *, signal_key: str, side: str,
                      summary: str, realized_pnl: float,
                      per_entry: list[str]) -> None:
        """Emit when a tracked signal's MT5 footprint is gone. `summary`
        is the dominant terminal status (TP2/SL/LOCK_TP1/TIME_EXIT/MIXED).
        `per_entry` is human-readable lines describing each entry's fate.
        """
        emoji = _STATUS_EMOJI.get(summary, "ℹ️")
        per_entry_block = "\n".join(f"  {x}" for x in per_entry)
        text = (
            f"{emoji} {signal_key} ({side}) closed: {summary}\n"
            f"{per_entry_block}\n"
            f"  Realized: ${realized_pnl:+.2f} (engine view)"
        )
        self._emit("signal_closed", signal_key, text=text,
                   side=side, summary=summary,
                   realized_pnl=realized_pnl, per_entry=per_entry)


def summarize_closed_position(pos) -> tuple[str, list[str]]:
    """Build (dominant_status, per_entry_lines) for a Position whose MT5
    footprint is gone. Caller passes the engine's *actual* replay so the
    summary matches what the backtest would have realized.

    Dominant status: the single terminal status if all (non-NO_FILL) entries
    share it; otherwise 'MIXED'. NO_FILL-only positions are summarized as
    'NO_FILL'.
    """
    per_entry: list[str] = []
    statuses: list[str] = []
    for e in pos.entries:
        if e.status in ("PENDING", "OPEN"):
            per_entry.append(
                f"#{e.entry_index}={e.status} (MT5 footprint gone, status from engine)"
            )
            statuses.append(e.status)
        else:
            pnl_str = f"${e.pnl:+.2f}" if e.pnl is not None else "n/a"
            per_entry.append(f"#{e.entry_index}={e.status} {pnl_str}")
            statuses.append(e.status)
    non_nofill = [s for s in statuses if s != "NO_FILL"]
    if not non_nofill:
        dominant = "NO_FILL"
    elif all(s == non_nofill[0] for s in non_nofill):
        dominant = non_nofill[0]
    else:
        dominant = "MIXED"
    return dominant, per_entry
