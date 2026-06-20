"""Per-leg closure reporting from MT5 deal history (Saved Messages).

Read-only observability layered on the live `auto` loop: it reports each
entry's *actual* broker close (price, realized profit, reason) the moment
it happens, rather than waiting for the whole signal's footprint to vanish
into the final `signal_closed` summary. It never decides, modifies, or
closes anything -- a failure here must never touch trading.

Dedup is disk-backed (`closed_deals.json`) because the executor is rebuilt
every auto cycle, so an in-memory "already reported" set would re-announce
every closure on the next pass.
"""
from __future__ import annotations
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Optional

from trading.engine import signal_to_magic
from trading.engine.execution.mt5_executor_tp2 import _entry_index_from_comment


def _reason_label(mt5, code: Any) -> str:
    """Map an MT5 deal reason code to a short human label. Reads the named
    constants off the live module when present so it survives broker/terminal
    builds; the fallbacks are the standard MT5 numeric codes.
    """
    mapping = {
        getattr(mt5, "DEAL_REASON_TP", 5): "TP",
        getattr(mt5, "DEAL_REASON_SL", 4): "SL",
        getattr(mt5, "DEAL_REASON_SO", 6): "SO",
        getattr(mt5, "DEAL_REASON_CLIENT", 0): "manual",
        getattr(mt5, "DEAL_REASON_MOBILE", 1): "manual",
        getattr(mt5, "DEAL_REASON_WEB", 2): "manual",
        getattr(mt5, "DEAL_REASON_EXPERT", 3): "expert",
    }
    return mapping.get(code, f"reason{code}")


def _load_reported(path: Path) -> set[int]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return {int(t) for t in data.get("reported", [])}
    except FileNotFoundError:
        return set()
    except Exception:
        return set()


def _save_reported(path: Path, reported: set[int]) -> None:
    # Keep the ledger bounded; deal tickets increase monotonically, so the
    # newest are the only ones a future cycle can still re-encounter.
    trimmed = sorted(reported)[-5000:]
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp.write_text(json.dumps({"reported": trimmed}), encoding="utf-8")
        tmp.replace(path)
    except Exception:
        pass


def report_entry_closures(executor, notifier, tracked, *,
                          ledger_path: Path | str,
                          server_offset_hours: int,
                          lookback_days: int = 3) -> None:
    """Emit one `position_closed` notification per newly-closed entry deal.

    `tracked` is the auto loop's list of (pos_ideal, pos_actual, executed_at).
    Best-effort and self-contained: any failure is swallowed so a reporting
    problem can never break the trading loop.
    """
    if notifier is None or getattr(notifier, "path", None) is None:
        return
    mt5 = getattr(executor, "mt5", None)
    if mt5 is None or not tracked:
        return

    out_kind = getattr(mt5, "DEAL_ENTRY_OUT", 1)
    in_kind = getattr(mt5, "DEAL_ENTRY_IN", 0)
    ledger_path = Path(ledger_path)

    try:
        now = datetime.now(UTC).replace(tzinfo=None)
        deals = mt5.history_deals_get(
            now - timedelta(days=lookback_days),
            now + timedelta(days=1),
        )
    except Exception:
        return
    if not deals:
        return

    by_position: dict[Any, list] = {}
    for d in deals:
        if getattr(d, "symbol", None) != executor.symbol:
            continue
        by_position.setdefault(getattr(d, "position_id", None), []).append(d)

    reported = _load_reported(ledger_path)
    newly: set[int] = set()

    for _ideal, actual, _exec in tracked:
        sig = getattr(actual, "signal", None)
        if sig is None:
            continue
        magic = signal_to_magic(sig.signal_key)
        for group in by_position.values():
            if not any(getattr(d, "magic", None) == magic for d in group):
                continue
            in_deal = next((d for d in group if getattr(d, "entry", None) == in_kind), None)
            idx = _entry_index_from_comment(getattr(in_deal, "comment", None)) if in_deal else None
            entry_label = (idx + 1) if idx is not None else "?"
            for d in group:
                if getattr(d, "entry", None) != out_kind:
                    continue
                ticket = int(getattr(d, "ticket", 0))
                if ticket in reported or ticket in newly:
                    continue
                try:
                    notifier.position_closed(
                        signal_key=sig.signal_key,
                        side=sig.side,
                        entry_index=entry_label,
                        ticket=int(getattr(d, "position_id", ticket) or ticket),
                        close_price=float(getattr(d, "price", 0.0)),
                        profit=float(getattr(d, "profit", 0.0)),
                        reason=_reason_label(mt5, getattr(d, "reason", None)),
                        close_time=executor._broker_epoch_to_chart_time(int(getattr(d, "time", 0))),
                    )
                except Exception:
                    continue
                newly.add(ticket)

    if newly:
        _save_reported(ledger_path, reported | newly)
