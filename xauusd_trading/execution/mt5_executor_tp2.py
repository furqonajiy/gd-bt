"""TP2-aware MT5 executor wrapper.

The base MT5 executor places orders, reconciles fills, cancels expired pendings,
locks to TP1, and handles time exit. This wrapper adds live-only parity safety
checks used by the public executor:

* wait for wall-clock chart activation time before order_send;
* skip expired signals by wall-clock chart time before order_send;
* cancel already-placed pending LIMITs by wall-clock chart expiry;
* use wall-clock chart time for live manage deadlines when MT5 bars lag;
* skip stale/marketable pending LIMITs before order_send; and
* optionally apply TP2 stop-lock parity when the strategy enables TP2 locking.

DD40 currently uses ``lock_after_tp2=False``, so TP2 locking remains disabled
unless a config explicitly enables it.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Optional

from xauusd_trading import (
    CHART_TIMEZONE_OFFSET,
    DEFAULT_CONFIG,
    Position,
    StrategyConfig,
    advance_bars,
)
from xauusd_trading.core.trend_runner import should_skip_time_exit

from .mt5_executor import (
    ExecutionLog,
    Mt5Executor as _BaseMt5Executor,
    mt5_entry_comment,
    round_lot,
    signal_entry_key,
    signal_to_magic,
)
from .sl_safety import clamp_sltp_sl


_REPLAY_CLOSED_STATUSES = {
    "SL", "BEP", "LOCK_HALF_TP1", "LOCK_TP1", "LOCK_TP2",
    "TP1", "TP2", "TP3", "TIME_EXIT",
}

# Late-lock fallback: when the replay already lock-exited a leg but the live SL
# never reached the lock level (the 2026-06-12 reconciliation lost $468 to the
# old close-at-market catch-up), the leg is protected instead of flattened. If
# price has already moved back through the lock level, the stop goes this far
# from the live bid/ask (or the broker minimum if larger) and later cycles
# ratchet it toward the true level as price recovers.
LATE_LOCK_FALLBACK_BUFFER = 0.5
# A fallback stop is only re-tightened when it improves by at least this much,
# so a slow price recovery doesn't spam an SLTP modify every watch cycle.
# Reaching the exact lock level is always allowed regardless of step size.
LATE_LOCK_MIN_STEP = 0.25


def _wall_clock_chart_now() -> datetime:
    """Return real wall-clock time in the chart timezone (GMT+3)."""
    return datetime.now(UTC).replace(tzinfo=None) + timedelta(hours=CHART_TIMEZONE_OFFSET)


def _entry_index_from_comment(comment: str | None) -> int | None:
    """Extract the zero-based entry index from an MT5 order/position comment.

    ``mt5_entry_comment`` always preserves the one-based ``.N`` suffix even when
    the signal key is truncated to fit MT5's comment length.  Prefer this suffix
    over chronological position order so partial fills and broker fill-order
    quirks are mapped back to the correct engine entry.
    """
    if not comment:
        return None
    suffix = str(comment).rsplit(".", 1)[-1]
    if not suffix.isdigit():
        return None
    idx = int(suffix) - 1
    return idx if idx >= 0 else None


class Mt5Executor(_BaseMt5Executor):
    """Public MT5 executor with live parity guards."""

    # Process-local guards. Auto creates a fresh executor every cycle, so these
    # class-level sets prevent repeated logs for the same inactive/stale entry,
    # expired signal, or zero-placement broker failure every watch interval.
    _session_skipped_inactive_signal_keys: set[str] = set()
    _session_skipped_stale_entries: set[str] = set()
    _session_skipped_expired_signal_keys: set[str] = set()
    _session_failed_signal_keys: set[str] = set()
    _session_skipped_traded_signal_keys: set[str] = set()
    # Each cycle re-replays the signal from scratch, so a live position whose
    # replay status is non-terminal (e.g. TRAILING_STOP, which is absent from
    # _REPLAY_CLOSED_STATUSES) gets re-patched to OPEN and would re-announce the
    # same fill every interval. Key by the actual MT5 fill so each reconcile is
    # announced once, not by the flapping engine-side status.
    _session_announced_reconciles: set[str] = set()
    # Trailing-open STOP fills are announced once per (signal, entry); the fill
    # is observed when reconcile flips a PENDING/NO_FILL slot to OPEN, and a
    # flapping replay could revisit PENDING, so the set guards against re-firing.
    _session_announced_triggers: set[str] = set()

    def _broker_epoch_to_chart_time(self, epoch: int) -> datetime:
        """MT5 broker-time-as-UTC-epoch -> chart-time naive datetime."""
        broker_naive = datetime.fromtimestamp(int(epoch), UTC).replace(tzinfo=None)
        return broker_naive + timedelta(hours=3 - self.server_offset_hours)

    def _cancel_ticket(self, ticket: int, signal_key: str, action_name: str) -> bool:
        req = {"action": self.mt5.TRADE_ACTION_REMOVE, "order": ticket}
        res = self.mt5.order_send(req)
        success = bool(res is not None and res.retcode == self.mt5.TRADE_RETCODE_DONE)
        self._log_order_send(signal_key, action_name, req, res, success=success)
        return success

    def place_signal(self, signal, plan) -> ExecutionLog:
        """Place live-valid pending LIMIT orders without creating partial registry drift.

        If decide() or live bid/ask validation leaves only a subset of the
        strategy ladder placeable, this executor skips the whole signal.  The
        current registry stores one signal-level record, not a per-entry order
        manifest, so placing a partial ladder would let later live management
        replay unplaced entries.  Broker-side order_send failures are handled as
        all-or-nothing: already-created pendings are rolled back where possible.
        """
        if signal.signal_key in self._session_failed_signal_keys:
            return ExecutionLog()

        log = ExecutionLog()
        log.placed_entry_indices = []
        now_chart = _wall_clock_chart_now()

        replay_pos = getattr(plan, "replay_position", None)
        if replay_pos is not None and len(plan.orders) < len(replay_pos.entries):
            log.actions.append(
                f"Signal {signal.signal_key}: skipped partial placement "
                f"({len(plan.orders)} of {len(replay_pos.entries)} entries). "
                f"Live registry is signal-level, so partial ladders are skipped "
                f"to avoid managing unplaced entries."
            )
            return log

        activation_at = getattr(plan, "pending_activates_at", None)
        if activation_at is None:
            # Backwards-compatible fallback for old tests/tools that construct
            # NewSignalPlan directly. Runtime plans from the engine carry the
            # exact activation time derived from the active StrategyConfig.
            activation_at = signal.signal_time_chart + timedelta(
                minutes=DEFAULT_CONFIG.activation_delay_minutes
            )
        if now_chart < activation_at:
            if signal.signal_key not in self._session_skipped_inactive_signal_keys:
                wait_min = (activation_at - now_chart).total_seconds() / 60.0
                log.actions.append(
                    f"Signal {signal.signal_key}: waiting for activation "
                    f"(activates {activation_at:%Y-%m-%d %H:%M} GMT+3, "
                    f"now {now_chart:%Y-%m-%d %H:%M} GMT+3, "
                    f"{wait_min:.0f} min remaining)."
                )
                self._session_skipped_inactive_signal_keys.add(signal.signal_key)
            return log

        expires_at = getattr(plan, "pending_expires_at", None)
        if expires_at is not None and now_chart >= expires_at:
            if signal.signal_key not in self._session_skipped_expired_signal_keys:
                minutes_past = (now_chart - expires_at).total_seconds() / 60.0
                log.actions.append(
                    f"Signal {signal.signal_key}: skipped expired by wall-clock "
                    f"(expired {expires_at:%Y-%m-%d %H:%M} GMT+3, "
                    f"now {now_chart:%Y-%m-%d %H:%M} GMT+3, "
                    f"{minutes_past:.0f} min past expiry)."
                )
                self._session_skipped_expired_signal_keys.add(signal.signal_key)
            return log

        tick = self.mt5.symbol_info_tick(self.symbol)
        if tick is None or tick.bid <= 0 or tick.ask <= 0:
            log.actions.append(
                f"Signal {signal.signal_key}: skipped placement because no live "
                f"bid/ask tick is available for {self.symbol}."
            )
            return log

        bid = float(tick.bid)
        ask = float(tick.ask)
        stale_entries = []
        for order in plan.orders:
            key = signal_entry_key(signal.signal_key, order.entry_index)
            price = float(order.entry_price)
            stale_reason = None
            if signal.side == "BUY" and price >= ask:
                stale_reason = f"stale BUY LIMIT {price:g} >= live ask {ask:g}"
            elif signal.side == "SELL" and price <= bid:
                stale_reason = f"stale SELL LIMIT {price:g} <= live bid {bid:g}"

            if stale_reason is not None:
                stale_entries.append(key)
                if key not in self._session_skipped_stale_entries:
                    log.actions.append(f"  {key}: skipped {stale_reason}")
                    self._session_skipped_stale_entries.add(key)

        if stale_entries:
            log.actions.append(
                f"Signal {signal.signal_key}: skipped entire ladder because "
                f"{len(stale_entries)} entr{'y was' if len(stale_entries) == 1 else 'ies were'} "
                f"not broker-placeable at the live bid/ask."
            )
            return log

        rounded_lots: dict[int, float] = {}
        for order in plan.orders:
            lot = round_lot(order.lot, self.min_lot, self.lot_step)
            if lot <= 0:
                log.actions.append(
                    f"Signal {signal.signal_key}: skipped entire ladder because "
                    f"entry #{order.entry_index} computed lot {order.lot:.4f} "
                    f"below broker minimum {self.min_lot}."
                )
                return log
            rounded_lots[order.entry_index] = lot

        magic = signal_to_magic(signal.signal_key)
        if self.find_orders(magic) or self.find_positions(magic):
            log.actions.append(
                f"Signal {signal.signal_key} already has MT5 orders/positions; "
                f"skipping placement (will manage instead)."
            )
            return log
        if self._magic_already_traded(magic, signal.signal_time_chart):
            if signal.signal_key not in self._session_skipped_traded_signal_keys:
                log.actions.append(
                    f"Signal {signal.signal_key} already traded this session "
                    f"(closed deals in MT5 history for its magic); skipping "
                    f"re-placement so a finished signal is never run twice."
                )
                self._session_skipped_traded_signal_keys.add(signal.signal_key)
            return log

        order_type = (self.mt5.ORDER_TYPE_BUY_LIMIT if signal.side == "BUY"
                      else self.mt5.ORDER_TYPE_SELL_LIMIT)
        sym = self._sym_info if self._sym_info is not None else self.mt5.symbol_info(self.symbol)
        digits = sym.digits
        place_failures: list[tuple[int, float, str]] = []
        placed_tickets: list[int] = []
        placed_details: list[dict] = []

        for o in plan.orders:
            lot = rounded_lots[o.entry_index]
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
                # RETURN strands the triggered fill on FOK-only market-execution
                # brokers (retcode 10030); use the broker-supported mode instead.
                "type_filling": self._market_fill_mode(),
            }
            res = self.mt5.order_send(request)
            success = bool(res is not None and res.retcode == self.mt5.TRADE_RETCODE_DONE)
            self._log_order_send(signal.signal_key, "place_pending", request, res, success=success)
            if res is None:
                reason = str(self.mt5.last_error())
                log.actions.append(f"  #{o.entry_index}: FAILED order_send returned None: {reason}")
                place_failures.append((o.entry_index, o.entry_price, reason))
                break
            if res.retcode != self.mt5.TRADE_RETCODE_DONE:
                reason = f"retcode={res.retcode} comment={res.comment!r}"
                log.actions.append(f"  #{o.entry_index}: FAILED {reason}")
                place_failures.append((o.entry_index, o.entry_price, reason))
                break

            ticket = int(res.order)
            placed_tickets.append(ticket)
            placed_details.append({
                "entry_index": o.entry_index, "ticket": ticket,
                "price": request["price"], "lot": lot,
                "sl": request["sl"], "tp": request["tp"],
            })
            log.placed += 1
            log.placed_entry_indices.append(o.entry_index)
            log.actions.append(
                f"  {entry_key}: placed ticket={ticket} comment={comment} "
                f"@ {request['price']:g} lot={lot} "
                f"SL={request['sl']:g} TP={request['tp']:g}"
            )

        if self.notifier is not None and not place_failures and placed_details:
            self.notifier.order_placed(
                signal_key=signal.signal_key, side=signal.side,
                order_kind=f"{signal.side} LIMIT", placed=placed_details,
            )

        if place_failures:
            if self.notifier is not None:
                self.notifier.place_failed(
                    signal_key=signal.signal_key,
                    side=signal.side,
                    failures=place_failures,
                )
            self._session_failed_signal_keys.add(signal.signal_key)
            if placed_tickets:
                rollback_failed: list[int] = []
                for ticket in placed_tickets:
                    if self._cancel_ticket(ticket, signal.signal_key, "rollback_partial_place"):
                        log.cancelled += 1
                        log.actions.append(
                            f"  Rolled back partial placement ticket={ticket} "
                            f"({signal.signal_key})"
                        )
                    else:
                        rollback_failed.append(ticket)
                        log.actions.append(
                            f"  FAILED rollback for partial placement ticket={ticket} "
                            f"({signal.signal_key}); live footprint remains"
                        )
                if not rollback_failed:
                    log.placed = 0
                    log.placed_entry_indices = []
                    log.actions.append(
                        f"Signal {signal.signal_key}: partial placement rolled back; "
                        f"no registry entry should be recorded."
                    )
            log.actions.append(
                f"Signal {signal.signal_key}: placement failed; skipped further "
                f"retries in this Auto run. Restart Auto to retry manually."
            )
        return log

    def _map_position_to_entry_index(self, p, used: set[int], entry_count: int) -> Optional[int]:
        idx = _entry_index_from_comment(getattr(p, "comment", None))
        if idx is None or idx >= entry_count or idx in used:
            idx = next((i for i in range(entry_count) if i not in used), None)
        return idx

    def reconcile_with_mt5(
            self, engine_pos: Position, config: StrategyConfig,
            chart, now: datetime,
    ) -> ExecutionLog:
        """Patch replay entries from actual MT5 positions using comment suffixes.

        The base executor maps positions by chronological order.  This wrapper
        uses the entry suffix embedded in MT5 comments (``.1``, ``.2``, ``.3``)
        so live fills are reconciled to the same ladder slot that was sent to
        the broker.  It also allows MT5 positions to revive a replay NO_FILL,
        because a live position is the source of truth for actual execution.
        """
        log = ExecutionLog()
        magic = signal_to_magic(engine_pos.signal.signal_key)
        signal_key = engine_pos.signal.signal_key
        mt5_positions = sorted(self.find_positions(magic), key=lambda p: getattr(p, "time", 0))
        if not mt5_positions:
            return log
        if len(mt5_positions) > len(engine_pos.entries):
            msg = (f"Magic {magic} ({signal_key}): MT5 has {len(mt5_positions)} "
                   f"positions but engine has only {len(engine_pos.entries)} entry slots. "
                   f"Skipping reconciliation to avoid mis-mapping.")
            log.warnings.append(msg)
            if self.forensic is not None:
                self.forensic.reconcile_skipped(
                    signal_key=signal_key, reason=msg,
                    mt5_count=len(mt5_positions), engine_count=len(engine_pos.entries),
                )
            return log

        used: set[int] = set()
        earliest_patched: Optional[datetime] = None
        for mt5_pos in mt5_positions:
            idx = self._map_position_to_entry_index(mt5_pos, used, len(engine_pos.entries))
            if idx is None:
                continue
            used.add(idx)
            entry = engine_pos.entries[idx]
            if entry.status in _REPLAY_CLOSED_STATUSES:
                if self.forensic is not None:
                    self.forensic.reconcile_skipped(
                        signal_key=signal_key,
                        reason=f"slot {idx} terminal in replay (status={entry.status})",
                        entry_index=idx,
                        entry_status=entry.status,
                        mt5_ticket=int(mt5_pos.ticket),
                        mt5_price_open=float(mt5_pos.price_open),
                    )
                continue

            fill_time_chart = self._broker_epoch_to_chart_time(mt5_pos.time)
            actual_price = float(mt5_pos.price_open)
            actual_lot = float(mt5_pos.volume)
            before_status = entry.status
            before_price = entry.entry_price

            needs_patch = (
                    entry.status in ("PENDING", "NO_FILL")
                    or entry.fill_time != fill_time_chart
                    or abs(entry.entry_price - actual_price) > 1e-9
                    or abs(entry.lot - actual_lot) > 1e-9
            )
            if not needs_patch:
                continue

            announce_sig = (
                f"{signal_key}|{idx}|{actual_price:.5f}|{actual_lot:.5f}|"
                f"{fill_time_chart:%Y-%m-%d %H:%M:%S}"
            )
            if announce_sig not in self._session_announced_reconciles:
                self._session_announced_reconciles.add(announce_sig)
                log.actions.append(
                    f"  Reconciled #{idx} ({signal_key}): MT5 fill at "
                    f"{actual_price:g} lot={actual_lot:.2f} at "
                    f"{fill_time_chart:%Y-%m-%d %H:%M:%S} GMT+3 "
                    f"(engine had {before_status} at {before_price:g})"
                )
            if self.forensic is not None:
                self.forensic.reconcile_action(
                    signal_key=signal_key,
                    entry_index=idx,
                    before_status=before_status,
                    after_status="OPEN",
                    mt5_ticket=int(mt5_pos.ticket),
                    fill_price=actual_price,
                    fill_time=fill_time_chart,
                    lot=actual_lot,
                    planned_price=before_price,
                )

            entry.status = "OPEN"
            entry.fill_time = fill_time_chart
            entry.entry_price = actual_price
            entry.lot = actual_lot

            trailing_open = float(getattr(config, "trailing_open_distance", 0.0) or 0.0)
            if before_status in ("PENDING", "NO_FILL") and trailing_open > 0:
                # A trailing-open STOP fill is the trigger event the operator is
                # watching for; surface it explicitly (deduped), not just as the
                # generic reconcile line.
                trigger_sig = f"{signal_key}|{idx}"
                if trigger_sig not in self._session_announced_triggers:
                    self._session_announced_triggers.add(trigger_sig)
                    log.actions.append(
                        f"  TRAILING-OPEN TRIGGERED #{idx} ({signal_key}): "
                        f"{engine_pos.signal.side} filled at {actual_price:g}, "
                        f"SL {float(mt5_pos.sl):g}"
                    )
            if before_status in ("PENDING", "NO_FILL") and self.notifier is not None:
                if trailing_open > 0:
                    self.notifier.trailing_open_filled(
                        signal_key=signal_key, side=engine_pos.signal.side,
                        entry_index=idx, ticket=int(mt5_pos.ticket),
                        fill_price=actual_price,
                    )
                else:
                    self.notifier.entry_filled(
                        signal_key=signal_key, side=engine_pos.signal.side,
                        entry_index=idx, fill_price=actual_price,
                        source="MT5 reconcile", ticket=int(mt5_pos.ticket),
                    )
            if earliest_patched is None or fill_time_chart < earliest_patched:
                earliest_patched = fill_time_chart

        if earliest_patched is None:
            return log

        fill_times = [e.fill_time for e in engine_pos.entries if e.fill_time is not None]
        if fill_times:
            engine_pos.first_fill_time = min(fill_times)
            engine_pos.time_exit_deadline = (
                    engine_pos.first_fill_time + timedelta(minutes=config.max_hold_minutes)
            )

        advance_bars(engine_pos, chart.bars_between(earliest_patched, now), config)
        return log

    def _cancel_orders(self, magic: int, signal_key: str, action_name: str,
                       message_prefix: str) -> ExecutionLog:
        log = ExecutionLog()
        cancel_failures: list[tuple[int, str]] = []
        cancelled: list[dict] = []
        for order in self.find_orders(magic):
            if self._cancel_ticket(int(order.ticket), signal_key, action_name):
                log.cancelled += 1
                log.actions.append(f"  {message_prefix} #{order.ticket} ({signal_key})")
                cancelled.append({"ticket": int(order.ticket), "reason": message_prefix})
            else:
                log.actions.append(f"  FAILED to cancel pending #{order.ticket}")
                cancel_failures.append((int(order.ticket), "order_remove failed"))
        side = ""  # cancels are signal-level; side is informational only here
        if self.notifier is not None and cancelled:
            self.notifier.pending_cancelled(signal_key=signal_key, side=side, cancelled=cancelled)
        if self.notifier is not None and cancel_failures:
            self.notifier.cancel_failed(signal_key=signal_key, side=side, failures=cancel_failures)
        return log

    def _cancel_pending_expired_by_wall_clock(self, engine_pos: Position) -> ExecutionLog:
        """Cancel live pending orders once real chart time passes strategy expiry."""
        now_chart = _wall_clock_chart_now()
        if now_chart <= engine_pos.expiry_time:
            return ExecutionLog()
        magic = signal_to_magic(engine_pos.signal.signal_key)
        signal_key = engine_pos.signal.signal_key
        return self._cancel_orders(
            magic,
            signal_key,
            "cancel_pending_wall_clock_expired",
            "Cancelled wall-clock expired pending",
        )

    def _position_entry_pairs(self, engine_pos: Position, magic: int) -> list[tuple[object, object]]:
        """Map MT5 positions to engine entries, preferring the comment suffix."""
        mt5_positions = sorted(self.find_positions(magic), key=lambda p: getattr(p, "time", 0))
        used: set[int] = set()
        pairs = []
        for p in mt5_positions:
            idx = self._map_position_to_entry_index(p, used, len(engine_pos.entries))
            if idx is None:
                continue
            used.add(idx)
            pairs.append((p, engine_pos.entries[idx]))
        return pairs

    def _close_position(self, p, magic: int, signal_key: str, action_name: str,
                        reason_label: str, log: ExecutionLog,
                        closed: list[tuple[int, float]],
                        failed: list[tuple[int, str]]) -> None:
        tick = self.mt5.symbol_info_tick(self.symbol)
        if tick is None:
            log.actions.append(f"  {reason_label} on #{p.ticket}: no tick available, skipping")
            failed.append((p.ticket, "no tick available"))
            return
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
            "comment":      f"{signal_key}/{action_name}"[:31],
            "deviation":    self.CLOSE_DEVIATION_POINTS,
            "type_filling": self._market_fill_mode(),
        }
        res = self.mt5.order_send(req)
        success = bool(res is not None and res.retcode == self.mt5.TRADE_RETCODE_DONE)
        self._log_order_send(signal_key, action_name, req, res, success=success)
        if success:
            log.closed += 1
            log.actions.append(f"  {reason_label} closed #{p.ticket} @ {price:g} ({signal_key})")
            closed.append((p.ticket, price))
        else:
            reason = str(res.comment if res else self.mt5.last_error())
            log.actions.append(f"  FAILED {reason_label} close on #{p.ticket}: {reason}")
            failed.append((p.ticket, reason))

    @staticmethod
    def _lock_improves(side: str, current_sl: float, target_sl: float, tolerance: float) -> bool:
        """True only when moving the protective stop to target_sl tightens it.

        A lock must never push a stop backwards: for BUY the stop may only rise
        toward TP, for SELL only fall.  If the live SL is already at or past the
        lock target (a prior trailing move or a manual edit), no modify is sent so
        the executor never loosens broker-side protection.
        """
        if side == "BUY":
            return target_sl > current_sl + tolerance
        return target_sl < current_sl - tolerance

    def _modify_stop(self, p, sl: float, signal_key: str, action_name: str,
                     label: str, log: ExecutionLog,
                     locked: list[int], failed: list[tuple[int, str]]) -> None:
        req = {"action": self.mt5.TRADE_ACTION_SLTP, "position": p.ticket, "sl": sl, "tp": p.tp}
        res = self.mt5.order_send(req)
        success = bool(res is not None and res.retcode == self.mt5.TRADE_RETCODE_DONE)
        self._log_order_send(signal_key, action_name, req, res, success=success)
        if success:
            log.modified += 1
            locked.append(p.ticket)
            log.actions.append(f"  Locked SL on #{p.ticket} to {label} {sl:g} ({signal_key})")
        else:
            reason = str(res.comment if res else self.mt5.last_error())
            failed.append((p.ticket, reason))
            log.actions.append(f"  FAILED {label} SL-lock on #{p.ticket}: {reason}")

    def _late_lock_or_close(self, p, magic: int, signal_key: str, side: str,
                            target_level: float, label: str, log: ExecutionLog,
                            locked: list[int],
                            closed: list[tuple[int, float]],
                            failed: list[tuple[int, str]]) -> None:
        """Protect a leg the replay already lock-exited; close only as last resort.

        The replay says this leg's lifecycle ended at `target_level` (LOCK_TP1 /
        LOCK_TP2), but the live SL never made it there in time. Behavior:

        * price still on the profitable side of the level -> SL moves to the
          exact level; the broker then exits at model parity (or the leg keeps
          running and beats the model — accepted, profit is never given up);
        * price already back through the level -> SL locks at the closest legal
          level (`LATE_LOCK_FALLBACK_BUFFER` off the live bid/ask) and later
          cycles ratchet it toward the true level as price recovers, in steps of
          at least `LATE_LOCK_MIN_STEP`, never backwards (`_lock_improves`);
        * no legal protective stop exists, or the modify is rejected -> close at
          market (the old catch-up behavior), because an unprotected leg riding
          to its original SL is the one outcome that must never happen.
        """
        sym = self._sym_info if self._sym_info is not None else self.mt5.symbol_info(self.symbol)
        digits = sym.digits
        tolerance = 10 ** (-digits)
        tick = self.mt5.symbol_info_tick(self.symbol)
        if tick is None or getattr(tick, "bid", 0) <= 0 or getattr(tick, "ask", 0) <= 0:
            log.actions.append(
                f"  Late {label} lock on #{p.ticket}: no tick available, skipping"
            )
            failed.append((p.ticket, "no tick available"))
            return

        target = round(float(target_level), digits)
        if side == "BUY":
            desired = min(target, float(tick.bid) - LATE_LOCK_FALLBACK_BUFFER)
        else:
            desired = max(target, float(tick.ask) + LATE_LOCK_FALLBACK_BUFFER)
        legal = clamp_sltp_sl(self, p, desired)
        if legal is None:
            log.actions.append(
                f"  Late {label} lock on #{p.ticket}: no broker-legal stop near "
                f"{desired:g}; closing at market instead ({signal_key})"
            )
            self._close_position(
                p, magic, signal_key, f"late-{label.lower()}",
                f"Late {label} catch-up", log, closed, failed,
            )
            return

        current_sl = float(getattr(p, "sl", 0.0) or 0.0)
        if not self._lock_improves(side, current_sl, legal, tolerance):
            return  # already protected at or beyond this level
        at_target = abs(legal - target) <= tolerance
        if not at_target and abs(legal - current_sl) < LATE_LOCK_MIN_STEP:
            return  # fallback ratchet too small this cycle; retry later

        modify_failed: list[tuple[int, str]] = []
        self._modify_stop(
            p, legal, signal_key, f"late_lock_{label.lower()}",
            f"late {label} lock" if at_target else f"late {label} fallback lock",
            log, locked, modify_failed,
        )
        if modify_failed:
            # Could not protect the leg broker-side: fall back to the old
            # catch-up close rather than leave it exposed to the original SL.
            self._close_position(
                p, magic, signal_key, f"late-{label.lower()}",
                f"Late {label} catch-up", log, closed, failed,
            )

    def _magic_already_traded(self, magic: int, since_chart: datetime) -> bool:
        """True when MT5 deal history already shows fills for this magic.

        Guards the fresh-placement path against re-trading a signal whose live
        lifecycle has fully closed (registry pruned, no live footprint left):
        the replay considers it finished, so placing the ladder again would
        double the exposure — signal 2026-06-12#10 was traded twice this way.
        Best-effort: a stub/old MT5 build without history_deals_get, or a
        history call failure, means no block (same behavior as before).
        """
        getter = getattr(self.mt5, "history_deals_get", None)
        if getter is None:
            return False
        try:
            deals = getter(
                since_chart - timedelta(hours=1),
                _wall_clock_chart_now() + timedelta(days=1),
            )
        except Exception:
            return False
        return any(int(getattr(d, "magic", -1) or -1) == magic for d in (deals or []))

    def reopen_missing_open_positions(self, engine_pos: Position,
                                      config: StrategyConfig) -> ExecutionLog:
        """Re-open live positions for entries the replay still holds OPEN.

        MT5 mirrors the replay (operator decision, 2026-06-12 reconciliation):
        when a leg the model still holds is missing from MT5 — typically closed
        by hand to thin out exposure — it is re-opened at market with the
        replay's lot, its current effective stop, and the leg's target, under
        the same per-entry comment so reconciliation re-attaches to the slot.
        Runs every cycle while the replay keeps the leg open, so a hand-closed
        leg comes back within one watch interval; once the replay exits the
        leg, re-opening stops on its own.
        """
        log = ExecutionLog()
        open_entries = [e for e in engine_pos.entries if e.status == "OPEN"]
        if not open_entries:
            return log
        wall_clock_now = _wall_clock_chart_now()
        if (engine_pos.time_exit_deadline is not None
                and wall_clock_now >= engine_pos.time_exit_deadline):
            return log  # time-exit cycle owns these legs; don't race it

        magic = signal_to_magic(engine_pos.signal.signal_key)
        signal_key = engine_pos.signal.signal_key
        side = engine_pos.signal.side
        paired_idx = {entry.entry_index for _p, entry in self._position_entry_pairs(engine_pos, magic)}
        order_comments = {str(getattr(o, "comment", "") or "") for o in self.find_orders(magic)}

        missing = [
            e for e in open_entries
            if e.entry_index not in paired_idx
            and mt5_entry_comment(signal_key, e.entry_index) not in order_comments
        ]
        if not missing:
            return log

        tick = self.mt5.symbol_info_tick(self.symbol)
        if tick is None or getattr(tick, "bid", 0) <= 0 or getattr(tick, "ask", 0) <= 0:
            log.actions.append(
                f"Signal {signal_key}: cannot re-open missing entries, no live tick."
            )
            return log
        sym = self._sym_info if self._sym_info is not None else self.mt5.symbol_info(self.symbol)
        digits = sym.digits

        reopened: list[dict] = []
        failed: list[tuple[int, float, str]] = []
        for entry in missing:
            lot = round_lot(entry.lot, self.min_lot, self.lot_step)
            if lot <= 0:
                log.actions.append(
                    f"  #{entry.entry_index}: replay lot {entry.lot:.4f} below broker "
                    f"minimum {self.min_lot}; cannot re-open"
                )
                continue
            if side == "BUY":
                order_type, price = self.mt5.ORDER_TYPE_BUY, float(tick.ask)
                sl = min(float(engine_pos.effective_stop_for(entry, config)),
                         float(tick.bid) - LATE_LOCK_FALLBACK_BUFFER)
            else:
                order_type, price = self.mt5.ORDER_TYPE_SELL, float(tick.bid)
                sl = max(float(engine_pos.effective_stop_for(entry, config)),
                         float(tick.ask) + LATE_LOCK_FALLBACK_BUFFER)
            tp = entry.target_price if entry.target_price is not None else engine_pos.target_level
            comment = mt5_entry_comment(signal_key, entry.entry_index)
            request = {
                "action":       self.mt5.TRADE_ACTION_DEAL,
                "symbol":       self.symbol,
                "volume":       lot,
                "type":         order_type,
                "price":        price,
                "sl":           round(sl, digits),
                "tp":           round(float(tp), digits),
                "magic":        magic,
                "comment":      comment,
                "deviation":    self.CLOSE_DEVIATION_POINTS,
                "type_filling": self._market_fill_mode(),
            }
            res = self.mt5.order_send(request)
            success = bool(res is not None and res.retcode == self.mt5.TRADE_RETCODE_DONE)
            self._log_order_send(signal_key, "reopen_missing_position", request, res, success=success)
            if success:
                ticket = int(getattr(res, "order", 0) or 0)
                log.placed += 1
                log.actions.append(
                    f"  Re-opened #{entry.entry_index} ({signal_key}) @ {price:g} "
                    f"lot={lot} SL={request['sl']:g} TP={request['tp']:g} "
                    f"(replay holds it OPEN; live position was missing)"
                )
                reopened.append({
                    "entry_index": entry.entry_index, "ticket": ticket,
                    "price": price, "lot": lot,
                    "sl": request["sl"], "tp": request["tp"],
                })
            else:
                reason = str(res.comment if res else self.mt5.last_error())
                log.actions.append(
                    f"  FAILED re-open of #{entry.entry_index} ({signal_key}): {reason}"
                )
                failed.append((entry.entry_index, price, reason))

        if self.notifier is not None and reopened:
            self.notifier.order_placed(
                signal_key=signal_key, side=side,
                order_kind=f"{side} MARKET (re-open)", placed=reopened,
            )
        if self.notifier is not None and failed:
            self.notifier.place_failed(
                signal_key=signal_key, side=side, failures=failed,
            )
        return log

    def manage_position(self, engine_pos: Position, config: StrategyConfig, chart_now):
        """Manage one tracked signal with per-entry stop-lock parity.

        The shared replay can now decide that only some entries are protected by
        a TP1/TP2 touch.  Live management must therefore modify/close only the MT5
        positions mapped to those protected entries, not every position sharing
        the same signal magic.  If replay says an entry is already terminal while
        MT5 still shows it open, close it at current market as a catch-up.
        """
        wall_clock_now = _wall_clock_chart_now()
        effective_chart_now = wall_clock_now if wall_clock_now > chart_now else chart_now

        log = self._cancel_pending_expired_by_wall_clock(engine_pos)
        magic = signal_to_magic(engine_pos.signal.signal_key)
        signal_key = engine_pos.signal.signal_key
        side = engine_pos.signal.side
        digits = self.mt5.symbol_info(self.symbol).digits
        tolerance = 10 ** (-digits)

        if effective_chart_now > engine_pos.expiry_time:
            log.merge(self._cancel_orders(
                magic,
                signal_key,
                "cancel_pending_expired",
                "Cancelled expired pending",
            ))

        pairs = self._position_entry_pairs(engine_pos, magic)

        terminal_closed: list[tuple[int, float]] = []
        terminal_failed: list[tuple[int, str]] = []
        tp1_catchup_closed: list[tuple[int, float]] = []
        tp1_catchup_failed: list[tuple[int, str]] = []
        tp2_catchup_closed: list[tuple[int, float]] = []
        tp2_catchup_failed: list[tuple[int, str]] = []
        tp1_late_locked: list[int] = []
        tp2_late_locked: list[int] = []
        active_pairs = []
        for p, entry in pairs:
            if entry.status not in _REPLAY_CLOSED_STATUSES:
                active_pairs.append((p, entry))
                continue
            # The replay already lock-exited these legs. Don't flatten at
            # whatever price the catch-up cycle happens to see (the 2026-06-12
            # reconciliation lost $468 to that): protect the leg with a stop at
            # the lock level — or the closest legal level, ratcheted toward it
            # later — and let the broker realize the model's exit (or better).
            if entry.status == "LOCK_TP1":
                self._late_lock_or_close(
                    p, magic, signal_key, side, engine_pos.signal.tp1, "TP1",
                    log, tp1_late_locked, tp1_catchup_closed, tp1_catchup_failed,
                )
            elif entry.status == "LOCK_TP2":
                self._late_lock_or_close(
                    p, magic, signal_key, side, engine_pos.signal.tp2, "TP2",
                    log, tp2_late_locked, tp2_catchup_closed, tp2_catchup_failed,
                )
            else:
                action_name = f"catchup-{entry.status.lower().replace('_', '-')}"
                self._close_position(
                    p, magic, signal_key, action_name, f"{entry.status} catch-up",
                    log, terminal_closed, terminal_failed,
                )
        pairs = active_pairs
        closed_tickets_this_cycle = {
            ticket for ticket, _price in (terminal_closed + tp1_catchup_closed + tp2_catchup_closed)
        }

        if self.notifier is not None and (tp1_catchup_closed or tp1_catchup_failed):
            backtest_lock_pnl = sum(e.pnl or 0.0 for e in engine_pos.entries if e.status == "LOCK_TP1")
            self.notifier.late_tp1_catchup(
                signal_key=signal_key, side=side,
                closed=tp1_catchup_closed, failed=tp1_catchup_failed,
                backtest_pnl=backtest_lock_pnl,
            )
        if self.notifier is not None and tp1_late_locked:
            self.notifier.tp1_lock(
                signal_key=signal_key, side=side,
                locked=tp1_late_locked, failed=[],
                sl=round(engine_pos.signal.tp1, digits),
            )
        if self.notifier is not None and tp2_late_locked:
            notify_tp2 = getattr(self.notifier, "tp2_lock", None)
            if callable(notify_tp2):
                notify_tp2(
                    signal_key=signal_key, side=side,
                    locked=tp2_late_locked, failed=[],
                    sl=round(engine_pos.signal.tp2, digits),
                )

        # TP1 SL-lock only for entries whose replay says TP1 applies and that are
        # still open in both replay and MT5.
        target_sl = round(engine_pos.signal.tp1, digits)
        lock_tickets: list[int] = []
        lock_failures: list[tuple[int, str]] = []
        for p, entry in pairs:
            if (
                    config.lock_after_tp1
                    and entry.status == "OPEN"
                    and engine_pos.lock_stage_for(entry, config.lock_after_tp1, config.lock_after_tp2) >= 1
                    and self._lock_improves(side, float(p.sl), target_sl, tolerance)
            ):
                self._modify_stop(
                    p, target_sl, signal_key, "modify_sl_to_tp1", "TP1",
                    log, lock_tickets, lock_failures,
                )

        if self.notifier is not None and (lock_tickets or lock_failures):
            self.notifier.tp1_lock(
                signal_key=signal_key, side=side,
                locked=lock_tickets, failed=lock_failures,
                sl=target_sl,
            )

        # Optional TP2 parity, only when the strategy enables it.
        if config.lock_after_tp2 and engine_pos.stage >= 2:
            tp2_sl = round(engine_pos.signal.tp2, digits)
            tp2_locked: list[int] = []
            tp2_lock_failed: list[tuple[int, str]] = []
            for p, entry in self._position_entry_pairs(engine_pos, magic):
                if int(p.ticket) in closed_tickets_this_cycle:
                    continue
                if (
                        entry.status == "OPEN"
                        and engine_pos.lock_stage_for(entry, config.lock_after_tp1, config.lock_after_tp2) >= 2
                        and self._lock_improves(side, float(p.sl), tp2_sl, tolerance)
                ):
                    self._modify_stop(
                        p, tp2_sl, signal_key, "modify_sl_to_tp2", "TP2",
                        log, tp2_locked, tp2_lock_failed,
                    )
            if self.notifier is not None and (tp2_locked or tp2_lock_failed):
                notify = getattr(self.notifier, "tp2_lock", None)
                if callable(notify):
                    notify(
                        signal_key=signal_key,
                        side=engine_pos.signal.side,
                        locked=tp2_locked,
                        failed=tp2_lock_failed,
                        sl=tp2_sl,
                    )

        # Time-exit closes all still-open live positions for the signal, except
        # active trend-runners that the shared engine explicitly allows to hold
        # past max-hold.  The paired timeout pending-cancel must be skipped too
        # so live and backtest keep the same signal lifecycle for this cycle.
        if (
                engine_pos.time_exit_deadline is not None
                and effective_chart_now >= engine_pos.time_exit_deadline
                and not should_skip_time_exit(engine_pos, config)
        ):
            timeout_closed: list[tuple[int, float]] = []
            timeout_failed: list[tuple[int, str]] = []
            for p in self.find_positions(magic):
                if int(p.ticket) in closed_tickets_this_cycle:
                    continue
                self._close_position(
                    p, magic, signal_key, "timeout", "Time-exit",
                    log, timeout_closed, timeout_failed,
                )
            if self.notifier is not None:
                self.notifier.time_exit(
                    signal_key=signal_key, side=side,
                    closed=timeout_closed, failed=timeout_failed,
                )
            log.merge(self._cancel_orders(
                magic,
                signal_key,
                "cancel_after_timeout",
                "Cancelled pending after timeout",
            ))

        return log