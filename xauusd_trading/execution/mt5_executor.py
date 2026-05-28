"""MT5 trade execution and active position management.

Used by `decide --execute` and `auto`. Places fresh signals as N LIMIT
orders with SL and final-target TP, manages the TP1-lock, cancels expired
pendings, and time-closes positions past max-hold.

Tagging: each signal gets a stable 31-bit magic from its signal_key, and
the signal_key is written to the order comment — so MT5 itself becomes
the source of truth for "what's currently active". A small JSON registry
(positions.json) maps magic -> signal text so engine state can be rebuilt
on later runs.

Three divergence-correction mechanisms (none are cross-signal overlays):
  - `reconcile_with_mt5` — patches PENDING entries from MT5's actual fills
    when the bar replay missed them (same-minute fills, positive slippage).
  - `place_signal` re-entry guard — skips if MT5 already has orders or
    positions tagged with this signal's magic.
  - `manage_position` Late TP1 catch-up — closes at market when the engine
    has LOCK_TP1 entries but the broker SL hasn't moved to TP1 yet.

Observability:
  - `notifier` (optional): every action point emits a Saved Messages event.
  - `forensic` (optional): every action point emits a structured JSONL
    event with request + response + outcome, plus reconcile patches.
"""
from __future__ import annotations
import hashlib
import json
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from xauusd_trading import StrategyConfig
from xauusd_trading import NewSignalPlan
from xauusd_trading import Mt5Connection
from xauusd_trading import Position, advance_bars
from xauusd_trading import Signal
from xauusd_trading import Notifier
from xauusd_trading import ForensicLog


DEFAULT_MIN_LOT = 0.01
DEFAULT_LOT_STEP = 0.01
DEFAULT_REGISTRY = "positions.json"


def signal_to_magic(signal_key: str) -> int:
    """Stable 31-bit positive int derived from signal_key."""
    h = hashlib.md5(signal_key.encode("utf-8")).digest()
    return int.from_bytes(h[:4], "big") & 0x7FFFFFFF


def signal_entry_key(signal_key: str, entry_index: int) -> str:
    """Human-readable one-based key for a generated entry."""
    return f"{signal_key}.{entry_index + 1}"


def mt5_entry_comment(signal_key: str, entry_index: int, max_len: int = 31) -> str:
    """MT5-safe per-entry comment preserving the one-based entry suffix."""
    suffix = f".{entry_index + 1}"
    prefix_len = max_len - len(suffix)
    if prefix_len <= 0:
        return suffix[-max_len:]
    return f"{signal_key[:prefix_len]}{suffix}"


def round_lot(lot: float, min_lot: float = 0.01, lot_step: float = 0.01) -> float:
    """Floor `lot` to a multiple of `lot_step` and enforce `min_lot`.

    Epsilon on the floor prevents FP dust (0.15 → 0.14). Returns 0.0 if
    floored value is below `min_lot`.
    """
    if lot <= 0:
        return 0.0
    steps = math.floor(lot / lot_step + 1e-9)
    rounded = round(steps * lot_step, 2)
    if rounded < min_lot - 1e-9:
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
    """JSON-file registry of currently-tracked signals.

    Entry shape: {"signal_key", "signal", "date", "tz", "equity_at_open",
                  "executed_at" (optional, chart-tz ISO)}
    Auto-pruned: entries whose magic has no MT5 footprint are removed on
    each manage/auto cycle.
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

    def add(self, signal: Signal, equity: float,
            executed_at: Optional[datetime] = None) -> None:
        """Insert or replace the entry for this signal."""
        entries = self.load()
        entries = [e for e in entries if e.get("signal_key") != signal.signal_key]
        record = {
            "signal_key": signal.signal_key,
            "signal": _reconstruct_signal_text(signal),
            "date": signal.source_date,
            "tz": signal.source_tz_offset,
            "equity_at_open": float(equity),
        }
        if executed_at is not None:
            record["executed_at"] = executed_at.isoformat()
        entries.append(record)
        self.save(entries)

    def prune(self, alive_magics: set[int]) -> int:
        """Remove entries whose magic is not in alive_magics. Returns count removed."""
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


# ---------------------------------------------------------------------------
# Mt5Executor
# ---------------------------------------------------------------------------

class Mt5Executor:
    """Place and manage trades for the validated strategy."""

    def __init__(self, conn: Mt5Connection, symbol: str,
                 min_lot: float = DEFAULT_MIN_LOT,
                 lot_step: float = DEFAULT_LOT_STEP,
                 server_offset_hours: int = 3,
                 notifier: Optional[Notifier] = None,
                 forensic: Optional[ForensicLog] = None):
        """
        conn : initialized Mt5Connection.
        symbol : exact symbol string from MT5 Market Watch.
        min_lot, lot_step : broker rounding constraints.
        server_offset_hours : broker server tz offset from UTC. Most XAUUSD
            brokers are GMT+3. Used by `reconcile_with_mt5` to convert
            MT5's position.time back into chart tz.
        notifier : optional Notifier; action outcomes are emitted as Saved
            Messages events.
        forensic : optional ForensicLog; every order_send + reconcile patch
            is appended as a JSONL event for post-mortem analysis.
        """
        self.conn = conn
        self.mt5 = conn.mt5
        self.symbol = symbol
        self.min_lot = min_lot
        self.lot_step = lot_step
        self.server_offset_hours = server_offset_hours
        self.notifier = notifier
        self.forensic = forensic
        self._sym_info = None

    def _log_order_send(self, signal_key: str, action: str,
                        request: dict, response, *, success: bool) -> None:
        if self.forensic is not None:
            self.forensic.order_send(signal_key=signal_key, action=action,
                                     request=request, response=response,
                                     success=success)

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

        # No fresh tick or zero bid/ask = market closed.
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

    # ---- reconciliation ------------------------------------------------

    def _broker_epoch_to_chart_time(self, epoch: int) -> datetime:
        """MT5's broker-time-as-UTC-epoch -> chart-tz naive datetime.

        Mirrors Mt5ChartSource._to_chart_time. MT5 stores times as Unix
        epoch ints but interprets them as broker-local-time-treated-as-UTC,
        so getting chart-tz back requires shifting by (3 - server_offset).
        """
        broker_naive = datetime.utcfromtimestamp(int(epoch))
        return broker_naive + timedelta(hours=3 - self.server_offset_hours)

    def reconcile_with_mt5(
            self, engine_pos: Position, config: StrategyConfig,
            chart, now: datetime,
    ) -> ExecutionLog:
        """Sync engine_pos's PENDING entries with MT5's actual open positions.

        Maps MT5 positions to engine entry slots by chronological order
        (laddered LIMITs fire in deterministic price order; their time
        order matches the slot order in the engine). Patches PENDING
        entries to OPEN using MT5's actual fill price, lot, and time.
        Re-advances the position from the earliest patched fill to `now`
        so stage transitions catch up. Idempotent.
        """
        log = ExecutionLog()
        magic = signal_to_magic(engine_pos.signal.signal_key)
        signal_key = engine_pos.signal.signal_key

        mt5_positions = sorted(
            self.find_positions(magic),
            key=lambda p: p.time,
        )
        if not mt5_positions:
            return log

        if len(mt5_positions) > len(engine_pos.entries):
            msg = (f"Magic {magic} ({signal_key}): MT5 has "
                   f"{len(mt5_positions)} positions but engine has only "
                   f"{len(engine_pos.entries)} entry slots. Skipping "
                   f"reconciliation to avoid mis-mapping.")
            log.warnings.append(msg)
            if self.forensic is not None:
                self.forensic.reconcile_skipped(
                    signal_key=signal_key, reason=msg,
                    mt5_count=len(mt5_positions),
                    engine_count=len(engine_pos.entries),
                )
            return log

        earliest_patched: Optional[datetime] = None

        for i, mt5_pos in enumerate(mt5_positions):
            entry = engine_pos.entries[i]
            if entry.status != "PENDING":
                # Already OPEN or terminal — don't overwrite.
                if self.forensic is not None and entry.status != "OPEN":
                    # OPEN is the normal case after first reconcile; only
                    # log terminal-status skips for diagnostic value.
                    self.forensic.reconcile_skipped(
                        signal_key=signal_key,
                        reason=f"slot {i} not PENDING (status={entry.status})",
                        entry_index=i,
                        entry_status=entry.status,
                        mt5_ticket=int(mt5_pos.ticket),
                        mt5_price_open=float(mt5_pos.price_open),
                    )
                continue

            fill_time_chart = self._broker_epoch_to_chart_time(mt5_pos.time)
            actual_price = float(mt5_pos.price_open)
            actual_lot = float(mt5_pos.volume)
            planned_price = entry.entry_price

            log.actions.append(
                f"  Reconciled #{i} ({signal_key}): "
                f"MT5 fill at {actual_price:g} lot={actual_lot:.2f} at "
                f"{fill_time_chart:%Y-%m-%d %H:%M:%S} GMT+3 "
                f"(engine had PENDING at planned {planned_price:g})"
            )
            if self.forensic is not None:
                self.forensic.reconcile_action(
                    signal_key=signal_key,
                    entry_index=i,
                    before_status="PENDING",
                    after_status="OPEN",
                    mt5_ticket=int(mt5_pos.ticket),
                    fill_price=actual_price,
                    fill_time=fill_time_chart,
                    lot=actual_lot,
                    planned_price=planned_price,
                )

            entry.status = "OPEN"
            entry.fill_time = fill_time_chart
            entry.entry_price = actual_price
            entry.lot = actual_lot
            # initial_sl NOT recomputed — preserves the planned stop distance
            # that matches the broker-side SL attached at placement. Positive
            # slippage = better R:R, not a wider stop.

            if earliest_patched is None or fill_time_chart < earliest_patched:
                earliest_patched = fill_time_chart

        if earliest_patched is None:
            return log

        # Anchor first_fill_time / time_exit_deadline off the real MT5 fill
        # only if our anchor is earlier than what's there (preserves any
        # first_fill_time the bar-replay correctly caught on another entry).
        if (engine_pos.first_fill_time is None
                or earliest_patched < engine_pos.first_fill_time):
            engine_pos.first_fill_time = earliest_patched
            engine_pos.time_exit_deadline = (
                earliest_patched + timedelta(minutes=config.max_hold_minutes)
            )

        # Re-advance from earliest patched fill so stage/exits catch up.
        bars = chart.bars_between(earliest_patched, now)
        advance_bars(engine_pos, bars, config)

        return log

    # ---- placement -----------------------------------------------------

    def place_signal(self, signal: Signal, plan: NewSignalPlan) -> ExecutionLog:
        """Place all PENDING entries from `plan` as LIMIT orders with the
        signal's magic and the strategy's SL/TP attached.
        """
        log = ExecutionLog()
        magic = signal_to_magic(signal.signal_key)

        # Re-entry guard: don't place if MT5 already has a footprint for
        # this magic (defends against duplicate placement within a session
        # even when the registry was manually edited or pruned).
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

        place_failures: list[tuple[int, float, str]] = []

        for o in plan.orders:
            lot = round_lot(o.lot, self.min_lot, self.lot_step)
            if lot <= 0:
                log.actions.append(
                    f"  #{o.entry_index}: computed lot {o.lot:.4f} < broker minimum "
                    f"{self.min_lot}; skipping this entry"
                )
                continue

            comment = mt5_entry_comment(signal.signal_key, o.entry_index)
            entry_key = signal_entry_key(signal.signal_key, o.entry_index)
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
            success = bool(res is not None
                           and res.retcode == self.mt5.TRADE_RETCODE_DONE)
            self._log_order_send(signal.signal_key, "place_pending",
                                 request, res, success=success)
            if res is None:
                reason = str(self.mt5.last_error())
                log.actions.append(
                    f"  #{o.entry_index}: FAILED order_send returned None: {reason}"
                )
                place_failures.append((o.entry_index, o.entry_price, reason))
            elif res.retcode != self.mt5.TRADE_RETCODE_DONE:
                reason = f"retcode={res.retcode} comment={res.comment!r}"
                log.actions.append(
                    f"  #{o.entry_index}: FAILED {reason}"
                )
                place_failures.append((o.entry_index, o.entry_price, reason))
            else:
                log.placed += 1
                log.actions.append(
                    f"  {entry_key}: placed ticket={res.order} comment={comment} "
                    f"@ {request['price']:g} lot={lot} "
                    f"SL={request['sl']:g} TP={request['tp']:g}"
                )

        if self.notifier is not None and place_failures:
            self.notifier.place_failed(
                signal_key=signal.signal_key,
                side=signal.side,
                failures=place_failures,
            )
        return log

    # ---- management ----------------------------------------------------

    def manage_position(self, engine_pos: Position, config: StrategyConfig,
                        chart_now: datetime) -> ExecutionLog:
        """Reconcile MT5 with engine state for one tracked signal.

        Pass the *actual* replay (started at executed_at when present, not
        signal time) so engine stage/fill state matches MT5 reality. Call
        `reconcile_with_mt5` first to absorb any same-minute fills.
        """
        log = ExecutionLog()
        magic = signal_to_magic(engine_pos.signal.signal_key)
        digits = self.mt5.symbol_info(self.symbol).digits
        signal_key = engine_pos.signal.signal_key
        side = engine_pos.signal.side

        # 1. Cancel pending orders that should have expired.
        if chart_now > engine_pos.expiry_time:
            cancel_failures: list[tuple[int, str]] = []
            for o in self.find_orders(magic):
                req = {"action": self.mt5.TRADE_ACTION_REMOVE, "order": o.ticket}
                res = self.mt5.order_send(req)
                success = bool(res is not None
                               and res.retcode == self.mt5.TRADE_RETCODE_DONE)
                self._log_order_send(signal_key, "cancel_pending_expired",
                                     req, res, success=success)
                if success:
                    log.cancelled += 1
                    log.actions.append(
                        f"  Cancelled expired pending #{o.ticket} ({signal_key})"
                    )
                else:
                    reason = str(res.comment if res else self.mt5.last_error())
                    log.actions.append(
                        f"  FAILED to cancel pending #{o.ticket}: {reason}"
                    )
                    cancel_failures.append((o.ticket, reason))
            if self.notifier is not None and cancel_failures:
                self.notifier.cancel_failed(
                    signal_key=signal_key, side=side, failures=cancel_failures,
                )

        # 2. Late TP1 catch-up.
        target_sl = round(engine_pos.signal.tp1, digits)
        if any(e.status == "LOCK_TP1" for e in engine_pos.entries):
            unlocked = [
                p for p in self.find_positions(magic)
                if abs(p.sl - target_sl) > 10 ** (-digits)
            ]
            if unlocked:
                backtest_lock_pnl = sum(
                    e.pnl or 0.0
                    for e in engine_pos.entries
                    if e.status == "LOCK_TP1"
                )
                catchup_closed: list[tuple[int, float]] = []
                catchup_failed: list[tuple[int, str]] = []
                for p in unlocked:
                    tick = self.mt5.symbol_info_tick(self.symbol)
                    if tick is None:
                        log.actions.append(
                            f"  Late TP1 catch-up on #{p.ticket}: no tick "
                            f"available, skipping"
                        )
                        catchup_failed.append((p.ticket, "no tick available"))
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
                        "comment":      f"{signal_key}/late-tp1"[:31],
                        "type_filling": self.mt5.ORDER_FILLING_RETURN,
                    }
                    res = self.mt5.order_send(req)
                    success = bool(res is not None
                                   and res.retcode == self.mt5.TRADE_RETCODE_DONE)
                    self._log_order_send(signal_key, "close_catchup_tp1",
                                         req, res, success=success)
                    if success:
                        log.closed += 1
                        log.actions.append(
                            f"  Late TP1 catch-up closed #{p.ticket} @ {price:g} "
                            f"({signal_key}; backtest LOCK_TP1 would have realized "
                            f"${backtest_lock_pnl:+.2f} -- actual close at current market)"
                        )
                        catchup_closed.append((p.ticket, price))
                    else:
                        reason = str(res.comment if res else self.mt5.last_error())
                        log.actions.append(
                            f"  FAILED late TP1 catch-up close on #{p.ticket}: {reason}"
                        )
                        catchup_failed.append((p.ticket, reason))
                if self.notifier is not None:
                    self.notifier.late_tp1_catchup(
                        signal_key=signal_key, side=side,
                        closed=catchup_closed, failed=catchup_failed,
                        backtest_pnl=backtest_lock_pnl,
                    )

        # 3. Move SL to TP1 if engine is in stage 1 (TP1 touched, no
        # LOCK_TP1 entries yet). Once a LOCK_TP1 entry appears, step 2
        # takes precedence and closes at market.
        if config.lock_after_tp1 and engine_pos.stage >= 1:
            locked_tickets: list[int] = []
            lock_failures: list[tuple[int, str]] = []
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
                success = bool(res is not None
                               and res.retcode == self.mt5.TRADE_RETCODE_DONE)
                self._log_order_send(signal_key, "modify_sl_to_tp1",
                                     req, res, success=success)
                if success:
                    log.modified += 1
                    log.actions.append(
                        f"  Locked SL on #{p.ticket} to TP1 {target_sl:g} ({signal_key})"
                    )
                    locked_tickets.append(p.ticket)
                else:
                    reason = str(res.comment if res else self.mt5.last_error())
                    log.actions.append(
                        f"  FAILED SL-lock on #{p.ticket}: {reason}"
                    )
                    lock_failures.append((p.ticket, reason))
            if self.notifier is not None:
                self.notifier.tp1_lock(
                    signal_key=signal_key, side=side,
                    locked=locked_tickets, failed=lock_failures,
                    sl=target_sl,
                )

        # 4. Time-exit: close still-open positions past the engine deadline.
        if (engine_pos.time_exit_deadline is not None
                and chart_now >= engine_pos.time_exit_deadline):
            timeout_closed: list[tuple[int, float]] = []
            timeout_failed: list[tuple[int, str]] = []
            for p in self.find_positions(magic):
                tick = self.mt5.symbol_info_tick(self.symbol)
                if tick is None:
                    log.actions.append(
                        f"  Time-exit on #{p.ticket}: no tick available, skipping"
                    )
                    timeout_failed.append((p.ticket, "no tick available"))
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
                    "comment":      f"{signal_key}/timeout"[:31],
                    "type_filling": self.mt5.ORDER_FILLING_RETURN,
                }
                res = self.mt5.order_send(req)
                success = bool(res is not None
                               and res.retcode == self.mt5.TRADE_RETCODE_DONE)
                self._log_order_send(signal_key, "close_time_exit",
                                     req, res, success=success)
                if success:
                    log.closed += 1
                    log.actions.append(
                        f"  Time-exit closed #{p.ticket} @ {price:g} ({signal_key})"
                    )
                    timeout_closed.append((p.ticket, price))
                else:
                    reason = str(res.comment if res else self.mt5.last_error())
                    log.actions.append(
                        f"  FAILED time-exit close on #{p.ticket}: {reason}"
                    )
                    timeout_failed.append((p.ticket, reason))
            if self.notifier is not None:
                self.notifier.time_exit(
                    signal_key=signal_key, side=side,
                    closed=timeout_closed, failed=timeout_failed,
                )
            # Also kill any leftover pendings for this signal.
            for o in self.find_orders(magic):
                req = {"action": self.mt5.TRADE_ACTION_REMOVE, "order": o.ticket}
                res = self.mt5.order_send(req)
                success = bool(res is not None
                               and res.retcode == self.mt5.TRADE_RETCODE_DONE)
                self._log_order_send(signal_key, "cancel_after_timeout",
                                     req, res, success=success)
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
