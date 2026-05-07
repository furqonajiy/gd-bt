"""MT5 trade execution and active position management.

Used by `decide --execute`. Places fresh signals as 3 LIMIT orders with SL+TP2
attached, manages the TP1-lock by modifying SL once TP1 has been touched,
cancels expired pendings, and time-closes positions past the 90-min hold.

Tagging: each signal gets a unique 31-bit magic number derived from its
signal_key (e.g. "2026-05-07#01"), and its signal_key is written to the
order/position comment so MT5 itself becomes the source of truth for "what's
currently active". A small JSON registry (positions.json) maps magic ->
signal text so we can replay engine state on later runs.
"""
from __future__ import annotations
import hashlib
import json
import math
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

from .config import StrategyConfig
from .engine import NewSignalPlan
from .mt5_adapter import Mt5Connection
from .positions import Position
from .signal import Signal


DEFAULT_MIN_LOT = 0.01
DEFAULT_LOT_STEP = 0.01
DEFAULT_REGISTRY = "positions.json"


def signal_to_magic(signal_key: str) -> int:
    """Stable 31-bit positive int derived from signal_key."""
    h = hashlib.md5(signal_key.encode("utf-8")).digest()
    return int.from_bytes(h[:4], "big") & 0x7FFFFFFF


def round_lot(lot: float, min_lot: float, lot_step: float) -> float:
    """Floor lot to step, enforce minimum. Returns 0.0 if rounding produces 0."""
    if lot <= 0:
        return 0.0
    rounded = math.floor(lot / lot_step) * lot_step
    rounded = round(rounded, 8)
    if rounded < min_lot:
        return 0.0
    return rounded


@dataclass
class ExecutionLog:
    actions: list[str] = field(default_factory=list)
    placed: int = 0
    cancelled: int = 0
    modified: int = 0
    closed: int = 0
    warnings: list[str] = field(default_factory=list)

    def merge(self, other: "ExecutionLog") -> None:
        self.actions.extend(other.actions)
        self.warnings.extend(other.warnings)
        self.placed += other.placed
        self.cancelled += other.cancelled
        self.modified += other.modified
        self.closed += other.closed


class SignalRegistry:
    """Tiny JSON-file registry of currently-tracked signals.

    Each entry: {"signal_key": str, "signal": str, "date": "YYYY-MM-DD",
                 "tz": int, "equity_at_open": float}
    Auto-pruned: entries whose magic has zero MT5 orders AND zero MT5 positions
    are removed on each call.
    """

    def __init__(self, path: Path):
        self.path = Path(path)

    def load(self) -> list[dict]:
        if not self.path.exists():
            return []
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return []

    def save(self, entries: list[dict]) -> None:
        self.path.write_text(json.dumps(entries, indent=2), encoding="utf-8")

    def add(self, signal: Signal, equity: float) -> None:
        entries = self.load()
        entries = [e for e in entries if e.get("signal_key") != signal.signal_key]
        entries.append({
            "signal_key": signal.signal_key,
            "signal": _reconstruct_signal_text(signal),
            "date": signal.source_date,
            "tz": signal.source_tz_offset,
            "equity_at_open": float(equity),
        })
        self.save(entries)

    def prune(self, alive_magics: set[int]) -> int:
        """Remove entries whose magic is NOT in alive_magics. Returns count removed."""
        entries = self.load()
        before = len(entries)
        entries = [e for e in entries
                   if signal_to_magic(e.get("signal_key", "?")) in alive_magics]
        if len(entries) != before:
            self.save(entries)
        return before - len(entries)


def _reconstruct_signal_text(s: Signal) -> str:
    """Rebuild a parseable line from a Signal (for round-tripping in registry)."""
    return (f"{s.day_id}. {s.side} XAUUSD {s.r1:g} - {s.r2:g} "
            f"SL {s.sl:g} TP1 {s.tp1:g} TP2 {s.tp2:g} TP3 {s.tp3:g} "
            f"{s.source_time_text}")


class Mt5Executor:
    """Place and manage trades for the validated strategy."""

    def __init__(self, conn: Mt5Connection, symbol: str,
                 min_lot: float = DEFAULT_MIN_LOT,
                 lot_step: float = DEFAULT_LOT_STEP):
        self.conn = conn
        self.mt5 = conn.mt5
        self.symbol = symbol
        self.min_lot = min_lot
        self.lot_step = lot_step
        self._sym_info = None

    # ---- pre-flight ----------------------------------------------------

    def sanity_checks(self, expected_equity: Optional[float] = None,
                      equity_tolerance_pct: float = 50.0) -> list[str]:
        """Return list of error strings (empty list = OK)."""
        errors: list[str] = []
        info = self.mt5.account_info()
        if info is None:
            errors.append(f"account_info() failed: {self.mt5.last_error()}")
            return errors
        if info.equity <= 0:
            errors.append(f"Account equity is non-positive: {info.equity}")
        if getattr(info, "trade_allowed", True) is False:
            errors.append("Account has trading disabled")

        sym = self.mt5.symbol_info(self.symbol)
        if sym is None:
            errors.append(f"Symbol {self.symbol!r} not found in MT5")
            return errors
        if not sym.visible:
            self.mt5.symbol_select(self.symbol, True)
            sym = self.mt5.symbol_info(self.symbol)
        if sym.trade_mode == self.mt5.SYMBOL_TRADE_MODE_DISABLED:
            errors.append(f"Trading disabled for {self.symbol}")
        self._sym_info = sym

        # Detect closed market: no fresh tick or zero bid/ask.
        tick = self.mt5.symbol_info_tick(self.symbol)
        if tick is None or (tick.bid == 0 and tick.ask == 0):
            errors.append(f"No live tick for {self.symbol} (market may be closed)")

        if expected_equity is not None and info.equity > 0:
            drift_pct = abs(info.equity - expected_equity) / expected_equity * 100
            if drift_pct > equity_tolerance_pct:
                errors.append(
                    f"Account equity ${info.equity:,.2f} differs from expected "
                    f"${expected_equity:,.2f} by {drift_pct:.1f}% "
                    f"(tolerance {equity_tolerance_pct}%) -- wrong account?"
                )
        return errors

    # ---- discovery -----------------------------------------------------

    def find_orders(self, magic: int) -> list:
        return [o for o in (self.mt5.orders_get(symbol=self.symbol) or [])
                if o.magic == magic]

    def find_positions(self, magic: int) -> list:
        return [p for p in (self.mt5.positions_get(symbol=self.symbol) or [])
                if p.magic == magic]

    def all_alive_magics(self) -> set[int]:
        magics: set[int] = set()
        for o in (self.mt5.orders_get(symbol=self.symbol) or []):
            magics.add(o.magic)
        for p in (self.mt5.positions_get(symbol=self.symbol) or []):
            magics.add(p.magic)
        return magics

    def warn_on_unknown(self, known_magics: set[int]) -> list[str]:
        warnings: list[str] = []
        for o in (self.mt5.orders_get(symbol=self.symbol) or []):
            if o.magic not in known_magics:
                warnings.append(
                    f"Unknown MT5 order on {self.symbol}: ticket={o.ticket} "
                    f"magic={o.magic} comment={o.comment!r}"
                )
        for p in (self.mt5.positions_get(symbol=self.symbol) or []):
            if p.magic not in known_magics:
                warnings.append(
                    f"Unknown MT5 position on {self.symbol}: ticket={p.ticket} "
                    f"magic={p.magic} comment={p.comment!r}"
                )
        return warnings

    # ---- placement -----------------------------------------------------

    def place_signal(self, signal: Signal, plan: NewSignalPlan) -> ExecutionLog:
        """Place 3 LIMIT orders with SL=effective stop and TP=final target (TP2)."""
        log = ExecutionLog()
        magic = signal_to_magic(signal.signal_key)
        comment = signal.signal_key[:31]

        if self.find_orders(magic) or self.find_positions(magic):
            log.actions.append(
                f"Signal {signal.signal_key} already has MT5 orders/positions; "
                f"skipping placement (will manage instead)."
            )
            return log

        order_type = (self.mt5.ORDER_TYPE_BUY_LIMIT if signal.side == "BUY"
                      else self.mt5.ORDER_TYPE_SELL_LIMIT)
        sym = self._sym_info if self._sym_info is not None else self.mt5.symbol_info(self.symbol)
        digits = sym.digits

        for o in plan.orders:
            lot = round_lot(o.lot, self.min_lot, self.lot_step)
            if lot <= 0:
                log.actions.append(
                    f"  #{o.entry_index}: computed lot {o.lot:.4f} < broker minimum "
                    f"{self.min_lot}; skipping this entry"
                )
                continue

            request = {
                "action":       self.mt5.TRADE_ACTION_PENDING,
                "symbol":       self.symbol,
                "volume":       lot,
                "type":         order_type,
                "price":        round(o.entry_price, digits),
                "sl":           round(o.initial_sl, digits),
                "tp":           round(plan.final_target_price, digits),
                "magic":        magic,
                "comment":      comment,
                "type_time":    self.mt5.ORDER_TIME_GTC,
                "type_filling": self.mt5.ORDER_FILLING_RETURN,
            }
            res = self.mt5.order_send(request)
            if res is None:
                log.actions.append(
                    f"  #{o.entry_index}: FAILED order_send returned None: "
                    f"{self.mt5.last_error()}"
                )
            elif res.retcode != self.mt5.TRADE_RETCODE_DONE:
                log.actions.append(
                    f"  #{o.entry_index}: FAILED retcode={res.retcode} "
                    f"comment={res.comment!r}"
                )
            else:
                log.placed += 1
                log.actions.append(
                    f"  #{o.entry_index}: placed ticket={res.order} "
                    f"@ {request['price']:g} lot={lot} "
                    f"SL={request['sl']:g} TP={request['tp']:g}"
                )
        return log

    # ---- management ----------------------------------------------------

    def manage_position(self, engine_pos: Position, config: StrategyConfig,
                        chart_now: datetime) -> ExecutionLog:
        """Reconcile MT5 with engine state for one tracked signal."""
        log = ExecutionLog()
        magic = signal_to_magic(engine_pos.signal.signal_key)
        digits = self.mt5.symbol_info(self.symbol).digits

        # 1. Cancel pending orders that should have expired.
        if chart_now > engine_pos.expiry_time:
            for o in self.find_orders(magic):
                req = {"action": self.mt5.TRADE_ACTION_REMOVE, "order": o.ticket}
                res = self.mt5.order_send(req)
                if res and res.retcode == self.mt5.TRADE_RETCODE_DONE:
                    log.cancelled += 1
                    log.actions.append(
                        f"  Cancelled expired pending #{o.ticket} "
                        f"({engine_pos.signal.signal_key})"
                    )
                else:
                    log.actions.append(
                        f"  FAILED to cancel pending #{o.ticket}: "
                        f"{res.comment if res else self.mt5.last_error()}"
                    )

        # 2. Move SL to TP1 if engine is in stage 1 (TP1 was touched).
        if config.lock_after_tp1 and engine_pos.stage >= 1:
            target_sl = round(engine_pos.signal.tp1, digits)
            for p in self.find_positions(magic):
                if abs(p.sl - target_sl) <= 10 ** (-digits):
                    continue  # already locked
                req = {
                    "action":   self.mt5.TRADE_ACTION_SLTP,
                    "position": p.ticket,
                    "sl":       target_sl,
                    "tp":       p.tp,
                }
                res = self.mt5.order_send(req)
                if res and res.retcode == self.mt5.TRADE_RETCODE_DONE:
                    log.modified += 1
                    log.actions.append(
                        f"  Locked SL on #{p.ticket} to TP1 {target_sl:g} "
                        f"({engine_pos.signal.signal_key})"
                    )
                else:
                    log.actions.append(
                        f"  FAILED SL-lock on #{p.ticket}: "
                        f"{res.comment if res else self.mt5.last_error()}"
                    )

        # 3. Time-exit: close still-open positions if engine deadline passed.
        if (engine_pos.time_exit_deadline is not None
                and chart_now >= engine_pos.time_exit_deadline):
            for p in self.find_positions(magic):
                tick = self.mt5.symbol_info_tick(self.symbol)
                if tick is None:
                    log.actions.append(
                        f"  Time-exit on #{p.ticket}: no tick available, skipping"
                    )
                    continue
                if p.type == self.mt5.POSITION_TYPE_BUY:
                    close_type, price = self.mt5.ORDER_TYPE_SELL, tick.bid
                else:
                    close_type, price = self.mt5.ORDER_TYPE_BUY, tick.ask
                req = {
                    "action":       self.mt5.TRADE_ACTION_DEAL,
                    "position":     p.ticket,
                    "symbol":       self.symbol,
                    "volume":       p.volume,
                    "type":         close_type,
                    "price":        price,
                    "magic":        magic,
                    "comment":      f"{engine_pos.signal.signal_key}/timeout"[:31],
                    "type_filling": self.mt5.ORDER_FILLING_RETURN,
                }
                res = self.mt5.order_send(req)
                if res and res.retcode == self.mt5.TRADE_RETCODE_DONE:
                    log.closed += 1
                    log.actions.append(
                        f"  Time-exit closed #{p.ticket} @ {price:g} "
                        f"({engine_pos.signal.signal_key})"
                    )
                else:
                    log.actions.append(
                        f"  FAILED time-exit close on #{p.ticket}: "
                        f"{res.comment if res else self.mt5.last_error()}"
                    )
            # Also kill any leftover pendings from this signal.
            for o in self.find_orders(magic):
                self.mt5.order_send({
                    "action": self.mt5.TRADE_ACTION_REMOVE, "order": o.ticket,
                })
        return log


def render_execution_log(log: ExecutionLog) -> str:
    lines = []
    lines.append(
        f"EXECUTION:  placed={log.placed}  modified={log.modified}  "
        f"cancelled={log.cancelled}  closed={log.closed}"
    )
    for a in log.actions:
        lines.append(a)
    if log.warnings:
        lines.append("")
        lines.append("WARNINGS:")
        for w in log.warnings:
            lines.append(f"  ! {w}")
    return "\n".join(lines)
