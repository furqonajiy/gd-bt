"""Engine -> forensic log (JSONL) for post-mortem analysis.

Append-only structured event log. Each event has ts, kind, cycle_id
(when inside an auto/manage iteration), plus event-specific fields.
The accompanying tools/dump_forensic.py script filters and pretty-prints.

Events:
    cycle_start, cycle_end       Bracket each auto/manage iteration.
    engine_snapshot              Per-tracked-signal engine state, post-reconcile.
    mt5_snapshot                 Per-tracked-signal MT5 state (orders + positions).
    reconcile_action             Each entry patched by reconcile.
    order_send                   Every MT5 order_send call + response + outcome.
    decision                     SKIP/FOLLOW outcomes for new signals.
    closure_detected             Tracked signal's MT5 footprint is gone.
    error                        Unexpected exception with traceback.

Disabling: pass path=None (no-op). Failures swallow silently -- forensic
logging must never break trading.
"""
from __future__ import annotations
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Optional
from uuid import uuid4


DEFAULT_FORENSIC_PATH = "forensic.jsonl"


def _utc_now_naive() -> datetime:
    """Return UTC wall-clock time as naive datetime for JSONL compatibility."""
    return datetime.now(UTC).replace(tzinfo=None)


def _serializable_request(req: dict) -> dict:
    """Make an MT5 order_send request dict JSON-safe."""
    out: dict = {}
    for k, v in req.items():
        if isinstance(v, (str, int, float, bool, type(None))):
            out[k] = v
        else:
            out[k] = str(v)
    return out


def _serializable_response(res: Any) -> Optional[dict]:
    """Pull the useful fields off an MT5 OrderSendResult."""
    if res is None:
        return None
    return {
        "retcode": int(getattr(res, "retcode", -1)),
        "comment": getattr(res, "comment", None),
        "order": int(getattr(res, "order", 0)),
        "deal": int(getattr(res, "deal", 0)),
        "price": float(getattr(res, "price", 0.0) or 0.0),
        "volume": float(getattr(res, "volume", 0.0) or 0.0),
    }


class ForensicLog:
    """Append-only JSONL event sink for post-mortem analysis."""

    def __init__(self, path: Optional[Path | str] = None):
        self.path: Optional[Path] = Path(path) if path else None
        self._cycle_id: Optional[str] = None

    @property
    def enabled(self) -> bool:
        return self.path is not None

    def _emit(self, kind: str, **fields: Any) -> None:
        if self.path is None:
            return
        event = {
            "ts": _utc_now_naive().isoformat(timespec="microseconds"),
            "kind": kind,
        }
        if self._cycle_id is not None:
            event["cycle_id"] = self._cycle_id
        event.update(fields)
        try:
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(event, default=str, ensure_ascii=False) + "\n")
        except Exception:
            pass  # never break trading

    # ---- cycle brackets ----------------------------------------------

    def start_cycle(self, subcommand: str, iteration: int, chart_time,
                    equity: float, bid: Optional[float] = None,
                    ask: Optional[float] = None,
                    tracked_count: int = 0) -> str:
        self._cycle_id = uuid4().hex[:12]
        self._emit("cycle_start",
                   subcommand=subcommand, iteration=iteration,
                   chart_time=str(chart_time),
                   equity=float(equity),
                   bid=(float(bid) if bid is not None else None),
                   ask=(float(ask) if ask is not None else None),
                   tracked_count=int(tracked_count))
        return self._cycle_id

    def end_cycle(self, placed: int = 0, modified: int = 0,
                  cancelled: int = 0, closed: int = 0,
                  errors: int = 0) -> None:
        self._emit("cycle_end",
                   placed=int(placed), modified=int(modified),
                   cancelled=int(cancelled), closed=int(closed),
                   errors=int(errors))
        self._cycle_id = None

    # ---- per-signal snapshots ----------------------------------------

    def engine_snapshot(self, pos) -> None:
        """Capture full engine state for one tracked signal."""
        s = pos.signal
        entries = []
        for e in pos.entries:
            entries.append({
                "index": e.entry_index,
                "entry_price": float(e.entry_price),
                "initial_sl": float(e.initial_sl),
                "lot": float(e.lot),
                "status": e.status,
                "fill_time": (str(e.fill_time) if e.fill_time else None),
                "exit_time": (str(e.exit_time) if e.exit_time else None),
                "exit_price": (float(e.exit_price)
                               if e.exit_price is not None else None),
                "pnl": (float(e.pnl) if e.pnl is not None else None),
                "stop_at_exit": (float(e.stop_at_exit)
                                 if e.stop_at_exit is not None else None),
                "armed_for_touch": bool(e.armed_for_touch),
            })
        self._emit(
            "engine_snapshot",
            signal_key=s.signal_key,
            side=s.side,
            signal_time=str(s.signal_time_chart),
            tp1=float(s.tp1), tp2=float(s.tp2), tp3=float(s.tp3),
            signal_sl=float(s.sl),
            stage=int(pos.stage),
            first_fill_time=(str(pos.first_fill_time)
                             if pos.first_fill_time else None),
            time_exit_deadline=(str(pos.time_exit_deadline)
                                if pos.time_exit_deadline else None),
            activation_time=str(pos.activation_time),
            expiry_time=str(pos.expiry_time),
            executed_at=(str(pos.executed_at) if pos.executed_at else None),
            last_processed_time=(str(pos.last_processed_time)
                                 if pos.last_processed_time else None),
            entries=entries,
        )

    def mt5_snapshot(self, signal_key: str, magic: int,
                     orders: list, positions: list) -> None:
        """Capture current MT5 footprint (orders + positions) for one magic."""
        ord_dicts = []
        for o in orders:
            ord_dicts.append({
                "ticket": int(o.ticket),
                "type": int(o.type),
                "price_open": float(o.price_open),
                "sl": float(o.sl),
                "tp": float(o.tp),
                "volume_initial": float(
                    getattr(o, "volume_initial",
                            getattr(o, "volume_current", 0.0)) or 0.0),
                "comment": getattr(o, "comment", ""),
            })
        pos_dicts = []
        for p in positions:
            pos_dicts.append({
                "ticket": int(p.ticket),
                "type": int(p.type),
                "price_open": float(p.price_open),
                "price_current": float(getattr(p, "price_current", 0.0) or 0.0),
                "sl": float(p.sl),
                "tp": float(p.tp),
                "volume": float(p.volume),
                "time": int(p.time),
                "profit": float(getattr(p, "profit", 0.0) or 0.0),
                "comment": getattr(p, "comment", ""),
            })
        self._emit("mt5_snapshot",
                   signal_key=signal_key, magic=int(magic),
                   orders=ord_dicts, positions=pos_dicts)

    # ---- per-action events -------------------------------------------

    def reconcile_action(self, signal_key: str, entry_index: int,
                         before_status: str, after_status: str,
                         mt5_ticket: int, fill_price: float,
                         fill_time, lot: float,
                         planned_price: float) -> None:
        self._emit("reconcile_action",
                   signal_key=signal_key,
                   entry_index=int(entry_index),
                   before_status=before_status,
                   after_status=after_status,
                   mt5_ticket=int(mt5_ticket),
                   fill_price=float(fill_price),
                   fill_time=str(fill_time),
                   lot=float(lot),
                   planned_price=float(planned_price))

    def reconcile_skipped(self, signal_key: str, reason: str, **extra) -> None:
        """Reconcile considered patching but skipped (positional-mapping
        mismatch, MT5 has more positions than engine slots, etc.)."""
        self._emit("reconcile_skipped",
                   signal_key=signal_key, reason=reason, **extra)

    def order_send(self, signal_key: str, action: str,
                   request: dict, response: Any, success: bool) -> None:
        """Every MT5 order_send call. action labels:
        place_pending, cancel_pending_expired, close_catchup_tp1,
        modify_sl_to_tp1, close_time_exit, cancel_after_timeout.
        """
        self._emit("order_send",
                   signal_key=signal_key,
                   action=action,
                   success=bool(success),
                   request=_serializable_request(request),
                   response=_serializable_response(response))

    def decision(self, signal_key: str, action: str,
                 rationale: str = "", **extra) -> None:
        self._emit("decision",
                   signal_key=signal_key,
                   action=action,
                   rationale=rationale,
                   **extra)

    def closure_detected(self, signal_key: str, side: str, summary: str,
                         realized_pnl: float, per_entry: list[str]) -> None:
        self._emit("closure_detected",
                   signal_key=signal_key,
                   side=side,
                   summary=summary,
                   realized_pnl=float(realized_pnl),
                   per_entry=per_entry)

    def error(self, where: str, message: str,
              traceback_str: Optional[str] = None) -> None:
        self._emit("error",
                   where=where,
                   message=message,
                   traceback=traceback_str)
