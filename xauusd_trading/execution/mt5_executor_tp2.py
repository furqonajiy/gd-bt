"""TP2-aware MT5 executor wrapper.

The base MT5 executor places orders, reconciles fills, cancels expired pendings,
locks to TP1, and handles time exit. This wrapper adds live-only parity safety
checks used by the public executor:

* skip expired signals by wall-clock chart time before order_send;
* cancel already-placed pending LIMITs by wall-clock chart expiry;
* use wall-clock chart time for live manage deadlines when MT5 bars lag;
* skip stale/marketable pending LIMITs before order_send; and
* optionally apply TP2 stop-lock parity when the strategy enables TP2 locking.

DD40 currently uses ``lock_after_tp2=False``, so TP2 locking remains disabled
unless a config explicitly enables it.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from xauusd_trading import CHART_TIMEZONE_OFFSET, Position, StrategyConfig

from .mt5_executor import (
    ExecutionLog,
    Mt5Executor as _BaseMt5Executor,
    signal_entry_key,
    signal_to_magic,
)


def _wall_clock_chart_now() -> datetime:
    """Return real wall-clock time in the chart timezone (GMT+3)."""
    return datetime.utcnow() + timedelta(hours=CHART_TIMEZONE_OFFSET)


class Mt5Executor(_BaseMt5Executor):
    """Public MT5 executor with live parity guards."""

    # Process-local guards. Auto creates a fresh executor every cycle, so these
    # class-level sets prevent repeated logs for the same stale entry, expired
    # signal, or zero-placement broker failure every watch interval.
    _session_skipped_stale_entries: set[str] = set()
    _session_skipped_expired_signal_keys: set[str] = set()
    _session_failed_signal_keys: set[str] = set()

    def place_signal(self, signal, plan) -> ExecutionLog:
        """Place only live-valid pending LIMIT orders.

        Backtest replay can leave an entry as PENDING because it was never
        touch-filled historically. In live Auto, that does not always mean the
        broker can still accept the pending order now. MT5 rejects marketable
        LIMIT orders, for example a SELL LIMIT below current Bid or a BUY LIMIT
        above current Ask. Skip those before order_send so we do not spam MT5
        with repeated retcode=10015 Invalid price requests.

        Placement expiry is checked against wall-clock chart time so stale MT5
        M1 history cannot keep old provider signals alive past the 630-minute
        DD40 pending window.
        """
        if signal.signal_key in self._session_failed_signal_keys:
            return ExecutionLog()

        log = ExecutionLog()
        expires_at = getattr(plan, "pending_expires_at", None)
        now_chart = _wall_clock_chart_now()
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
        valid_orders = []
        for order in plan.orders:
            key = signal_entry_key(signal.signal_key, order.entry_index)
            price = float(order.entry_price)
            stale_reason = None
            if signal.side == "BUY" and price >= ask:
                stale_reason = f"stale BUY LIMIT {price:g} >= live ask {ask:g}"
            elif signal.side == "SELL" and price <= bid:
                stale_reason = f"stale SELL LIMIT {price:g} <= live bid {bid:g}"

            if stale_reason is None:
                valid_orders.append(order)
                continue

            if key not in self._session_skipped_stale_entries:
                log.actions.append(f"  {key}: skipped {stale_reason}")
                self._session_skipped_stale_entries.add(key)

        if not valid_orders:
            return log

        original_orders = plan.orders
        plan.orders = valid_orders
        try:
            place_log = super().place_signal(signal, plan)
        finally:
            plan.orders = original_orders

        log.merge(place_log)
        if place_log.placed == 0 and (place_log.actions or place_log.warnings):
            self._session_failed_signal_keys.add(signal.signal_key)
            log.actions.append(
                f"Signal {signal.signal_key}: placement failed; skipped further "
                f"retries in this Auto run. Restart Auto to retry manually."
            )
        return log

    def _cancel_pending_expired_by_wall_clock(self, engine_pos: Position) -> ExecutionLog:
        """Cancel live pending orders once real chart time passes strategy expiry.

        The base executor cancels expired pendings using ``chart_now`` from the
        latest MT5 M1 bar. If the feed stalls after order placement, that bar
        time can lag behind the true DD40 pending expiry. This live-only guard
        keeps MT5 GTC pending orders aligned with the backtest's 630-minute
        pending window even during broker/session data gaps.
        """
        log = ExecutionLog()
        now_chart = _wall_clock_chart_now()
        if now_chart <= engine_pos.expiry_time:
            return log

        magic = signal_to_magic(engine_pos.signal.signal_key)
        signal_key = engine_pos.signal.signal_key
        cancel_failures: list[tuple[int, str]] = []
        for order in self.find_orders(magic):
            req = {"action": self.mt5.TRADE_ACTION_REMOVE, "order": order.ticket}
            res = self.mt5.order_send(req)
            success = bool(res is not None and res.retcode == self.mt5.TRADE_RETCODE_DONE)
            self._log_order_send(signal_key, "cancel_pending_wall_clock_expired", req, res, success=success)
            if success:
                log.cancelled += 1
                log.actions.append(
                    f"  Cancelled wall-clock expired pending #{order.ticket} "
                    f"({signal_key}; expired {engine_pos.expiry_time:%Y-%m-%d %H:%M} "
                    f"GMT+3, now {now_chart:%Y-%m-%d %H:%M} GMT+3)"
                )
            else:
                reason = str(res.comment if res else self.mt5.last_error())
                log.actions.append(
                    f"  FAILED to cancel wall-clock expired pending #{order.ticket}: {reason}"
                )
                cancel_failures.append((order.ticket, reason))

        if self.notifier is not None and cancel_failures:
            self.notifier.cancel_failed(
                signal_key=signal_key,
                side=engine_pos.signal.side,
                failures=cancel_failures,
            )
        return log

    def manage_position(self, engine_pos: Position, config: StrategyConfig, chart_now):
        """Manage one tracked signal, including optional TP2 SL-lock parity.

        The base executor handles chart-replay expiry, reconciliation-dependent
        TP1 lock, late TP1 catch-up, and time exit. This wrapper first applies a
        wall-clock pending-expiry guard for live GTC orders, then passes the
        later of chart time and wall-clock chart time into base management so
        live expiry/time-exit deadlines are not delayed by a stale M1 feed.
        TP2 protection is applied only when ``config.lock_after_tp2`` is enabled.
        """
        wall_clock_now = _wall_clock_chart_now()
        effective_chart_now = wall_clock_now if wall_clock_now > chart_now else chart_now

        log = self._cancel_pending_expired_by_wall_clock(engine_pos)
        base_log = super().manage_position(engine_pos, config, effective_chart_now)
        log.merge(base_log)

        if not config.lock_after_tp2 or engine_pos.stage < 2:
            return log

        magic = signal_to_magic(engine_pos.signal.signal_key)
        signal_key = engine_pos.signal.signal_key
        digits = self.mt5.symbol_info(self.symbol).digits
        tp2_sl = round(engine_pos.signal.tp2, digits)
        tolerance = 10 ** (-digits)

        # If the backtest/replay says an entry already exited at LOCK_TP2 but
        # MT5 still shows an open position, close it at market as a catch-up.
        # This mirrors the existing late TP1 catch-up safety behavior.
        if any(e.status == "LOCK_TP2" for e in engine_pos.entries):
            unlocked = [
                p for p in self.find_positions(magic)
                if abs(p.sl - tp2_sl) > tolerance
            ]
            backtest_lock_pnl = sum(
                e.pnl or 0.0
                for e in engine_pos.entries
                if e.status == "LOCK_TP2"
            )
            for p in unlocked:
                tick = self.mt5.symbol_info_tick(self.symbol)
                if tick is None:
                    log.actions.append(
                        f"  Late TP2 catch-up on #{p.ticket}: no tick available, skipping"
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
                    "comment":      f"{signal_key}/late-tp2"[:31],
                    "deviation":    self.CLOSE_DEVIATION_POINTS,
                    "type_filling": self._market_fill_mode(),
                }
                res = self.mt5.order_send(req)
                success = bool(
                    res is not None and res.retcode == self.mt5.TRADE_RETCODE_DONE
                )
                self._log_order_send(signal_key, "close_catchup_tp2", req, res, success=success)
                if success:
                    log.closed += 1
                    log.actions.append(
                        f"  Late TP2 catch-up closed #{p.ticket} @ {price:g} "
                        f"({signal_key}; backtest LOCK_TP2 would have realized "
                        f"${backtest_lock_pnl:+.2f} -- actual close at current market)"
                    )
                else:
                    reason = str(res.comment if res else self.mt5.last_error())
                    log.actions.append(
                        f"  FAILED late TP2 catch-up close on #{p.ticket}: {reason}"
                    )

        # For remaining open positions, move broker SL to TP2. This is the
        # direct live equivalent of Position.effective_stop_for(... stage >= 2).
        locked_tickets: list[int] = []
        lock_failures: list[tuple[int, str]] = []
        for p in self.find_positions(magic):
            if abs(p.sl - tp2_sl) <= tolerance:
                continue
            req = {
                "action":   self.mt5.TRADE_ACTION_SLTP,
                "position": p.ticket,
                "sl":       tp2_sl,
                "tp":       p.tp,
            }
            res = self.mt5.order_send(req)
            success = bool(res is not None and res.retcode == self.mt5.TRADE_RETCODE_DONE)
            self._log_order_send(signal_key, "modify_sl_to_tp2", req, res, success=success)
            if success:
                log.modified += 1
                locked_tickets.append(p.ticket)
                log.actions.append(
                    f"  Locked SL on #{p.ticket} to TP2 {tp2_sl:g} ({signal_key})"
                )
            else:
                reason = str(res.comment if res else self.mt5.last_error())
                lock_failures.append((p.ticket, reason))
                log.actions.append(
                    f"  FAILED TP2 SL-lock on #{p.ticket}: {reason}"
                )

        if self.notifier is not None and (locked_tickets or lock_failures):
            # Reuse the generic tp1_lock notification shape if the notifier has
            # not grown a dedicated TP2 method yet; the action text above keeps
            # the execution log explicit.
            notify = getattr(self.notifier, "tp2_lock", None)
            if callable(notify):
                notify(
                    signal_key=signal_key,
                    side=engine_pos.signal.side,
                    locked=locked_tickets,
                    failed=lock_failures,
                    sl=tp2_sl,
                )

        return log
