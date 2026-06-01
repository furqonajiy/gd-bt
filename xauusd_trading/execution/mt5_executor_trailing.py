"""Trailing-aware public MT5 executor.

A broker BUY LIMIT cannot express the requested trailing-open rule: if the limit is
4750 and Ask drops through 4750 on the way to 4740, MT5 fills immediately.
When ``trailing_open_distance`` is enabled this executor therefore uses broker
STOP orders as virtual trailing entries:

* BUY: after Ask has moved at least ``distance`` below the planned entry, place a
  BUY STOP at ``Ask + distance`` and trail that pending stop lower while Ask keeps
  falling.
* SELL: after Bid has moved at least ``distance`` above the planned entry, place a
  SELL STOP at ``Bid - distance`` and trail that pending stop higher while Bid
  keeps rising.

This keeps live behavior close to the shared backtest lifecycle while avoiding
unsafe immediate LIMIT fills.
"""
from __future__ import annotations

from .mt5_executor_tp2 import Mt5Executor as _Tp2Mt5Executor
from .mt5_executor import ExecutionLog, mt5_entry_comment, round_lot, signal_entry_key, signal_to_magic
from xauusd_trading.core.config import DEFAULT_CONFIG


class Mt5Executor(_Tp2Mt5Executor):
    """MT5 executor with trailing-open and trailing-close parity helpers."""

    def _trailing_open_distance(self) -> float:
        return float(getattr(DEFAULT_CONFIG, "trailing_open_distance", 0.0) or 0.0)

    def _pending_stop_type(self, side: str):
        return self.mt5.ORDER_TYPE_BUY_STOP if side == "BUY" else self.mt5.ORDER_TYPE_SELL_STOP

    def _pending_limit_type(self, side: str):
        return self.mt5.ORDER_TYPE_BUY_LIMIT if side == "BUY" else self.mt5.ORDER_TYPE_SELL_LIMIT

    @staticmethod
    def _planned_stop_distance(side: str, planned_entry: float, planned_sl: float) -> float:
        return planned_entry - planned_sl if side == "BUY" else planned_sl - planned_entry

    @staticmethod
    def _sl_from_fill(side: str, fill_price: float, stop_distance: float) -> float:
        return fill_price - stop_distance if side == "BUY" else fill_price + stop_distance

    def _candidate_trailing_open_price(self, side: str, planned_entry: float, bid: float, ask: float, distance: float):
        if distance <= 0:
            return None
        if side == "BUY":
            # Wait until Ask is safely below the planned entry. A BUY STOP at
            # Ask+distance then opens only after rebound.
            if ask > planned_entry - distance:
                return None
            return ask + distance
        # SELL waits until Bid is safely above the planned entry, then uses a
        # SELL STOP below current Bid so it opens only after pullback.
        if bid < planned_entry + distance:
            return None
        return bid - distance

    def place_signal(self, signal, plan) -> ExecutionLog:
        trailing_open_distance = self._trailing_open_distance()
        if trailing_open_distance <= 0:
            return super().place_signal(signal, plan)

        # Largely mirrors the TP2 wrapper's all-or-nothing placement guard, but
        # sends STOP orders at the current trailing trigger instead of LIMITs.
        log = ExecutionLog()
        log.placed_entry_indices = []
        now_chart = __import__("xauusd_trading.execution.mt5_executor_tp2", fromlist=["_wall_clock_chart_now"])._wall_clock_chart_now()

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
            activation_at = signal.signal_time_chart
        if now_chart < activation_at:
            log.actions.append(
                f"Signal {signal.signal_key}: waiting for activation "
                f"(activates {activation_at:%Y-%m-%d %H:%M} GMT+3, "
                f"now {now_chart:%Y-%m-%d %H:%M} GMT+3)."
            )
            return log

        expires_at = getattr(plan, "pending_expires_at", None)
        if expires_at is not None and now_chart >= expires_at:
            log.actions.append(
                f"Signal {signal.signal_key}: skipped expired by wall-clock "
                f"(expired {expires_at:%Y-%m-%d %H:%M} GMT+3, "
                f"now {now_chart:%Y-%m-%d %H:%M} GMT+3)."
            )
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

        sym = self._sym_info if self._sym_info is not None else self.mt5.symbol_info(self.symbol)
        digits = sym.digits
        order_type = self._pending_stop_type(signal.side)
        rounded_lots: dict[int, float] = {}
        trigger_prices: dict[int, float] = {}
        planned_stop_distances: dict[int, float] = {}

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
                waiting.append(signal_entry_key(signal.signal_key, order.entry_index))
            else:
                rounded_lots[order.entry_index] = lot
                trigger_prices[order.entry_index] = trigger
                planned_stop_distances[order.entry_index] = self._planned_stop_distance(
                    signal.side, float(order.entry_price), float(order.initial_sl)
                )

        if waiting:
            log.actions.append(
                f"Signal {signal.signal_key}: trailing-open waiting; "
                f"{len(waiting)} entr{'y has' if len(waiting) == 1 else 'ies have'} not yet moved "
                f"{trailing_open_distance:g} beyond the planned entry. No broker LIMIT is placed."
            )
            return log

        place_failures: list[tuple[int, float, str]] = []
        placed_tickets: list[int] = []
        for order in plan.orders:
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
                "type_filling": self.mt5.ORDER_FILLING_RETURN,
            }
            res = self.mt5.order_send(request)
            success = bool(res is not None and res.retcode == self.mt5.TRADE_RETCODE_DONE)
            self._log_order_send(signal.signal_key, "place_trailing_open_stop", request, res, success=success)
            if res is None:
                reason = str(self.mt5.last_error())
                log.actions.append(f"  #{order.entry_index}: FAILED order_send returned None: {reason}")
                place_failures.append((order.entry_index, trigger, reason))
                break
            if res.retcode != self.mt5.TRADE_RETCODE_DONE:
                reason = f"retcode={res.retcode} comment={res.comment!r}"
                log.actions.append(f"  #{order.entry_index}: FAILED {reason}")
                place_failures.append((order.entry_index, trigger, reason))
                break
            ticket = int(res.order)
            placed_tickets.append(ticket)
            log.placed += 1
            log.placed_entry_indices.append(order.entry_index)
            log.actions.append(
                f"  {entry_key}: placed trailing-open STOP ticket={ticket} comment={comment} "
                f"@ {request['price']:g} lot={request['volume']} "
                f"SL={request['sl']:g} TP={request['tp']:g}"
            )

        if place_failures:
            if placed_tickets:
                for ticket in placed_tickets:
                    if self._cancel_ticket(ticket, signal.signal_key, "rollback_trailing_open_place"):
                        log.cancelled += 1
                        log.actions.append(f"  Rolled back partial trailing-open placement ticket={ticket} ({signal.signal_key})")
                log.placed = 0
                log.placed_entry_indices = []
            log.actions.append(f"Signal {signal.signal_key}: trailing-open placement failed; no registry entry should be recorded.")
        return log

    def _trail_pending_open_orders(self, engine_pos, config) -> ExecutionLog:
        distance = float(getattr(config, "trailing_open_distance", 0.0) or self._trailing_open_distance())
        if distance <= 0:
            return ExecutionLog()
        log = ExecutionLog()
        tick = self.mt5.symbol_info_tick(self.symbol)
        if tick is None or tick.bid <= 0 or tick.ask <= 0:
            return log
        bid = float(tick.bid)
        ask = float(tick.ask)
        magic = signal_to_magic(engine_pos.signal.signal_key)
        signal_key = engine_pos.signal.signal_key
        digits = self.mt5.symbol_info(self.symbol).digits
        modify_action = getattr(self.mt5, "TRADE_ACTION_MODIFY", 7)
        stop_type = self._pending_stop_type(engine_pos.signal.side)
        used: set[int] = set()
        for order in self.find_orders(magic):
            if getattr(order, "type", None) != stop_type:
                continue
            idx = self._map_position_to_entry_index(order, used, len(engine_pos.entries))
            if idx is None:
                continue
            used.add(idx)
            entry = engine_pos.entries[idx]
            trigger = self._candidate_trailing_open_price(
                engine_pos.signal.side, float(entry.entry_price), bid, ask, distance
            )
            if trigger is None:
                continue
            current_price = float(getattr(order, "price_open", getattr(order, "price_current", 0.0)) or 0.0)
            improves = trigger < current_price if engine_pos.signal.side == "BUY" else trigger > current_price
            if not improves:
                continue
            dynamic_sl = self._sl_from_fill(engine_pos.signal.side, trigger, engine_pos.base_stop_distance)
            req = {
                "action": modify_action,
                "order": order.ticket,
                "price": round(trigger, digits),
                "sl": round(dynamic_sl, digits),
                "tp": getattr(order, "tp", engine_pos.target_level),
            }
            res = self.mt5.order_send(req)
            success = bool(res is not None and res.retcode == self.mt5.TRADE_RETCODE_DONE)
            self._log_order_send(signal_key, "modify_trailing_open_stop", req, res, success=success)
            if success:
                log.modified += 1
                log.actions.append(
                    f"  Trailed pending open STOP #{order.ticket} to {req['price']:g} "
                    f"SL={req['sl']:g} ({signal_key})"
                )
            else:
                reason = str(res.comment if res else self.mt5.last_error())
                log.actions.append(f"  FAILED trailing-open modify on #{order.ticket}: {reason}")
        return log

    def _apply_trailing_close_stops(self, engine_pos, config) -> ExecutionLog:
        if float(getattr(config, "trailing_close_distance", 0.0) or 0.0) <= 0:
            return ExecutionLog()
        log = ExecutionLog()
        magic = signal_to_magic(engine_pos.signal.signal_key)
        signal_key = engine_pos.signal.signal_key
        digits = self.mt5.symbol_info(self.symbol).digits
        tolerance = 10 ** (-digits)
        locked: list[int] = []
        failed: list[tuple[int, str]] = []
        for p, entry in self._position_entry_pairs(engine_pos, magic):
            if entry.status != "OPEN" or entry.trailing_stop is None:
                continue
            target_sl = round(engine_pos.effective_stop_for(entry, config), digits)
            current_sl = float(getattr(p, "sl", 0.0) or 0.0)
            improves = (
                current_sl <= 0
                or (engine_pos.signal.side == "BUY" and target_sl > current_sl + tolerance)
                or (engine_pos.signal.side == "SELL" and target_sl < current_sl - tolerance)
            )
            if not improves:
                continue
            self._modify_stop(
                p, target_sl, signal_key, "modify_trailing_close_sl", "trailing-stop",
                log, locked, failed,
            )
        return log

    def manage_position(self, engine_pos, config, chart_now):
        log = super().manage_position(engine_pos, config, chart_now)
        log.merge(self._trail_pending_open_orders(engine_pos, config))
        log.merge(self._apply_trailing_close_stops(engine_pos, config))
        return log
