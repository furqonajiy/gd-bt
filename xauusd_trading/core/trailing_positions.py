"""Trailing-open / trailing-close position advancement.

This module wraps the validated core position lifecycle with optional mechanics
that are disabled by default so DD40 behaviour is preserved.
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
from .trend_runner import (
    runner_can_hold,
    should_skip_time_exit,
    stop_status_for,
    update_indicators,
    update_runner_stop,
)


def _entry_open_before_bar(entry: Entry, bar: Bar) -> bool:
    return entry.status == "OPEN" and entry.fill_time is not None and entry.fill_time < bar.time


def _set_first_fill(position: Position, entry: Entry, bar: Bar, config: StrategyConfig) -> None:
    if position.first_fill_time is None:
        position.first_fill_time = bar.time
        position.time_exit_deadline = bar.time + timedelta(minutes=config.max_hold_minutes)


def _fill_entry_at_market_side(position: Position, entry: Entry, fill_price: float, bar: Bar,
                               config: StrategyConfig) -> None:
    planned_risk_distance = abs(entry.entry_price - entry.initial_sl)
    entry.status = "OPEN"
    entry.fill_time = bar.time
    entry.entry_price = fill_price
    entry.initial_sl = initial_stop_for_entry(position.signal.side, fill_price, planned_risk_distance)
    _set_first_fill(position, entry, bar, config)


def _normal_limit_fills(position: Position, bar: Bar, config: StrategyConfig) -> None:
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
                e.armed_for_touch = True
                continue
            else:
                continue
        if fill_trigger(side, h, l, e.entry_price, sp):
            e.status = "OPEN"
            e.fill_time = bar.time
            _set_first_fill(position, e, bar, config)


def _trailing_open_fills(position: Position, bar: Bar, config: StrategyConfig) -> None:
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
        ask_low = bar.low + sp
        ask_high = bar.high + sp
        deepest_entry = min(e.entry_price for e in pending)
        if not active:
            if ask_low <= deepest_entry:
                position.trailing_open_active = True
                position.trailing_open_extreme = ask_low
            return

        prev_extreme = float(extreme if extreme is not None else ask_low)
        if ask_low < prev_extreme:
            position.trailing_open_extreme = ask_low
            return

        trigger_price = prev_extreme + distance
        if ask_high >= trigger_price:
            for e in pending:
                _fill_entry_at_market_side(position, e, trigger_price, bar, config)
            position.trailing_open_active = False
            position.trailing_open_extreme = None
        return

    bid_high = bar.high
    bid_low = bar.low
    deepest_entry = max(e.entry_price for e in pending)
    if not active:
        if bid_high >= deepest_entry:
            position.trailing_open_active = True
            position.trailing_open_extreme = bid_high
        return

    prev_extreme = float(extreme if extreme is not None else bid_high)
    if bid_high > prev_extreme:
        position.trailing_open_extreme = bid_high
        return

    trigger_price = prev_extreme - distance
    if bid_low <= trigger_price:
        for e in pending:
            _fill_entry_at_market_side(position, e, trigger_price, bar, config)
        position.trailing_open_active = False
        position.trailing_open_extreme = None


def _effective_stop_with_trailing(position: Position, entry: Entry, config: StrategyConfig) -> float:
    return position.effective_stop_for(entry, config)


def _update_trailing_close_stops(position: Position, bar: Bar, config: StrategyConfig) -> None:
    distance = float(getattr(config, "trailing_close_distance", 0.0) or 0.0)
    if distance <= 0:
        return
    side = position.signal.side
    for e in position.open_entries():
        if not _entry_open_before_bar(e, bar):
            continue
        if side == "BUY":
            candidate = bar.high - distance
            if candidate <= e.entry_price:
                continue
            current = getattr(e, "trailing_stop", None)
            e.trailing_stop = candidate if current is None else max(float(current), candidate)
        else:
            candidate = bar.low + distance
            if candidate >= e.entry_price:
                continue
            current = getattr(e, "trailing_stop", None)
            e.trailing_stop = candidate if current is None else min(float(current), candidate)


def advance_one_bar(
        position: Position, bar: Bar, config: StrategyConfig,
        contract_size: float = CONTRACT_SIZE_OZ,
) -> None:
    side = position.signal.side
    sp = bar.spread_price
    h, l, c = bar.high, bar.low, bar.close
    update_indicators(position, bar, config)

    if position.activation_time <= bar.time <= position.expiry_time:
        _trailing_open_fills(position, bar, config)

    if bar.time > position.expiry_time:
        for e in position.entries:
            if e.status == "PENDING":
                e.status = "NO_FILL"

    open_entries = position.open_entries()
    if open_entries:
        tp1_hit, tp2_hit, tp3_hit, target_hit = _target_levels_hit(position, side, h, l, sp)
        runner_after_tp3 = bool(getattr(config, "runner_after_tp3", False)) and config.final_target.upper() == "TP3"
        trend_runner_holds = bool(target_hit and runner_can_hold(position, config))
        if trend_runner_holds or getattr(position, "trend_runner_active", False):
            update_runner_stop(position, bar, config)

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
                fallback_status = _stop_status(lock_stage, stop_level, e, config)
                status = stop_status_for(e, stop_level, fallback_status)
                _close_entry(e, status, bar.time, stop_level, side, contract_size, stop_level)
            elif target_hit and not runner_after_tp3 and not trend_runner_holds and _entry_open_before_bar(e, bar):
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

        _update_trailing_close_stops(position, bar, config)

    if (
        position.time_exit_deadline is not None
        and bar.time >= position.time_exit_deadline
        and not should_skip_time_exit(position, config)
    ):
        exit_price = _time_exit_price(side, c, sp)
        for e in position.entries:
            if e.status == "OPEN":
                _close_entry(e, "TIME_EXIT", bar.time, exit_price, side, contract_size)

    position.last_processed_time = bar.time


def advance_bars(
        position: Position, bars, config: StrategyConfig,
        contract_size: float = CONTRACT_SIZE_OZ,
) -> None:
    for bar in bars:
        advance_one_bar(position, bar, config, contract_size)
        if position.is_terminal():
            break
