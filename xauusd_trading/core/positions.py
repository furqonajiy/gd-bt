"""Position primitives shared by the single wired lifecycle engine.

`core.trailing_positions` is the only bar-advance engine.  This module keeps the
state containers, sizing, construction, P&L, stop/target helper primitives, and
status helpers that the wired engine imports.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from .triggers import initial_stop_for_entry, target_trigger
from xauusd_trading import CONTRACT_SIZE_OZ, Signal, StrategyConfig, compute_entries


TERMINAL = {
    "NO_FILL", "SL", "BEP", "LOCK_HALF_TP1", "LOCK_TP1", "LOCK_TP2",
    "TP1", "TP2", "TP3", "TIME_EXIT", "TRAILING_STOP",
}


def _scale_out_mode(config: StrategyConfig) -> bool:
    """True when any multi-entry scale-out flag is set.

    Gates the alternate stop ladder so legacy runs (all flags off) are untouched.
    """
    return bool(
        getattr(config, "scale_out_at_tp1", False)
        or getattr(config, "scale_out_at_tp2", False)
        or getattr(config, "bep_after_tp1", False)
        or int(getattr(config, "trailing_close_after_stage", 0) or 0) > 0
        or getattr(config, "runner_no_final_cap", False)
    )


def _stage1_active(config: StrategyConfig) -> bool:
    # Broadened so stage promotion also fires when a stage-1-dependent scale-out
    # feature is enabled; with all new flags off this equals lock_after_tp1.
    return bool(
        getattr(config, "lock_after_tp1", False)
        or getattr(config, "bep_after_tp1", False)
        or int(getattr(config, "trailing_close_after_stage", 0) or 0) == 1
    )


def _stage2_active(config: StrategyConfig) -> bool:
    return bool(
        getattr(config, "lock_after_tp2", False)
        or int(getattr(config, "trailing_close_after_stage", 0) or 0) == 2
    )


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
    scaled_tp1: bool = False
    scaled_tp2: bool = False

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

    def _raw_stage_for(self, entry: Entry) -> int:
        # Same touch-applies gating as lock_stage_for but without the lock flags, so
        # the scale-out ladder can key off the real stage independent of legacy locks.
        if entry.fill_time is None:
            return 0
        if self.stage >= 3 and self._stage_touch_applies_to(entry, self.stage3_time):
            return 3
        if self.stage >= 2 and self._stage_touch_applies_to(entry, self.stage2_time):
            return 2
        if self.stage >= 1 and self._stage_touch_applies_to(entry, self.stage1_time):
            return 1
        return 0

    def _bep_lock_stop(self, entry: Entry, buffer: float) -> float:
        buf = max(0.0, float(buffer))
        return entry.entry_price + buf if self.signal.side == "BUY" else entry.entry_price - buf

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
        if entry.trailing_stop is None:
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

        if config is not None and _scale_out_mode(config):
            # Scale-out ladder, independent of legacy locks:
            #   stage 0  -> initial SL
            #   stage>=1 -> BEP + buffer (if bep_after_tp1)
            # The ratcheting trailing stop is folded in only once the leg reaches
            # the configured engage stage (trailing_close_after_stage; 0 = from open).
            eff_stage = self._raw_stage_for(entry)
            if getattr(config, "bep_after_tp1", False) and eff_stage >= 1:
                stop = self._bep_lock_stop(entry, getattr(config, "bep_buffer", 0.0))
            else:
                stop = entry.initial_sl
            after = int(getattr(config, "trailing_close_after_stage", 0) or 0)
            if after > 0 and eff_stage < after:
                return stop
            return self._combine_with_trailing_stop(entry, stop, config)

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
    # A sizeable signal always trades at least the broker minimum: when the
    # computed per-entry lot floors below `minimum_lot` we clamp it UP to the
    # minimum (e.g. an 8-entry ladder on a small risk budget) instead of zeroing
    # the entry. Only genuinely unsizable signals (no entries, zero price-risk)
    # stay at 0.0.
    min_lot = config.minimum_lot if config.minimum_lot > 0 else 0.01
    if getattr(config, "sizing_mode", "risk") == "fixed":
        lot = _floor_to_step(getattr(config, "lot_per_entry", 0.0), config.lot_step if config.lot_step > 0 else 0.01)
        return max(lot, min_lot), base_stop_distance
    risk_amount = equity * config.risk_per_signal
    total_price_risk = sum(abs(e - initial_stop_for_entry(signal.side, e, base_stop_distance)) for e in entries)
    if total_price_risk <= 0:
        return 0.0, base_stop_distance
    lot = _floor_to_step(risk_amount / (total_price_risk * contract_size), config.lot_step if config.lot_step > 0 else 0.01)
    return max(lot, min_lot), base_stop_distance


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
    if _scale_out_mode(config):
        # lock_stage is legacy-gated (0 when locks are off), so detect the BEP lock
        # by matching the stop to the entry +/- buffer level instead.
        buf = abs(float(getattr(config, "bep_buffer", 0.0)))
        if getattr(config, "bep_after_tp1", False) and (
            abs(stop_level - (entry.entry_price + buf)) < 1e-9
            or abs(stop_level - (entry.entry_price - buf)) < 1e-9
        ):
            return "BEP"
        return "SL"
    return "LOCK_TP2" if lock_stage >= 2 else "LOCK_TP1" if lock_stage >= 1 else "SL"


def _delay_elapsed(first_touch: Optional[datetime], current_time: datetime, delay_minutes: int) -> bool:
    return first_touch is not None and current_time >= first_touch + timedelta(minutes=max(0, int(delay_minutes)))


def _time_exit_price(side: str, close_bid: float, spread_price: float) -> float:
    return close_bid if side == "BUY" else close_bid + spread_price
