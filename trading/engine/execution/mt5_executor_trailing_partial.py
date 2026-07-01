"""Partial-arming patch for trailing-open MT5 execution.

This wrapper keeps the existing trailing executor behavior but fixes a live-only
ladder bug: one still-waiting trailing-open entry must not block already-armed
entries from placing their STOP orders.
"""
from __future__ import annotations

from . import mt5_executor_trailing as _base_trailing
from .mt5_executor import ExecutionLog, mt5_entry_comment, round_lot, signal_entry_key, signal_to_magic
from .sl_safety import min_stop_distance_for


class Mt5Executor(_base_trailing.Mt5Executor):
    """Trailing executor that places armed ladder legs even when later legs wait."""

    def place_signal(self, signal, plan) -> ExecutionLog:
        trailing_open_distance = self._plan_trailing_open_distance(plan)
        if trailing_open_distance <= 0:
            return super().place_signal(signal, plan)

        log = ExecutionLog()
        log.placed_entry_indices = []
        now_chart = _base_trailing._wall_clock_chart_now()

        replay_pos = getattr(plan, "replay_position", None)
        allow_partial = bool(getattr(self, "_allow_partial_placement", False))
        if (replay_pos is not None
                and len(plan.orders) < len(replay_pos.entries)
                and not allow_partial):
            if signal.signal_key not in self._session_skipped_partial_signal_keys:
                log.actions.append(
                    f"Signal {signal.signal_key}: skipped partial placement "
                    f"({len(plan.orders)} of {len(replay_pos.entries)} entries). "
                    f"Live registry is signal-level, so partial ladders are skipped "
                    f"to avoid managing unplaced entries (enable "
                    f"--reopen-missing-positions to place the live legs instead)."
                )
                self._session_skipped_partial_signal_keys.add(signal.signal_key)
            return log

        activation_at = getattr(plan, "pending_activates_at", None)
        if activation_at is None:
            activation_at = signal.signal_time_chart
        if now_chart < activation_at:
            if signal.signal_key not in self._session_skipped_inactive_signal_keys:
                log.actions.append(
                    f"Signal {signal.signal_key}: waiting for activation "
                    f"(activates {activation_at:%Y-%m-%d %H:%M} GMT+3, "
                    f"now {now_chart:%Y-%m-%d %H:%M} GMT+3)."
                )
                self._session_skipped_inactive_signal_keys.add(signal.signal_key)
            return log

        expires_at = getattr(plan, "pending_expires_at", None)
        if expires_at is not None and now_chart >= expires_at:
            if signal.signal_key not in self._session_skipped_expired_signal_keys:
                log.actions.append(
                    f"Signal {signal.signal_key}: skipped expired by wall-clock "
                    f"(expired {expires_at:%Y-%m-%d %H:%M} GMT+3, "
                    f"now {now_chart:%Y-%m-%d %H:%M} GMT+3)."
                )
                self._session_skipped_expired_signal_keys.add(signal.signal_key)
            return log

        tick = self.mt5.symbol_info_tick(self.symbol)
        if tick is None or tick.bid <= 0 or tick.ask <= 0:
            log.actions.append(
                f"Signal {signal.signal_key}: skipped trailing-open placement because no live "
                f"bid/ask tick is available for {self.symbol}."
            )
            return log
        bid = float(tick.bid)
        ask = float(tick.ask)

        magic = signal_to_magic(signal.signal_key)
        if self.find_orders(magic) or self.find_positions(magic):
            log.actions.append(
                f"Signal {signal.signal_key} already has MT5 orders/positions; "
                f"skipping placement (will manage instead)."
            )
            return log

        if not getattr(plan, "force_replace", False) and self._magic_system_closed(
                magic, signal.signal_time_chart):
            if signal.signal_key not in self._session_skipped_traded_signal_keys:
                log.actions.append(
                    f"Signal {signal.signal_key} already traded "
                    f"(SL/TP/engine close in MT5 history for its magic); skipping. "
                    f"(A manual close would instead re-arm the trailing-open.)"
                )
                self._session_skipped_traded_signal_keys.add(signal.signal_key)
            return log

        sym = self._sym_info if self._sym_info is not None else self.mt5.symbol_info(self.symbol)
        digits = sym.digits
        order_type = self._pending_stop_type(signal.side)
        rounded_lots: dict[int, float] = {}
        trigger_prices: dict[int, float] = {}
        planned_stop_distances: dict[int, float] = {}
        armed_orders = []
        waiting = []

        for order in plan.orders:
            lot = round_lot(order.lot, self.min_lot, self.lot_step)
            if lot <= 0:
                log.actions.append(
                    f"Signal {signal.signal_key}: skipped entire ladder because "
                    f"entry #{order.entry_index} computed lot {order.lot:.4f} "
                    f"below broker minimum {self.min_lot}."
                )
                return log
            trigger = self._candidate_trailing_open_price(
                signal.side, float(order.entry_price), bid, ask, trailing_open_distance
            )
            if trigger is None:
                waiting.append((order.entry_index, float(order.entry_price)))
                continue
            armed_orders.append(order)
            rounded_lots[order.entry_index] = lot
            trigger_prices[order.entry_index] = trigger
            planned_stop_distances[order.entry_index] = self._planned_stop_distance(
                signal.side, float(order.entry_price), float(order.initial_sl)
            )

        if waiting and signal.signal_key not in self._session_trailing_open_waiting_keys:
            log.actions.append(
                self._trailing_open_waiting_line(signal, waiting, trailing_open_distance)
            )
            self._session_trailing_open_waiting_keys.add(signal.signal_key)

        # Previously, any waiting entry returned here and blocked already-armed
        # legs. That missed V017 #02 at Ask ~= 3970: #4/#5 were armed while deeper
        # #6-#8 still waited. Return only when nothing is armed.
        if not armed_orders:
            return log

        min_stop = min_stop_distance_for(self)
        place_failures: list[tuple[int, float, str]] = []
        placed_tickets: list[int] = []
        armed_details: list[dict] = []
        market_fill_indices: list[int] = []

        for order in armed_orders:
            entry_key = signal_entry_key(signal.signal_key, order.entry_index)
            trigger = trigger_prices[order.entry_index]
            stop_distance = planned_stop_distances[order.entry_index]
            dynamic_sl = self._sl_from_fill(signal.side, trigger, stop_distance)
            comment = mt5_entry_comment(signal.signal_key, order.entry_index)
            request = {
                "action":       self.mt5.TRADE_ACTION_PENDING,
                "symbol":       self.symbol,
                "volume":       rounded_lots[order.entry_index],
                "type":         order_type,
                "price":        round(trigger, digits),
                "sl":           round(dynamic_sl, digits),
                "tp":           round(plan.final_target_price, digits),
                "magic":        magic,
                "comment":      comment,
                "type_time":    self.mt5.ORDER_TIME_GTC,
                "type_filling": self._market_fill_mode(),
            }
            res = self.mt5.order_send(request)
            success = bool(res is not None and res.retcode == self.mt5.TRADE_RETCODE_DONE)
            self._log_order_send(signal.signal_key, "place_trailing_open_stop", request, res, success=success)
            if not success:
                reason = (f"order_send returned None: {self.mt5.last_error()}" if res is None
                          else f"retcode={res.retcode} comment={res.comment!r}")
                if self._market_fill_passed_trailing_open(
                        signal, order, trigger, rounded_lots[order.entry_index],
                        stop_distance, float(plan.final_target_price),
                        magic, comment, digits, log, reason,
                        original_entry=float(order.entry_price)):
                    market_fill_indices.append(order.entry_index)
                    log.placed += 1
                    log.placed_entry_indices.append(order.entry_index)
                    continue
                price_now = ask if signal.side == "BUY" else bid
                log.actions.append(
                    f"  #{order.entry_index}: trailing-open STOP rejected -- wanted to "
                    f"{signal.side} at {trigger:g} (planned entry {float(order.entry_price):g}), "
                    f"price now {price_now:g} (bid {bid:g} / ask {ask:g}). Broker said: {reason}."
                )
                place_failures.append((order.entry_index, trigger, reason))
                break
            ticket = int(res.order)
            placed_tickets.append(ticket)
            armed_details.append({
                "entry_index": order.entry_index, "ticket": ticket,
                "stop_price": float(request["price"]),
                "sl": float(request["sl"]), "tp": float(request["tp"]),
            })
            log.placed += 1
            log.placed_entry_indices.append(order.entry_index)
            log.actions.append(
                f"  {entry_key}: placed trailing-open STOP ticket={ticket} comment={comment} "
                f"@ {request['price']:g} lot={request['volume']} "
                f"SL={request['sl']:g} TP={request['tp']:g}"
            )

        if self.notifier is not None and not place_failures:
            for detail in armed_details:
                self.notifier.trailing_open_armed(
                    signal_key=signal.signal_key, side=signal.side,
                    entry_index=detail["entry_index"], ticket=detail["ticket"],
                    stop_price=detail["stop_price"], sl=detail["sl"], tp=detail["tp"],
                )

        if place_failures:
            for ticket in placed_tickets:
                if self._cancel_ticket(ticket, signal.signal_key, "rollback_trailing_open_place"):
                    log.cancelled += 1
                    log.actions.append(
                        f"  Rolled back partial trailing-open placement ticket={ticket} ({signal.signal_key})"
                    )
            log.placed = len(market_fill_indices)
            log.placed_entry_indices = list(market_fill_indices)
            if market_fill_indices:
                log.actions.append(
                    f"Signal {signal.signal_key}: partial trailing-open placement -- "
                    f"{len(market_fill_indices)} leg(s) already filled at market are kept "
                    f"and will be managed; un-filled pending STOPs were rolled back."
                )
            else:
                price_now = ask if signal.side == "BUY" else bid
                planned = "/".join(f"{float(o.entry_price):g}" for o in armed_orders)
                msg = (
                    f"Signal {signal.signal_key}: trailing-open placement FAILED. "
                    f"Activated {activation_at:%Y-%m-%d %H:%M} GMT+3, expected entry {planned}, "
                    f"price now {price_now:g} (bid {bid:g} / ask {ask:g}). No order placed."
                )
                if min_stop > 0 and trailing_open_distance < min_stop:
                    msg += (
                        f" Cause: the {trailing_open_distance:g}-pt trailing-open distance is "
                        f"below this broker's minimum stop distance ({min_stop:g} pt), so the "
                        f"STOP can't rest that close to price. Raise --trailing-open-distance "
                        f"above {min_stop:g} to trade this signal live."
                    )
                log.actions.append(msg)
        return log
