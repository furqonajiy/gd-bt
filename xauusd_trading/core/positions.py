"""Shared position lifecycle used by backtest and live replay."""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from .chart import Bar
from .triggers import fill_trigger, initial_stop_for_entry, stop_trigger, target_trigger
from xauusd_trading import CONTRACT_SIZE_OZ, Signal, StrategyConfig, compute_entries


TERMINAL = {
    "NO_FILL", "SL", "BEP", "LOCK_HALF_TP1", "LOCK_TP1", "LOCK_TP2",
    "TP1", "TP2", "TP3", "TIME_EXIT", "TRAILING_STOP",
}


@dataclass
class Entry:
    entry_index: int
    entry_price: float
    initial_sl: float
    lot: float
    status: str = "PENDING"
    fill_time: Optional[datetime] = None
    exit_time: Optional[datetime] = None
    exit_price: Optional[float] = None
    pnl: Optional[float] = None
    stop_at_exit: Optional[float] = None
    armed_for_touch: bool = False
    bep_armed: bool = False
    trailing_open_extreme: Optional[float] = None
    trailing_open_touched_at: Optional[datetime] = None
    trailing_stop: Optional[float] = None


@dataclass
class Position:
    signal: Signal
    entries: list[Entry]
    base_stop_distance: float
    target_level: float
    activation_time: datetime
    expiry_time: datetime
    stage: int = 0
    stage1_time: Optional[datetime] = None
    stage2_time: Optional[datetime] = None
    stage3_time: Optional[datetime] = None
    first_fill_time: Optional[datetime] = None
    time_exit_deadline: Optional[datetime] = None
    last_processed_time: Optional[datetime] = None
    executed_at: Optional[datetime] = None

    def is_terminal(self) -> bool:
        return all(e.status in TERMINAL for e in self.entries)

    def filled_entries(self) -> list[Entry]:
        return [e for e in self.entries if e.fill_time is not None]

    def open_entries(self) -> list[Entry]:
        return [e for e in self.entries if e.status == "OPEN"]

    def realized_pnl(self) -> float:
        return sum(e.pnl for e in self.entries if e.pnl is not None)

    def _stage_touch_applies_to(self, entry: Entry, touch_time: Optional[datetime]) -> bool:
        return entry.fill_time is not None and touch_time is not None and entry.fill_time < touch_time

    def lock_stage_for(self, entry: Entry, lock_after_tp1: bool, lock_after_tp2: bool) -> int:
        if entry.fill_time is None:
            return 0
        if self.stage >= 3 and self._stage_touch_applies_to(entry, self.stage3_time):
            return 3
        if lock_after_tp2 and self.stage >= 2 and self._stage_touch_applies_to(entry, self.stage2_time):
            return 2
        if lock_after_tp1 and self.stage >= 1 and self._stage_touch_applies_to(entry, self.stage1_time):
            return 1
        return 0

    def _half_tp1_stop_for(self, entry: Entry, fraction: float) -> float:
        fraction = max(0.0, min(1.0, float(fraction)))
        if self.signal.side == "BUY":
            return entry.entry_price + (self.signal.tp1 - entry.entry_price) * fraction
        return entry.entry_price - (entry.entry_price - self.signal.tp1) * fraction

    def _tp2_lock_stop(self, config: StrategyConfig) -> float:
        return self.signal.tp2 if getattr(config, "tp2_lock_target", "TP1").upper() == "TP2" else self.signal.tp1

    def _tp3_runner_stop(self, config: StrategyConfig) -> float:
        return self.signal.tp2

    def _combine_with_trailing_stop(self, entry: Entry, stop_level: float, config: StrategyConfig) -> float:
        if getattr(config, "trailing_close_distance", 0.0) <= 0 or entry.trailing_stop is None:
            return stop_level
        return max(stop_level, entry.trailing_stop) if self.signal.side == "BUY" else min(stop_level, entry.trailing_stop)

    def effective_stop_for(self, entry: Entry, config_or_lock_after_tp1, lock_after_tp2: bool = False) -> float:
        if isinstance(config_or_lock_after_tp1, StrategyConfig):
            config = config_or_lock_after_tp1
            lock_after_tp1 = config.lock_after_tp1
            lock_after_tp2 = config.lock_after_tp2
            mode = getattr(config, "profit_lock_mode", "tp_levels")
        else:
            config = None
            lock_after_tp1 = bool(config_or_lock_after_tp1)
            mode = "tp_levels"

        stage = self.lock_stage_for(entry, lock_after_tp1, lock_after_tp2)
        if mode == "bep_plus_half_tp1" and config is not None:
            if stage >= 3:
                stop = self._tp3_runner_stop(config)
            elif stage >= 2:
                stop = self._tp2_lock_stop(config)
            elif stage >= 1:
                stop = self._half_tp1_stop_for(entry, config.tp1_lock_fraction)
            elif entry.bep_armed:
                stop = entry.entry_price
            else:
                stop = entry.initial_sl
            return self._combine_with_trailing_stop(entry, stop, config)

        if stage >= 3:
            stop = self.signal.tp2
        elif stage >= 2:
            stop = self.signal.tp2
        elif stage >= 1:
            stop = self.signal.tp1
        else:
            stop = entry.initial_sl
        return self._combine_with_trailing_stop(entry, stop, config) if config is not None else stop


def _floor_to_step(value: float, step: float) -> float:
    if step <= 0:
        return value
    steps = math.floor(value / step + 1e-9)
    out = steps * step
    return round(out, 2) if math.isclose(step, 0.01, abs_tol=1e-9) else round(out, 8)


def compute_lot(equity: float, signal: Signal, config: StrategyConfig, contract_size: float = CONTRACT_SIZE_OZ) -> tuple[float, float]:
    entries = compute_entries(signal, config)
    if not entries:
        return 0.0, 0.0
    first = entries[0]
    raw_distance = first - signal.sl if signal.side == "BUY" else signal.sl - first
    base_stop_distance = raw_distance * config.sl_multiplier
    if getattr(config, "sizing_mode", "risk") == "fixed":
        lot = _floor_to_step(getattr(config, "lot_per_entry", 0.0), config.lot_step if config.lot_step > 0 else 0.01)
        return (0.0 if lot < config.minimum_lot - 1e-9 else lot), base_stop_distance
    risk_amount = equity * config.risk_per_signal
    total_price_risk = sum(abs(e - initial_stop_for_entry(signal.side, e, base_stop_distance)) for e in entries)
    if total_price_risk <= 0:
        return 0.0, base_stop_distance
    lot = _floor_to_step(risk_amount / (total_price_risk * contract_size), config.lot_step if config.lot_step > 0 else 0.01)
    min_lot = config.minimum_lot if config.minimum_lot > 0 else 0.01
    return (0.0 if lot < min_lot - 1e-9 else lot), base_stop_distance


def _pnl(side: str, entry: float, exit_price: float, lot: float, contract_size: float) -> float:
    return (exit_price - entry) * lot * contract_size if side == "BUY" else (entry - exit_price) * lot * contract_size


def open_position(signal: Signal, equity: float, config: StrategyConfig, contract_size: float = CONTRACT_SIZE_OZ) -> Position:
    lot, base_stop_distance = compute_lot(equity, signal, config, contract_size)
    entries = [
        Entry(i, p, initial_stop_for_entry(signal.side, p, base_stop_distance), lot)
        for i, p in enumerate(compute_entries(signal, config))
    ]
    target = {"TP1": signal.tp1, "TP2": signal.tp2, "TP3": signal.tp3}[config.final_target.upper()]
    activation = signal.signal_time_chart + timedelta(minutes=config.activation_delay_minutes)
    return Position(signal, entries, base_stop_distance, target, activation, activation + timedelta(minutes=config.pending_expiry_minutes))


def _close_entry(entry: Entry, status: str, t: datetime, exit_price: float, side: str, contract_size: float, stop_at: Optional[float] = None) -> None:
    entry.status = status
    entry.exit_time = t
    entry.exit_price = exit_price
    entry.stop_at_exit = stop_at
    entry.pnl = _pnl(side, entry.entry_price, exit_price, entry.lot, contract_size)


def _target_levels_hit(position: Position, side: str, h: float, l: float, sp: float) -> tuple[bool, bool, bool, bool]:
    return (
        target_trigger(side, h, l, position.signal.tp1, sp),
        target_trigger(side, h, l, position.signal.tp2, sp),
        target_trigger(side, h, l, position.signal.tp3, sp),
        target_trigger(side, h, l, position.target_level, sp),
    )


def _bep_triggered(side: str, entry: Entry, h: float, l: float, sp: float, trigger_distance: float) -> bool:
    if trigger_distance <= 0:
        return True
    level = entry.entry_price + trigger_distance if side == "BUY" else entry.entry_price - trigger_distance
    return target_trigger(side, h, l, level, sp)


def _stop_status(lock_stage: int, stop_level: float, entry: Entry, config: StrategyConfig) -> str:
    if getattr(config, "trailing_close_distance", 0.0) > 0 and entry.trailing_stop is not None and abs(stop_level - entry.trailing_stop) < 1e-9:
        return "TRAILING_STOP"
    mode = getattr(config, "profit_lock_mode", "tp_levels")
    if mode == "bep_plus_half_tp1":
        if lock_stage >= 3:
            return "LOCK_TP2"
        if lock_stage >= 2:
            return "LOCK_TP2" if getattr(config, "tp2_lock_target", "TP1").upper() == "TP2" else "LOCK_TP1"
        if lock_stage >= 1:
            return "LOCK_HALF_TP1"
        if entry.bep_armed and abs(stop_level - entry.entry_price) < 1e-9:
            return "BEP"
        return "SL"
    return "LOCK_TP2" if lock_stage >= 2 else "LOCK_TP1" if lock_stage >= 1 else "SL"


def _delay_elapsed(first_touch: Optional[datetime], current_time: datetime, delay_minutes: int) -> bool:
    return first_touch is not None and current_time >= first_touch + timedelta(minutes=max(0, int(delay_minutes)))


def _time_exit_price(side: str, close_bid: float, spread_price: float) -> float:
    return close_bid if side == "BUY" else close_bid + spread_price


def _open_entry(entry: Entry, position: Position, fill_price: float, fill_time: datetime, config: StrategyConfig) -> None:
    entry.status = "OPEN"
    entry.fill_time = fill_time
    if getattr(config, "trailing_open_distance", 0.0) > 0:
        entry.entry_price = fill_price
        entry.initial_sl = initial_stop_for_entry(position.signal.side, fill_price, position.base_stop_distance)
    if getattr(config, "trailing_close_distance", 0.0) > 0:
        d = float(config.trailing_close_distance)
        entry.trailing_stop = fill_price - d if position.signal.side == "BUY" else fill_price + d
    if position.first_fill_time is None:
        position.first_fill_time = fill_time
        position.time_exit_deadline = fill_time + timedelta(minutes=config.max_hold_minutes)


def _try_standard_fill(position: Position, e: Entry, bar: Bar, config: StrategyConfig) -> None:
    side = position.signal.side
    sp = bar.spread_price
    h, l = bar.high, bar.low
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
            return
        else:
            return
    if fill_trigger(side, h, l, e.entry_price, sp):
        _open_entry(e, position, e.entry_price, bar.time, config)


def _try_trailing_open_fill(position: Position, e: Entry, bar: Bar, config: StrategyConfig) -> None:
    d = float(getattr(config, "trailing_open_distance", 0.0) or 0.0)
    if d <= 0:
        _try_standard_fill(position, e, bar, config)
        return
    side = position.signal.side
    sp = bar.spread_price
    ask_low, ask_high = bar.low + sp, bar.high + sp
    bid_low, bid_high = bar.low, bar.high
    if side == "BUY":
        if ask_low <= e.entry_price:
            if e.trailing_open_extreme is None or ask_low < e.trailing_open_extreme:
                e.trailing_open_extreme = ask_low
                e.trailing_open_touched_at = bar.time
        if e.trailing_open_extreme is None:
            return
        trigger = e.trailing_open_extreme + d
        if e.trailing_open_touched_at is not None and bar.time <= e.trailing_open_touched_at:
            return
        if ask_high >= trigger:
            _open_entry(e, position, trigger, bar.time, config)
    else:
        if bid_high >= e.entry_price:
            if e.trailing_open_extreme is None or bid_high > e.trailing_open_extreme:
                e.trailing_open_extreme = bid_high
                e.trailing_open_touched_at = bar.time
        if e.trailing_open_extreme is None:
            return
        trigger = e.trailing_open_extreme - d
        if e.trailing_open_touched_at is not None and bar.time <= e.trailing_open_touched_at:
            return
        if bid_low <= trigger:
            _open_entry(e, position, trigger, bar.time, config)


def _update_trailing_close(position: Position, entry: Entry, bar: Bar, config: StrategyConfig) -> None:
    d = float(getattr(config, "trailing_close_distance", 0.0) or 0.0)
    if d <= 0 or entry.status != "OPEN" or entry.fill_time is None or entry.fill_time >= bar.time:
        return
    if position.signal.side == "BUY":
        candidate = bar.high - d
        entry.trailing_stop = candidate if entry.trailing_stop is None else max(entry.trailing_stop, candidate)
    else:
        candidate = bar.low + d
        entry.trailing_stop = candidate if entry.trailing_stop is None else min(entry.trailing_stop, candidate)


def advance_one_bar(position: Position, bar: Bar, config: StrategyConfig, contract_size: float = CONTRACT_SIZE_OZ) -> None:
    side = position.signal.side
    sp = bar.spread_price
    h, l, c = bar.high, bar.low, bar.close

    def _entry_open_before_bar(entry: Entry) -> bool:
        return entry.status == "OPEN" and entry.fill_time is not None and entry.fill_time < bar.time

    if position.activation_time <= bar.time <= position.expiry_time:
        for e in position.entries:
            if e.status == "PENDING":
                _try_trailing_open_fill(position, e, bar, config)

    if bar.time > position.expiry_time:
        for e in position.entries:
            if e.status == "PENDING":
                e.status = "NO_FILL"

    open_entries = position.open_entries()
    if open_entries:
        tp1_hit, tp2_hit, tp3_hit, target_hit = _target_levels_hit(position, side, h, l, sp)
        runner_after_tp3 = bool(getattr(config, "runner_after_tp3", False)) and config.final_target.upper() == "TP3"
        if getattr(config, "profit_lock_mode", "tp_levels") == "bep_plus_half_tp1":
            for e in open_entries:
                if _entry_open_before_bar(e) and not e.bep_armed and _bep_triggered(side, e, h, l, sp, config.bep_trigger_distance):
                    e.bep_armed = True
        for e in list(open_entries):
            stop_level = position.effective_stop_for(e, config)
            lock_stage = position.lock_stage_for(e, config.lock_after_tp1, config.lock_after_tp2)
            if stop_trigger(side, h, l, stop_level, sp):
                _close_entry(e, _stop_status(lock_stage, stop_level, e, config), bar.time, stop_level, side, contract_size, stop_level)
            elif target_hit and not runner_after_tp3 and _entry_open_before_bar(e):
                _close_entry(e, config.final_target.upper(), bar.time, position.target_level, side, contract_size)
        for e in position.open_entries():
            _update_trailing_close(position, e, bar, config)
        stageable_entries = [e for e in position.open_entries() if _entry_open_before_bar(e)]
        if config.lock_after_tp1 and position.stage1_time is None and tp1_hit and stageable_entries:
            position.stage1_time = bar.time
        if config.lock_after_tp2 and position.stage2_time is None and tp2_hit and stageable_entries:
            position.stage2_time = bar.time
        if config.lock_after_tp1 and position.stage < 1 and stageable_entries and _delay_elapsed(position.stage1_time, bar.time, config.tp1_lock_delay_minutes):
            position.stage = 1
        if config.lock_after_tp2 and position.stage < 2 and stageable_entries and _delay_elapsed(position.stage2_time, bar.time, config.tp2_lock_delay_minutes):
            position.stage = 2
        if runner_after_tp3 and position.stage < 3 and tp3_hit and stageable_entries:
            position.stage = 3
            position.stage3_time = bar.time

    if position.time_exit_deadline is not None and bar.time >= position.time_exit_deadline:
        exit_price = _time_exit_price(side, c, sp)
        for e in position.entries:
            if e.status == "OPEN":
                _close_entry(e, "TIME_EXIT", bar.time, exit_price, side, contract_size)
    position.last_processed_time = bar.time


def advance_bars(position: Position, bars, config: StrategyConfig, contract_size: float = CONTRACT_SIZE_OZ) -> None:
    for bar in bars:
        advance_one_bar(position, bar, config, contract_size)
        if position.is_terminal():
            break
