"""Trailing-open / trailing-close position advancement.

This module wraps the validated core position lifecycle with optional mechanics
that are disabled by default so DD40 behaviour is preserved.
"""
from __future__ import annotations

import math
from datetime import timedelta

from .chart import Bar
from .positions import Entry, Position
from .positions import (
    _bep_triggered,
    _close_entry,
    _delay_elapsed,
    _scale_out_mode,
    _stage1_active,
    _stage2_active,
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


def _locked_exit_fill(side: str, stop_level: float, status: str,
                      high: float, low: float, config: StrategyConfig) -> float:
    """Exit price when a stop triggers. A *locked* protective stop
    (LOCK_TP1/LOCK_TP2) fills at market on the retrace, a touch past the level,
    so live gives back ``config.lock_exit_slippage_points`` (price units). Raw SL
    keeps the level — its fill is tick-confirmed and never market-fills past the
    trigger. The fill is clamped to the bar's range so it never models more slip
    than the bar allows. Default slippage 0 -> exact-level fill (preserves the
    parity contract); set >0 only in the backtest to match live's locked-exit
    give-back. Backtest-only: the live executor's config keeps this at 0, so live
    order placement is unaffected."""
    slip = float(getattr(config, "lock_exit_slippage_points", 0.0) or 0.0)
    if slip <= 0 or not str(status).startswith("LOCK_"):
        return stop_level
    if side == "BUY":
        return max(stop_level - slip, low)
    return min(stop_level + slip, high)


def _entry_open_before_bar(entry: Entry, bar: Bar) -> bool:
    return entry.status == "OPEN" and entry.fill_time is not None and entry.fill_time < bar.time


def _scale_out_worst(position: Position, bar: Bar, target_price: float, status: str,
                     contract_size: float) -> bool:
    """Close the worst open leg at a TP level, keeping the rest as runners.

    Worst = leg furthest from the signal SL (BUY: highest fill, SELL: lowest fill) --
    the worst risk:reward leg. Only legs filled before this bar are eligible (the
    same touch-applies rule the stage locks use), and at least two such legs must be
    open so a runner remains; otherwise no scale-out happens and the single leg runs.
    """
    eligible = [e for e in position.open_entries() if _entry_open_before_bar(e, bar)]
    if len(eligible) < 2:
        return False
    worst = max(eligible, key=lambda e: abs(e.entry_price - position.signal.sl))
    _close_entry(worst, status, bar.time, target_price, position.signal.side, contract_size, None)
    return True


def _set_first_fill(position: Position, entry: Entry, bar: Bar, config: StrategyConfig) -> None:
    if position.first_fill_time is None:
        position.first_fill_time = bar.time
        position.time_exit_deadline = bar.time + timedelta(minutes=config.max_hold_minutes)


def _fill_entry_at_market_side(position: Position, entry: Entry, fill_price: float, bar: Bar,
                               config: StrategyConfig) -> None:
    side = position.signal.side
    # Capture the leg's planned risk distance BEFORE entry_price is overwritten:
    # for a shared-SL leg that is the gap from its planned entry to the shared
    # level (each leg keeps its own distance, the "per-leg risk sizing" shared_sl
    # promises); otherwise it is the common base_stop_distance.
    if position.shared_sl_level is not None:
        leg_stop_distance = abs(entry.entry_price - position.shared_sl_level)
    else:
        leg_stop_distance = position.base_stop_distance
    entry.status = "OPEN"
    entry.fill_time = bar.time
    entry.entry_price = fill_price
    # Trailing-open fills land away from the planned entry (a BUY fills on the
    # rebound, often BELOW its planned price), so the frozen shared level can end
    # up on the WRONG side of the fill -- above a BUY fill -- which is not a stop
    # at all. The live executor never sends that: it anchors each leg's stop at
    # (fill -/+ its planned distance to the shared level). Mirror that here, for
    # both shared and per-entry stops, so backtest == live and we never book a
    # phantom exit at a level the position opened already beyond.
    entry.initial_sl = initial_stop_for_entry(side, fill_price, leg_stop_distance)
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

    side = position.signal.side
    sp = bar.spread_price

    if side == "BUY":
        ask_low = bar.low + sp
        ask_high = bar.high + sp
        for entry in position.entries:
            if entry.status != "PENDING":
                continue
            armed_at = entry.trailing_open_touched_at
            if armed_at is None:
                if ask_low <= entry.entry_price - distance:
                    entry.trailing_open_touched_at = bar.time
                    entry.trailing_open_extreme = ask_low
                continue

            if entry.trailing_open_extreme is None or ask_low < entry.trailing_open_extreme:
                entry.trailing_open_extreme = ask_low
            trigger_price = float(entry.trailing_open_extreme) + distance
            if bar.time > armed_at and ask_high >= trigger_price:
                _fill_entry_at_market_side(position, entry, trigger_price, bar, config)
        return

    bid_high = bar.high
    bid_low = bar.low
    for entry in position.entries:
        if entry.status != "PENDING":
            continue
        armed_at = entry.trailing_open_touched_at
        if armed_at is None:
            if bid_high >= entry.entry_price + distance:
                entry.trailing_open_touched_at = bar.time
                entry.trailing_open_extreme = bid_high
            continue

        if entry.trailing_open_extreme is None or bid_high > entry.trailing_open_extreme:
            entry.trailing_open_extreme = bid_high
        trigger_price = float(entry.trailing_open_extreme) - distance
        if bar.time > armed_at and bid_low <= trigger_price:
            _fill_entry_at_market_side(position, entry, trigger_price, bar, config)


def _effective_stop_with_trailing(position: Position, entry: Entry, config: StrategyConfig) -> float:
    return position.effective_stop_for(entry, config)


def _engage_trend_runner(position: Position, bar: Bar) -> None:
    """Flag the TP3 runner active before the stop/time-exit checks only.

    Stage promotion, stage3_time, and the protective runner stop are deferred to
    update_runner_stop (which runs after the per-entry stop loop) so the
    runner-owned stop can only trigger from the next bar onward, never against the
    same engage bar's pre-TP3 extreme.  Setting just the flag here still suppresses
    the final-target close and the time-exit on the bar that first reaches TP3.
    """
    position.trend_runner_active = True


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


# ---------------------------------------------------------------------------
# Per-entry-target mode (gated by config.per_entry_targets)
# ---------------------------------------------------------------------------

def _favorable_move(side: str, entry_price: float, high: float, low: float) -> float:
    return (high - entry_price) if side == "BUY" else (entry_price - low)


def _bep_plus_stop(side: str, entry_price: float, buffer: float) -> float:
    return entry_price + buffer if side == "BUY" else entry_price - buffer


def _tighter_stop(side: str, a: float, b: float) -> float:
    """The more protective of two stop levels (higher for BUY, lower for SELL)."""
    return max(a, b) if side == "BUY" else min(a, b)


def _per_entry_stop_status(side: str, e: Entry, stop: float, bep_buffer: float) -> str:
    if e.runner_engaged and e.trailing_stop is not None and math.isclose(stop, float(e.trailing_stop)):
        return "TRAILING_STOP"
    if e.bep_after_move_armed and math.isclose(stop, _bep_plus_stop(side, e.entry_price, bep_buffer)):
        return "BEP"
    return "SL"


def _advance_per_entry_target_bar(position: Position, bar: Bar, config: StrategyConfig,
                                  contract_size: float) -> None:
    """One bar of the per-entry-target strategy.

    Each leg exits at its OWN target (TP1/TP2/TP3); RUN legs engage a trailing
    stop once `runner_trail_from` (default TP3) is touched, then trail by
    trailing_close_distance (and skip the max-hold time exit). The trail never
    runs from entry. Independently, once a filled leg is bep_after_move price
    units in favour its SL ratchets to entry +/- bep_buffer.
    """
    side = position.signal.side
    sp = bar.spread_price
    h, l, c = bar.high, bar.low, bar.close
    tp_levels = {"TP1": position.signal.tp1, "TP2": position.signal.tp2, "TP3": position.signal.tp3}
    trail_from = tp_levels.get(str(getattr(config, "runner_trail_from", "TP3")).upper(), position.signal.tp3)
    bep_move = float(getattr(config, "bep_after_move", 0.0) or 0.0)
    bep_buffer = float(getattr(config, "bep_buffer", 0.0) or 0.0)
    trail_dist = float(getattr(config, "trailing_close_distance", 0.0) or 0.0)

    if position.activation_time <= bar.time <= position.expiry_time:
        _normal_limit_fills(position, bar, config)
    if bar.time > position.expiry_time:
        for e in position.entries:
            if e.status == "PENDING":
                e.status = "NO_FILL"

    for e in list(position.open_entries()):
        before_bar = _entry_open_before_bar(e, bar)
        # Protective stop uses state established on PRIOR bars, so a BEP/runner
        # stop that arms this bar can't pre-empt a target also reached this bar
        # (the favourable move that arms it happened first, on the way up).
        stop = e.initial_sl
        if e.bep_after_move_armed:
            stop = _tighter_stop(side, stop, _bep_plus_stop(side, e.entry_price, bep_buffer))
        if e.runner_engaged and e.trailing_stop is not None:
            stop = _tighter_stop(side, stop, float(e.trailing_stop))

        if stop_trigger(side, h, l, stop, sp):
            status = _per_entry_stop_status(side, e, stop, bep_buffer)
            _close_entry(e, status, bar.time, stop, side, contract_size, stop)
            continue
        if before_bar and e.target_label in ("TP1", "TP2", "TP3"):
            if target_trigger(side, h, l, e.target_price, sp):
                _close_entry(e, e.target_label, bar.time, e.target_price, side, contract_size)
                continue

        # Arm protective mechanics for the NEXT bar.
        if bep_move > 0 and not e.bep_after_move_armed and before_bar:
            if _favorable_move(side, e.entry_price, h, l) >= bep_move:
                e.bep_after_move_armed = True
        if e.target_label == "RUN" and not e.runner_engaged and before_bar:
            if target_trigger(side, h, l, trail_from, sp):
                e.runner_engaged = True

    # Trail engaged runners after the stop loop (protects from the next bar on).
    if trail_dist > 0:
        for e in position.open_entries():
            if not (e.target_label == "RUN" and e.runner_engaged and _entry_open_before_bar(e, bar)):
                continue
            if side == "BUY":
                cand = bar.high - trail_dist
                if cand > e.entry_price:
                    e.trailing_stop = cand if e.trailing_stop is None else max(float(e.trailing_stop), cand)
            else:
                cand = bar.low + trail_dist
                if cand < e.entry_price:
                    e.trailing_stop = cand if e.trailing_stop is None else min(float(e.trailing_stop), cand)

    # Max-hold time exit — engaged runners are exempt so they keep trailing.
    if position.time_exit_deadline is not None and bar.time >= position.time_exit_deadline:
        exit_price = _time_exit_price(side, c, sp)
        for e in position.entries:
            if e.status == "OPEN" and not (e.target_label == "RUN" and e.runner_engaged):
                _close_entry(e, "TIME_EXIT", bar.time, exit_price, side, contract_size)

    position.last_processed_time = bar.time


def advance_one_bar(
        position: Position, bar: Bar, config: StrategyConfig,
        contract_size: float = CONTRACT_SIZE_OZ,
) -> None:
    if getattr(config, "per_entry_targets", ()):
        _advance_per_entry_target_bar(position, bar, config, contract_size)
        return

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
        runner_was_active = bool(getattr(position, "trend_runner_active", False))
        runner_should_ratchet_after_stops = bool(trend_runner_holds or runner_was_active)
        # Pure-trail: the scale-out remainder rides the trailing stop past the final
        # target instead of force-closing there.
        pure_trail = bool(
            _scale_out_mode(config)
            and getattr(config, "runner_no_final_cap", False)
            and float(getattr(config, "trailing_close_distance", 0.0) or 0.0) > 0
        )
        if trend_runner_holds and not runner_was_active:
            _engage_trend_runner(position, bar)

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
                exit_fill = _locked_exit_fill(side, stop_level, status, h, l, config)
                _close_entry(e, status, bar.time, exit_fill, side, contract_size, stop_level)
            elif (target_hit and not runner_after_tp3 and not trend_runner_holds
                  and not runner_was_active and not pure_trail and _entry_open_before_bar(e, bar)):
                _close_entry(e, config.final_target.upper(), bar.time, position.target_level,
                             side, contract_size)

        # Scale-out runs after the stop loop so the same-bar SL-wins convention holds:
        # if the worst leg's stop triggered this bar it is already closed, and the
        # scale-out then takes the worst still-open leg at the TP level.
        if getattr(config, "scale_out_at_tp1", False) and tp1_hit and not position.scaled_tp1:
            if _scale_out_worst(position, bar, position.signal.tp1, "TP1", contract_size):
                position.scaled_tp1 = True
        if getattr(config, "scale_out_at_tp2", False) and tp2_hit and not position.scaled_tp2:
            if _scale_out_worst(position, bar, position.signal.tp2, "TP2", contract_size):
                position.scaled_tp2 = True

        stageable_entries = [e for e in position.open_entries() if _entry_open_before_bar(e, bar)]

        if _stage1_active(config) and position.stage1_time is None and tp1_hit and stageable_entries:
            position.stage1_time = bar.time
        if _stage2_active(config) and position.stage2_time is None and tp2_hit and stageable_entries:
            position.stage2_time = bar.time

        if _stage1_active(config) and position.stage < 1 and stageable_entries:
            if _delay_elapsed(position.stage1_time, bar.time, config.tp1_lock_delay_minutes):
                position.stage = 1
        if _stage2_active(config) and position.stage < 2 and stageable_entries:
            if _delay_elapsed(position.stage2_time, bar.time, config.tp2_lock_delay_minutes):
                position.stage = 2
        if runner_after_tp3 and position.stage < 3 and tp3_hit and stageable_entries:
            position.stage = 3
            position.stage3_time = bar.time

        _update_trailing_close_stops(position, bar, config)
        if runner_should_ratchet_after_stops and position.open_entries():
            update_runner_stop(position, bar, config)

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