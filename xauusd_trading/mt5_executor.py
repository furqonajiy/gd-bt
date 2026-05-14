"""MT5 trade execution and active position management.

Used by `decide --execute`. Places fresh signals as N LIMIT orders (where N
is the configured entry_count) with SL and the configured final-target TP
attached, manages the TP1-lock by modifying SL once TP1 has been touched,
cancels expired pendings, and time-closes positions past the configured
max-hold deadline.

Tagging: each signal gets a unique 31-bit magic number derived from its
signal_key (e.g. "2026-05-07#01"), and its signal_key is written to the
order/position comment so MT5 itself becomes the source of truth for "what's
currently active". A small JSON registry (positions.json) maps magic ->
signal text so we can replay engine state on later runs.

Re-entry protection: `place_signal` consults TWO guards before sending any
LIMITs:
  1. positions.json membership (checked in cli.py before calling here)
  2. find_orders / find_positions  -- magic has a current MT5 footprint

The third layer that used to live here (a 12h MT5-history lookback) has been
removed. The engine's `_build_new_signal_plan` now filters placed entries
based on the backtest replay's per-entry status: entries whose replay status
is PENDING or OPEN reach this method; entries whose replay status is terminal
never do. With the engine's replay as the authoritative source for the
place/skip decision, the magic-level history guard is redundant in the normal
case and over-restrictive in the user's case (re-entry after a manual clear,
where the replay correctly says "still in play" but history shows old deals).

The remaining two guards continue to check current live state, not history,
so they're orthogonal to the replay and still useful as duplicate-prevention.
`has_recent_history` is retained below for diagnostic scripts (e.g.
diagnose_history.py) but is no longer called from `place_signal`.

The registry also persists `executed_at` (wall-clock placement time in chart
tz). This lets `manage` and `decide` show "X min late" annotations and
optionally replay the position from your real placement moment to reflect
what MT5 actually sees -- because the engine's default replay-from-signal-time
assumption is the strategy's ideal, not what happens when a human is slow.

Divergence reconciliation: `reconcile_with_mt5` patches the engine's
PENDING entries from MT5's actual open positions when the bar-by-bar replay
missed a fill (typically a same-minute fill at executed_at, or positive
slippage). This is a per-signal correction, not a cross-signal overlay --
backtest is unaffected.
"""
from __future__ import annotations
import calendar
import hashlib
import json
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from .config import StrategyConfig
from .engine import NewSignalPlan
from .mt5_adapter import Mt5Connection
from .positions import Position, advance_bars
from .signal import Signal


DEFAULT_MIN_LOT = 0.01
DEFAULT_LOT_STEP = 0.01
DEFAULT_REGISTRY = "positions.json"
# Lookback used by the (now-diagnostic-only) has_recent_history function.
# Kept here so external scripts that import the constant (e.g.
# diagnose_history.py at the repo root) continue to work. The constant
# is no longer consulted by place_signal -- the engine's per-entry replay
# filtering supersedes it.
HISTORY_LOOKBACK_HOURS = 12


def signal_to_magic(signal_key: str) -> int:
    """Stable 31-bit positive int derived from signal_key."""
    h = hashlib.md5(signal_key.encode("utf-8")).digest()
    return int.from_bytes(h[:4], "big") & 0x7FFFFFFF


def round_lot(lot: float, min_lot: float = 0.01, lot_step: float = 0.01) -> float:
    """Floor `lot` to a multiple of `lot_step` and enforce `min_lot`.

    Always returns a clean multiple of 0.01 (no floating-point dust like
    0.15000000000000002). Uses a small epsilon on the floor so values that
    are *already* on the step don't drop a step (e.g. 0.15 -> 0.14 due to FP).
    Returns 0.0 if the floored value is below `min_lot`.
    """
    if lot <= 0:
        return 0.0
    steps = math.floor(lot / lot_step + 1e-9)
    rounded = round(steps * lot_step, 2)  # 2 decimals = clean 0.01 multiples
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
    """Tiny JSON-file registry of currently-tracked signals.

    Each entry: {"signal_key": str, "signal": str, "date": "YYYY-MM-DD",
                 "tz": int, "equity_at_open": float,
                 "executed_at": "YYYY-MM-DDTHH:MM:SS" (chart tz, optional)}
    Auto-pruned: entries whose magic has zero MT5 orders AND zero MT5 positions
    are removed on each call.

    `executed_at` is set by `decide --execute` immediately after `place_signal`
    reports a successful placement. Older entries written before this field
    existed will be missing it; downstream tooling treats that as "unknown
    lateness" and falls back to the ideal-execution replay (signal-time start).
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
        """Insert or replace the registry entry for this signal.

        `executed_at` is the wall-clock placement time in chart tz (GMT+3).
        Pass None to omit (e.g. when migrating legacy code paths).
        """
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
                 lot_step: float = DEFAULT_LOT_STEP,
                 server_offset_hours: int = 3):
        """
        Parameters
        ----------
        conn : initialized Mt5Connection.
        symbol : exact symbol string as in MT5 Market Watch.
        min_lot, lot_step : broker rounding constraints.
        server_offset_hours : broker server timezone offset from UTC. Most
            XAUUSD brokers run GMT+3 year-round, which is the project default.
            Used by `reconcile_with_mt5` to convert MT5's `position.time` back
            into chart-tz, and by the (diagnostic-only) `has_recent_history`
            to build query windows in the broker-time-as-UTC epoch space MT5
            stores history in.
        """
        self.conn = conn
        self.mt5 = conn.mt5
        self.symbol = symbol
        self.min_lot = min_lot
        self.lot_step = lot_step
        self.server_offset_hours = server_offset_hours
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

    # ---- reconciliation ------------------------------------------------

    def _broker_epoch_to_chart_time(self, epoch: int) -> datetime:
        """Convert MT5's broker-time-as-UTC-epoch to chart-tz naive datetime.

        Mirrors mt5_adapter.Mt5ChartSource._to_chart_time. MT5 stores times
        (bar.time, position.time, deal.time) as Unix-style epoch ints but
        interprets them as broker-local-time treated as UTC. To get chart-tz
        (GMT+3 naive), shift by (3 - server_offset_hours).
        """
        broker_naive = datetime.utcfromtimestamp(int(epoch))
        return broker_naive + timedelta(hours=3 - self.server_offset_hours)

    def reconcile_with_mt5(
            self, engine_pos: Position, config: StrategyConfig,
            chart, now: datetime,
    ) -> ExecutionLog:
        """Sync engine_pos's PENDING entries with MT5's actual open positions.

        When the engine's bar-by-bar replay misses an MT5 fill (most commonly
        because the fill happened in the same M1 bar as placement, or because
        positive slippage took the fill price below the planned entry), MT5
        has positions the engine still thinks are PENDING. This method queries
        MT5 directly, maps positions to engine entry slots by chronological
        order (laddered LIMITs fire in deterministic price order, so the
        time-order they fill in matches the slot-order in the engine), and
        patches PENDING entries to OPEN using MT5's actual fill price, lot,
        and time.

        The engine's `initial_sl` is left unchanged. Combined with the
        broker-side SL that was attached at placement (also unchanged), this
        preserves the original planned stop distance. Positive slippage on
        the entry then translates directly to improved R:R, not a wider stop.

        After patching, re-advances the engine through chart bars from the
        earliest patched fill time to `now`, so stage transitions (TP1 touch
        -> stage 1) catch up. Safe to re-process bars: fill checks skip
        non-PENDING entries; stop/target checks on terminal entries are no-ops.

        Idempotent: nothing happens if all entries are already in sync.
        Per-signal divergence correction, not a cross-signal overlay -- does
        not change which signals the strategy fills, only reconciles when
        bar-replay misses MT5 reality. Backtest is unaffected because it
        doesn't go through this code path.

        Returns an ExecutionLog noting any patches made (empty if none).
        """
        log = ExecutionLog()
        magic = signal_to_magic(engine_pos.signal.signal_key)

        mt5_positions = sorted(
            self.find_positions(magic),
            key=lambda p: p.time,
        )
        if not mt5_positions:
            return log

        if len(mt5_positions) > len(engine_pos.entries):
            log.warnings.append(
                f"Magic {magic} ({engine_pos.signal.signal_key}): MT5 has "
                f"{len(mt5_positions)} positions but engine has only "
                f"{len(engine_pos.entries)} entry slots. Skipping "
                f"reconciliation to avoid mis-mapping."
            )
            return log

        earliest_patched: Optional[datetime] = None

        for i, mt5_pos in enumerate(mt5_positions):
            entry = engine_pos.entries[i]
            if entry.status != "PENDING":
                # Already in sync (OPEN) or terminal (SL / TP / TIME_EXIT /
                # LOCK_TP1 / NO_FILL). Don't overwrite.
                continue

            fill_time_chart = self._broker_epoch_to_chart_time(mt5_pos.time)
            actual_price = float(mt5_pos.price_open)
            actual_lot = float(mt5_pos.volume)
            planned_price = entry.entry_price

            log.actions.append(
                f"  Reconciled #{i} ({engine_pos.signal.signal_key}): "
                f"MT5 fill at {actual_price:g} lot={actual_lot:.2f} at "
                f"{fill_time_chart:%Y-%m-%d %H:%M:%S} GMT+3 "
                f"(engine had PENDING at planned {planned_price:g})"
            )

            entry.status = "OPEN"
            entry.fill_time = fill_time_chart
            entry.entry_price = actual_price
            entry.lot = actual_lot
            # entry.initial_sl intentionally NOT recomputed -- preserves the
            # planned stop distance, which matches the broker-side SL that
            # was attached at placement. Positive slippage = better R:R, not
            # a wider stop.

            if earliest_patched is None or fill_time_chart < earliest_patched:
                earliest_patched = fill_time_chart

        if earliest_patched is None:
            return log

        # Anchor first_fill_time / time_exit_deadline off the real MT5 fill.
        # Only update if our anchor is earlier than what's already there
        # (preserves any first_fill_time the bar-replay correctly caught on
        # a different ladder entry).
        if (engine_pos.first_fill_time is None
                or earliest_patched < engine_pos.first_fill_time):
            engine_pos.first_fill_time = earliest_patched
            engine_pos.time_exit_deadline = earliest_patched + timedelta(
                minutes=config.max_hold_minutes
            )

        # Re-advance through bars from the earliest patched fill to now.
        # Bars before are unaffected; bars in between may now trigger stage
        # transitions or stop/target exits on the newly-OPEN entries that
        # the first replay couldn't see.
        bars = chart.bars_between(earliest_patched, now)
        advance_bars(engine_pos, bars, config)

        return log

    def has_recent_history(
            self, magic: int, lookback_hours: int = HISTORY_LOOKBACK_HOURS,
    ) -> bool:
        """True if MT5 history shows any closed orders/deals for this magic.

        NOTE: Retained for diagnostic scripts (e.g. diagnose_history.py at
        the repo root) but NO LONGER called from `place_signal`. The engine's
        `_build_new_signal_plan` performs per-entry filtering based on the
        backtest replay, which supersedes this guard. See module docstring
        for the rationale.

        Builds the query window in broker-time-pretending-to-be-UTC epoch
        ints, the same space MT5 uses internally to store history.time
        fields (see mt5_adapter._chart_time_to_mt5_epoch for the matching
        trick on the chart side).

        Soft-fails (returns False) if the MT5 history calls error out.
        """
        try:
            broker_now = datetime.utcnow() + timedelta(hours=self.server_offset_hours)
            to_epoch = calendar.timegm(
                (broker_now + timedelta(minutes=1)).timetuple()
            )
            from_epoch = calendar.timegm(
                (broker_now - timedelta(hours=lookback_hours)).timetuple()
            )
            orders = self.mt5.history_orders_get(from_epoch, to_epoch) or []
            if any(getattr(o, "magic", None) == magic for o in orders):
                return True
            deals = self.mt5.history_deals_get(from_epoch, to_epoch) or []
            return any(getattr(d, "magic", None) == magic for d in deals)
        except Exception:
            return False

    # ---- placement -----------------------------------------------------

    def place_signal(self, signal: Signal, plan: NewSignalPlan) -> ExecutionLog:
        """Place LIMIT orders for the planned entries, with SL and final-target TP.

        ONE idempotency guard runs before anything is sent:
          1. Live MT5 footprint (find_orders / find_positions) -- magic has
             current MT5 orders or positions.

        Plus the positions.json membership check that runs upstream in cli.py.

        The 12h MT5-history guard that used to live here has been removed:
        the engine's `_build_new_signal_plan` now filters `plan.orders` based
        on per-entry backtest-replay status (only PENDING / OPEN entries
        reach this method), which supersedes the magic-level history check.
        """
        log = ExecutionLog()
        magic = signal_to_magic(signal.signal_key)
        comment = signal.signal_key[:31]

        # Guard 1: currently-live orders or open positions for this magic.
        # Prevents duplicate placement within the same session, even if the
        # registry was manually edited or pruned.
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
        """Reconcile MT5 with engine state for one tracked signal.

        Pass the position you want MT5 actions to be driven by. When a
        signal has a recorded `executed_at`, the caller should pass the
        *actual* replay (started at executed_at, not signal time) so the
        engine's stage/fill state matches what MT5 actually saw.

        For best results, call `reconcile_with_mt5` on the same position
        first so any MT5 fills the bar-replay missed have been patched in.
        """
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

        # 2. Late TP1 catch-up.
        # When the engine's replay has LOCK_TP1 entries, the SL-at-TP1 lock
        # would have closed those positions at breakeven-plus in backtest. If
        # MT5 still has open positions for this magic whose SL is NOT yet at
        # TP1, manage was late: the lock never made it to the broker. Rather
        # than wait for the SL-lock step below to try (which gets rejected by
        # the broker if price has moved past TP1) and possibly leave the
        # position exposed to the original SL, close them at market now. This
        # caps the divergence from the backtest path -- you take the current
        # price as your exit instead of risking a full original-SL loss.
        #
        # Positions whose SL is already at TP1 (lock applied on some earlier
        # cycle) are left alone -- the broker will trigger them when price
        # returns to TP1, matching backtest's LOCK_TP1 exit precisely.
        target_sl = round(engine_pos.signal.tp1, digits)
        if any(e.status == "LOCK_TP1" for e in engine_pos.entries):
            unlocked = [
                p for p in self.find_positions(magic)
                if abs(p.sl - target_sl) > 10 ** (-digits)
            ]
            if unlocked:
                # Compute the engine's would-have-been LOCK_TP1 outcome so we
                # can log the gap between backtest and live for each close.
                backtest_lock_pnl = sum(
                    e.pnl or 0.0
                    for e in engine_pos.entries
                    if e.status == "LOCK_TP1"
                )
                for p in unlocked:
                    tick = self.mt5.symbol_info_tick(self.symbol)
                    if tick is None:
                        log.actions.append(
                            f"  Late TP1 catch-up on #{p.ticket}: no tick "
                            f"available, skipping"
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
                        "comment":      f"{engine_pos.signal.signal_key}/late-tp1"[:31],
                        "type_filling": self.mt5.ORDER_FILLING_RETURN,
                    }
                    res = self.mt5.order_send(req)
                    if res and res.retcode == self.mt5.TRADE_RETCODE_DONE:
                        log.closed += 1
                        log.actions.append(
                            f"  Late TP1 catch-up closed #{p.ticket} @ {price:g} "
                            f"({engine_pos.signal.signal_key}; backtest LOCK_TP1 "
                            f"would have realized ${backtest_lock_pnl:+.2f} -- "
                            f"actual close at current market)"
                        )
                    else:
                        log.actions.append(
                            f"  FAILED late TP1 catch-up close on #{p.ticket}: "
                            f"{res.comment if res else self.mt5.last_error()}"
                        )

        # 3. Move SL to TP1 if engine is in stage 1 (TP1 was touched).
        # This handles the "TP1 touched but hasn't returned to TP1 yet" case
        # (stage 1, no LOCK_TP1 entries yet). Once a LOCK_TP1 entry appears,
        # step 2 above takes precedence and closes at market instead.
        if config.lock_after_tp1 and engine_pos.stage >= 1:
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