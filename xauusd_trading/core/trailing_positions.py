"""Trailing-open / trailing-close position advancement.

This module wraps the validated core position lifecycle with two optional
mechanics that are safe to keep disabled by default:

* trailing_open_distance > 0: entries are virtual until price has moved through
  the whole ladder and then rebounded by the configured distance.  This prevents
  a BUY LIMIT at 4750 from being filled immediately while price is still dumping
  to 4740.
* trailing_close_distance > 0: once an entry is open, a protective stop trails
  favourable price action by the configured distance.  The stop is advanced only
  after a completed bar, so the replay does not assume impossible intrabar
  ordering.

The default StrategyConfig keeps both distances at 0.0, which preserves the
current DD40 lifecycle exactly.
"""
from __future__ import annotations

from datetime import timedelta

from .chart import Bar
from .positions import Entry, Position
from .positions import (
    _bep_triggered,
    _close_entry,
    _delay_elapsed,
    _stop_status,
    _target_levels_hit,
    _time_exit_price,
)
from .triggers import fill_trigger, initial_stop_for_entry, stop_trigger, target_trigger
from .config import CONTRACT_SIZE_OZ, StrategyConfig


def _entry_open_before_bar(entry: Entry, bar: Bar) -> bool:
    return entry.status == "OPEN" and entry.fill_time is not None and entry.fill_time < bar.time


def _set_first_fill(position: Position, entry: Entry, bar: Bar, config: StrategyConfig) -> None:
    if position.first_fill_time is None:
        position.first_fill_time = bar.time
        position.time_exit_deadline = bar.time + timedelta(minutes=config.max_hold_minutes)


def _fill_entry_at_market_side(position: Position, entry: Entry, fill_price: float, bar: Bar,
                               config: StrategyConfig) -> None:
    """Fill a virtual trailing entry and preserve its planned risk distance."""
    planned_risk_distance = abs(entry.entry_price - entry.initial_sl)
    entry.status = "OPEN"
    entry.fill_time = bar.time
    entry.entry_price = fill_price
    entry.initial_sl = initial_stop_for_entry(position.signal.side, fill_price, planned_risk_distance)
    _set_first_fill(position, entry, bar, config)


def _normal_limit_fills(position: Position, bar: Bar, config: StrategyConfig) -> None:
    """Original strict-touch LIMIT fill behaviour."""
    side = position.signal.side
    sp = bar.spread_price
    h, l = bar.high, bar.low
    for e in position.entries:
        if e.status != "PENDING":
            continue
        if side == "BUY":
            opened_safe = (bar.open + sp) > e.entry_price
            returned_safe = (h + sp) > e.entry_price
        else:
            opened_safe = bar.open < e.entry_price
            returned_safe = l < e.entry_price
        if not e.armed_for_touch:
            if opened_safe:
                e.armed_for_touch = True
            elif returned_safe:
                # The bar visited the safe side, but OHLC cannot order the touch
                # and the fill inside this candle. Require a later candle.
                e.armed_for_touch = True
                continue
            else:
                continue
        if fill_trigger(side, h, l, e.entry_price, sp):
            e.status = "OPEN"
            e.fill_time = bar.time
            _set_first_fill(position, e, bar, config)


def _trailing_open_fills(position: Position, bar: Bar, config: StrategyConfig) -> None:
    """Virtual all-ladder trailing entry.

    For BUY, the whole ladder is armed only after Ask reaches the deepest entry.
    The entries then fill together after Ask rebounds from the lowest observed
    Ask by trailing_open_distance.  SELL is symmetric on Bid.
    """
    distance = float(getattr(config, "trailing_open_distance", 0.0) or 0.0)
    if distance <= 0:
        _normal_limit_fills(position, bar, config)
        return

    pending = [e for e in position.entries if e.status == "PENDING"]
    if not pending:
        return

    side = position.signal.side
    sp = bar.spread_price
    active = bool(getattr(position, "trailing_open_active", False))
    extreme = getattr(position, "trailing_open_extreme", None)

    if side == "BUY":
        # Market-side prices for BUY are Ask = Bid + spread.
        ask_low = bar.low + sp
        ask_high = bar.high + sp
        deepest_entry = min(e.entry_price for e in pending)
        if not active:
            if ask_low <= deepest_entry:
                position.trailing_open_active = True
                position.trailing_open_extreme = ask_low
            return

        prev_extreme = float(extreme if extreme is not None else ask_low)
        trigger_price = prev_extreme + distance
        if ask_high >= trigger_price:
            for e in pending:
                _fill_entry_at_market_side(position, e, trigger_price, bar, config)
            position.trailing_open_active = False
            position.trailing_open_extreme = None
        else:
            position.trailing_open_extreme = min(prev_extreme, ask_low)
        return

    # SELL uses Bid side.
    bid_high = bar.high
    bid_low = bar.low
    deepest_entry = max(e.entry_price for e in pending)
    if not active:
        if bid_high >= deepest_entry:
            position.trailing_open_active = True
            position.trailing_open_extreme = bid_high
        return

    prev_extreme = float(extreme if extreme is not None else bid_high)
    trigger_price = prev_extreme - distance
    if bid_low <= trigger_price:
        for e in pending:
            _fill_entry_at_market_side(position, e, trigger_price, bar, config)
        position.trailing_open_active = False
        position.trailing_open_extreme = None
    else:
        position.trailing_open_extreme = max(prev_extreme, bid_high)


def _effective_stop_with_trailing(position: Position, entry: Entry, config: StrategyConfig) -> float:
    base_stop = position.effective_stop_for(entry, config)
    trail_stop = getattr(entry, "trailing_close_stop", None)
    if trail_stop is None:
        return base_stop
    if position.signal.side == "BUY":
        return max(base_stop, float(trail_stop))
    return min(base_stop, float(trail_stop))


def _update_trailing_close_stops(position: Position, bar: Bar, config: StrategyConfig) -> None:
    distance = float(getattr(config, "trailing_close_distance", 0.0) or 0.0)
    if distance <= 0:
        return
    side = position.signal.side
    for e in position.open_entries():
        # Do not allow a position filled in this same M1 candle to receive and
        # use a trailing stop derived from that candle.
        if not _entry_open_before_bar(e, bar):
            continue
        if side == "BUY":
            candidate = bar.high - distance
            if candidate <= e.entry_price:
                continue
            current = getattr(e, "trailing_close_stop", None)
            e.trailing_close_stop = candidate if current is None else max(float(current), candidate)
        else:
            candidate = bar.low + distance
            if candidate >= e.entry_price:
                continue
            current = getattr(e, "trailing_close_stop", None)
            e.trailing_close_stop = candidate if current is None else min(float(current), candidate)


def advance_one_bar(
        position: Position, bar: Bar, config: StrategyConfig,
        contract_size: float = CONTRACT_SIZE_OZ,
) -> None:
    """Mutate `position` state to reflect one minute of price action."""
    side = position.signal.side
    sp = bar.spread_price
    h, l, c = bar.high, bar.low, bar.close

    # 1. Fills.
    if position.activation_time <= bar.time <= position.expiry_time:
        _trailing_open_fills(position, bar, config)

    # 2. Pending expiry.
    if bar.time > position.expiry_time:
        for e in position.entries:
            if e.status == "PENDING":
                e.status = "NO_FILL"

    # 3. Stop / target. Worst-case: active stop wins same bar.
    open_entries = position.open_entries()
    if open_entries:
        tp1_hit, tp2_hit, tp3_hit, target_hit = _target_levels_hit(position, side, h, l, sp)
        runner_after_tp3 = bool(getattr(config, "runner_after_tp3", False)) and config.final_target.upper() == "TP3"

        if getattr(config, "profit_lock_mode", "tp_levels") == "bep_plus_half_tp1":
            for e in open_entries:
                if (
                    _entry_open_before_bar(e, bar)
                    and not e.bep_armed
                    and _bep_triggered(side, e, h, l, sp, config.bep_trigger_distance)
                ):
                    e.bep_armed = True

        for e in list(open_entries):
            stop_level = _effective_stop_with_trailing(position, e, config)
            lock_stage = position.lock_stage_for(e, config.lock_after_tp1, config.lock_after_tp2)
            if stop_trigger(side, h, l, stop_level, sp):
                status = _stop_status(lock_stage, stop_level, e, config)
                _close_entry(e, status, bar.time, stop_level, side, contract_size, stop_level)
            elif target_hit and not runner_after_tp3 and _entry_open_before_bar(e, bar):
                _close_entry(e, config.final_target.upper(), bar.time, position.target_level,
                             side, contract_size)

        stageable_entries = [e for e in position.open_entries() if _entry_open_before_bar(e, bar)]

        if config.lock_after_tp1 and position.stage1_time is None and tp1_hit and stageable_entries:
            position.stage1_time = bar.time
        if config.lock_after_tp2 and position.stage2_time is None and tp2_hit and stageable_entries:
            position.stage2_time = bar.time

        if config.lock_after_tp1 and position.stage < 1 and stageable_entries:
            if _delay_elapsed(position.stage1_time, bar.time, config.tp1_lock_delay_minutes):
                position.stage = 1
        if config.lock_after_tp2 and position.stage < 2 and stageable_entries:
            if _delay_elapsed(position.stage2_time, bar.time, config.tp2_lock_delay_minutes):
                position.stage = 2
        if runner_after_tp3 and position.stage < 3 and tp3_hit and stageable_entries:
            position.stage = 3
            position.stage3_time = bar.time

        # Advance trailing close stops only after stop/target checks for this bar.
        _update_trailing_close_stops(position, bar, config)

    # 5. Time exit at first bar at/after the deadline; closes at market-side close.
    if position.time_exit_deadline is not None and bar.time >= position.time_exit_deadline:
        exit_price = _time_exit_price(side, c, sp)
        for e in position.entries:
            if e.status == "OPEN":
                _close_entry(e, "TIME_EXIT", bar.time, exit_price, side, contract_size)

    position.last_processed_time = bar.time


def advance_bars(
        position: Position, bars, config: StrategyConfig,
        contract_size: float = CONTRACT_SIZE_OZ,
) -> None:
    """Advance through an iterable of Bar objects, stopping early if terminal."""
    for bar in bars:
        advance_one_bar(position, bar, config, contract_size)
        if position.is_terminal():
            break
