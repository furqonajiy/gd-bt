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
from datetime import datetime
from pathlib import Path
from typing import Any, Optional


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


class Notifier:
    """Append-only JSONL event sink. All emit_* methods are best-effort
    and swallow errors -- notifications never break trading logic.
    """

    def __init__(self, path: Optional[Path | str] = None):
        self.path: Optional[Path] = Path(path) if path else None

    def _emit(self, kind: str, signal_key: str, text: str, **details: Any) -> None:
        if self.path is None:
            return
        event = {
            "ts": datetime.utcnow().isoformat(timespec="seconds"),
            "kind": kind,
            "signal_key": signal_key,
            "text": text,
        }
        if details:
            event["details"] = details
        try:
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(event, default=str) + "\n")
        except Exception:
            # Never let a notification failure break trading.
            pass

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
