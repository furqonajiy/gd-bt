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

from xauusd_trading import CHART_TIMEZONE_OFFSET, DEFAULT_CONFIG, Position, StrategyConfig

from .mt5_executor import (
    ExecutionLog,
    Mt5Executor as _BaseMt5Executor,
    mt5_entry_comment,
    round_lot,
    signal_entry_key,
    signal_to_magic,
)


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

    def place_signal(self, signal, plan) -> ExecutionLog:
        """Place live-valid pending LIMIT orders without creating partial registry drift.

        If decide() or live bid/ask validation leaves only a subset of the
        strategy ladder placeable, this executor skips the whole signal.  The
        current registry stores one signal-level record, not a per-entry order
        manifest, so placing a partial ladder would let later live management
        replay unplaced entries.  Skipping the partial ladder is conservative but
        keeps live execution as close as possible to the shared replay model.
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

        magic = signal_to_magic(signal.signal_key)
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
            success = bool(res is not None and res.retcode == self.mt5.TRADE_RETCODE_DONE)
            self._log_order_send(signal.signal_key, "place_pending", request, res, success=success)
            if res is None:
                reason = str(self.mt5.last_error())
                log.actions.append(f"  #{o.entry_index}: FAILED order_send returned None: {reason}")
                place_failures.append((o.entry_index, o.entry_price, reason))
            elif res.retcode != self.mt5.TRADE_RETCODE_DONE:
                reason = f"retcode={res.retcode} comment={res.comment!r}"
                log.actions.append(f"  #{o.entry_index}: FAILED {reason}")
                place_failures.append((o.entry_index, o.entry_price, reason))
            else:
                log.placed += 1
                log.placed_entry_indices.append(o.entry_index)
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
        if log.placed == 0 and place_failures:
            self._session_failed_signal_keys.add(signal.signal_key)
            log.actions.append(
                f"Signal {signal.signal_key}: placement failed; skipped further "
                f"retries in this Auto run. Restart Auto to retry manually."
            )
        return log

    def _cancel_orders(self, magic: int, signal_key: str, action_name: str,
                       message_prefix: str) -> ExecutionLog:
        log = ExecutionLog()
        cancel_failures: list[tuple[int, str]] = []
        for order in self.find_orders(magic):
            req = {"action": self.mt5.TRADE_ACTION_REMOVE, "order": order.ticket}
            res = self.mt5.order_send(req)
            success = bool(res is not None and res.retcode == self.mt5.TRADE_RETCODE_DONE)
            self._log_order_send(signal_key, action_name, req, res, success=success)
            if success:
                log.cancelled += 1
                log.actions.append(f"  {message_prefix} #{order.ticket} ({signal_key})")
            else:
                reason = str(res.comment if res else self.mt5.last_error())
                log.actions.append(f"  FAILED to cancel pending #{order.ticket}: {reason}")
                cancel_failures.append((order.ticket, reason))
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
            (
                f"Cancelled wall-clock expired pending"
            ),
        )

    def _position_entry_pairs(self, engine_pos: Position, magic: int) -> list[tuple[object, object]]:
        """Map MT5 positions to engine entries, preferring the comment suffix."""
        mt5_positions = sorted(self.find_positions(magic), key=lambda p: getattr(p, "time", 0))
        used: set[int] = set()
        pairs = []
        for p in mt5_positions:
            idx = _entry_index_from_comment(getattr(p, "comment", None))
            if idx is None or idx >= len(engine_pos.entries) or idx in used:
                idx = next((i for i in range(len(engine_pos.entries)) if i not in used), None)
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

    def manage_position(self, engine_pos: Position, config: StrategyConfig, chart_now):
        """Manage one tracked signal with per-entry stop-lock parity.

        The shared replay can now decide that only some entries are protected by
        a TP1/TP2 touch.  Live management must therefore modify/close only the MT5
        positions mapped to those protected entries, not every position sharing
        the same signal magic.
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

        # TP1 catch-up/lock only for entries whose replay says TP1 applies.
        target_sl = round(engine_pos.signal.tp1, digits)
        catchup_closed: list[tuple[int, float]] = []
        catchup_failed: list[tuple[int, str]] = []
        lock_tickets: list[int] = []
        lock_failures: list[tuple[int, str]] = []
        backtest_lock_pnl = sum(e.pnl or 0.0 for e in engine_pos.entries if e.status == "LOCK_TP1")
        for p, entry in pairs:
            if entry.status == "LOCK_TP1" and abs(p.sl - target_sl) > tolerance:
                self._close_position(
                    p, magic, signal_key, "late-tp1", "Late TP1 catch-up",
                    log, catchup_closed, catchup_failed,
                )
                continue
            if (
                config.lock_after_tp1
                and entry.status == "OPEN"
                and engine_pos.lock_stage_for(entry, config.lock_after_tp1, config.lock_after_tp2) >= 1
                and abs(p.sl - target_sl) > tolerance
            ):
                self._modify_stop(
                    p, target_sl, signal_key, "modify_sl_to_tp1", "TP1",
                    log, lock_tickets, lock_failures,
                )

        if self.notifier is not None and (catchup_closed or catchup_failed):
            self.notifier.late_tp1_catchup(
                signal_key=signal_key, side=side,
                closed=catchup_closed, failed=catchup_failed,
                backtest_pnl=backtest_lock_pnl,
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
            tp2_closed: list[tuple[int, float]] = []
            tp2_failed_close: list[tuple[int, str]] = []
            tp2_locked: list[int] = []
            tp2_lock_failed: list[tuple[int, str]] = []
            for p, entry in self._position_entry_pairs(engine_pos, magic):
                if entry.status == "LOCK_TP2" and abs(p.sl - tp2_sl) > tolerance:
                    self._close_position(
                        p, magic, signal_key, "late-tp2", "Late TP2 catch-up",
                        log, tp2_closed, tp2_failed_close,
                    )
                    continue
                if (
                    entry.status == "OPEN"
                    and engine_pos.lock_stage_for(entry, config.lock_after_tp1, config.lock_after_tp2) >= 2
                    and abs(p.sl - tp2_sl) > tolerance
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

        # Time-exit closes all still-open live positions for the signal.
        if (
            engine_pos.time_exit_deadline is not None
            and effective_chart_now >= engine_pos.time_exit_deadline
        ):
            timeout_closed: list[tuple[int, float]] = []
            timeout_failed: list[tuple[int, str]] = []
            for p in self.find_positions(magic):
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
