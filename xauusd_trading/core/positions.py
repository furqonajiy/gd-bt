"""Position lifecycle — the only place that owns state transitions.

`advance_one_bar` mirrors the validated backtest logic exactly:

    1. Try fills on PENDING entries during the pending window using the
       strict-touch arming rule (no marketable / no stale fills).
    2. After the pending window closes, mark unfilled entries NO_FILL.
    3. For OPEN entries, evaluate stop and target with same-bar worst-case
       priority (stop wins when both trigger).
    4. When TP1 is touched, lock remaining open entries to TP1 if configured.
    5. When TP2 is touched, lock remaining open entries to TP2 if configured.
    6. After max_hold_minutes from first fill, time-exit any still-OPEN
       entries at bar close.

Both the engine and the backtest runner call into here.
"""
from __future__ import annotations
import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from .chart import Bar
from xauusd_trading import CONTRACT_SIZE_OZ, StrategyConfig
from xauusd_trading import Signal, compute_entries
from .triggers import (
    fill_trigger, initial_stop_for_entry, stop_trigger, target_trigger,
)


TERMINAL = {"NO_FILL", "SL", "LOCK_TP1", "LOCK_TP2", "TP1", "TP2", "TP3", "TIME_EXIT"}


# ---------------------------------------------------------------------------
# data classes
# ---------------------------------------------------------------------------

@dataclass
class Entry:
    """One entry slot of a Position."""
    entry_index: int
    entry_price: float
    initial_sl: float            # SL applied while position is in stage 0
    lot: float
    status: str = "PENDING"      # PENDING | OPEN | NO_FILL | SL | LOCK_TP1 | LOCK_TP2 | TP1 | TP2 | TP3 | TIME_EXIT
    fill_time: Optional[datetime] = None
    exit_time: Optional[datetime] = None
    exit_price: Optional[float] = None
    pnl: Optional[float] = None
    stop_at_exit: Optional[float] = None
    armed_for_touch: bool = False


@dataclass
class Position:
    """All state for one Signal in flight."""
    signal: Signal
    entries: list[Entry]
    base_stop_distance: float          # raw |entry-SL| * sl_multiplier
    target_level: float                # the configured final target's price
    activation_time: datetime
    expiry_time: datetime              # pending orders die after this
    stage: int = 0                     # 0=initial SL; 1=locked TP1; 2=locked TP2
    stage1_time: Optional[datetime] = None  # bar time TP1 was touched
    stage2_time: Optional[datetime] = None  # bar time TP2 was touched
    first_fill_time: Optional[datetime] = None
    time_exit_deadline: Optional[datetime] = None
    last_processed_time: Optional[datetime] = None
    # Wall-clock placement time (chart tz). Pure metadata: not used by
    # advance_one_bar. Live tooling uses it to render "X min late" and
    # compare ideal-vs-actual replays. Backtest leaves it None.
    executed_at: Optional[datetime] = None

    def is_terminal(self) -> bool:
        return all(e.status in TERMINAL for e in self.entries)

    def filled_entries(self) -> list[Entry]:
        return [e for e in self.entries if e.fill_time is not None]

    def open_entries(self) -> list[Entry]:
        return [e for e in self.entries if e.status == "OPEN"]

    def realized_pnl(self) -> float:
        return sum(e.pnl for e in self.entries if e.pnl is not None)

    def lock_stage_for(self, entry: Entry, lock_after_tp1: bool, lock_after_tp2: bool) -> int:
        """Highest stop-lock stage that applies to this entry.

        Project rule: late fills inherit the current lock stage. If a new
        entry fills after TP1/TP2 was already touched, its stop is immediately
        TP1/TP2 rather than the original SL.
        """
        if entry.fill_time is None:
            return 0
        if lock_after_tp2 and self.stage >= 2:
            return 2
        if lock_after_tp1 and self.stage >= 1:
            return 1
        return 0

    def effective_stop_for(self, entry: Entry, lock_after_tp1: bool, lock_after_tp2: bool = False) -> float:
        """The stop price currently protecting this entry."""
        stage = self.lock_stage_for(entry, lock_after_tp1, lock_after_tp2)
        if stage >= 2:
            return self.signal.tp2
        if stage >= 1:
            return self.signal.tp1
        return entry.initial_sl


# ---------------------------------------------------------------------------
# sizing
# ---------------------------------------------------------------------------

def _floor_to_step(value: float, step: float) -> float:
    """FP-safe floor to a multiple of `step`."""
    if step <= 0:
        return value
    steps = math.floor(value / step + 1e-9)
    out = steps * step
    if math.isclose(step, 0.01, abs_tol=1e-9):
        return round(out, 2)
    return round(out, 8)


def compute_lot(
        equity: float, signal: Signal, config: StrategyConfig,
        contract_size: float = CONTRACT_SIZE_OZ,
) -> tuple[float, float]:
    """Return (lot_per_entry, base_stop_distance)."""
    entries = compute_entries(signal, config)
    if not entries:
        return 0.0, 0.0

    first = entries[0]
    raw_distance = first - signal.sl if signal.side == "BUY" else signal.sl - first
    base_stop_distance = raw_distance * config.sl_multiplier

    if getattr(config, "sizing_mode", "risk") == "fixed":
        lot = getattr(config, "lot_per_entry", 0.0)
        step = config.lot_step if config.lot_step > 0 else 0.01
        lot = _floor_to_step(lot, step)
        if lot < config.minimum_lot - 1e-9:
            lot = 0.0
        return lot, base_stop_distance

    risk_amount = equity * config.risk_per_signal
    total_price_risk = sum(
        abs(e - initial_stop_for_entry(signal.side, e, base_stop_distance))
        for e in entries
    )
    if total_price_risk <= 0:
        return 0.0, base_stop_distance
    lot = risk_amount / (total_price_risk * contract_size)

    step = config.lot_step if config.lot_step > 0 else 0.01
    min_lot = config.minimum_lot if config.minimum_lot > 0 else 0.01
    lot = _floor_to_step(lot, step)
    if lot < min_lot - 1e-9:
        lot = 0.0
    return lot, base_stop_distance


def _pnl(side: str, entry: float, exit_price: float, lot: float, contract_size: float) -> float:
    return (exit_price - entry) * lot * contract_size if side == "BUY" else (entry - exit_price) * lot * contract_size


# ---------------------------------------------------------------------------
# construction
# ---------------------------------------------------------------------------

def open_position(
        signal: Signal, equity: float, config: StrategyConfig,
        contract_size: float = CONTRACT_SIZE_OZ,
) -> Position:
    """Create a fresh Position with PENDING entries from a Signal."""
    lot, base_stop_distance = compute_lot(equity, signal, config, contract_size)
    entries_prices = compute_entries(signal, config)
    entries = [
        Entry(
            entry_index=i, entry_price=p,
            initial_sl=initial_stop_for_entry(signal.side, p, base_stop_distance),
            lot=lot,
        )
        for i, p in enumerate(entries_prices)
    ]
    target = {"TP1": signal.tp1, "TP2": signal.tp2, "TP3": signal.tp3}[config.final_target.upper()]
    activation = signal.signal_time_chart + timedelta(minutes=config.activation_delay_minutes)
    expiry = activation + timedelta(minutes=config.pending_expiry_minutes)
    return Position(
        signal=signal, entries=entries, base_stop_distance=base_stop_distance,
        target_level=target, activation_time=activation, expiry_time=expiry,
    )


# ---------------------------------------------------------------------------
# bar-by-bar advancement
# ---------------------------------------------------------------------------

def _close_entry(
        entry: Entry, status: str, t: datetime, exit_price: float,
        side: str, contract_size: float, stop_at: Optional[float] = None,
) -> None:
    entry.status = status
    entry.exit_time = t
    entry.exit_price = exit_price
    entry.stop_at_exit = stop_at
    entry.pnl = _pnl(side, entry.entry_price, exit_price, entry.lot, contract_size)


def _target_levels_hit(position: Position, side: str, h: float, l: float, sp: float) -> tuple[bool, bool, bool, bool]:
    tp1_hit = target_trigger(side, h, l, position.signal.tp1, sp)
    tp2_hit = target_trigger(side, h, l, position.signal.tp2, sp)
    tp3_hit = target_trigger(side, h, l, position.signal.tp3, sp)
    target_hit = target_trigger(side, h, l, position.target_level, sp)
    return tp1_hit, tp2_hit, tp3_hit, target_hit


def advance_one_bar(
        position: Position, bar: Bar, config: StrategyConfig,
        contract_size: float = CONTRACT_SIZE_OZ,
) -> None:
    """Mutate `position` state to reflect one minute of price action."""
    side = position.signal.side
    sp = bar.spread_price
    h, l, c = bar.high, bar.low, bar.close

    # 1. Fills, with strict-touch arming.
    if position.activation_time <= bar.time <= position.expiry_time:
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
                    # Bar visited safe side; OHLC alone can't order events
                    # within the bar. Arm and require a touch on a later bar.
                    e.armed_for_touch = True
                    continue
                else:
                    continue
            if fill_trigger(side, h, l, e.entry_price, sp):
                e.status = "OPEN"
                e.fill_time = bar.time
                if position.first_fill_time is None:
                    position.first_fill_time = bar.time
                    position.time_exit_deadline = bar.time + timedelta(minutes=config.max_hold_minutes)

    # 2. Pending expiry.
    if bar.time > position.expiry_time:
        for e in position.entries:
            if e.status == "PENDING":
                e.status = "NO_FILL"

    # 3. Stop / target. Worst-case: active stop wins same bar.
    open_entries = position.open_entries()
    if open_entries:
        tp1_hit, tp2_hit, _tp3_hit, target_hit = _target_levels_hit(position, side, h, l, sp)

        for e in list(open_entries):
            stop_level = position.effective_stop_for(e, config.lock_after_tp1, config.lock_after_tp2)
            lock_stage = position.lock_stage_for(e, config.lock_after_tp1, config.lock_after_tp2)
            if stop_trigger(side, h, l, stop_level, sp):
                status = "LOCK_TP2" if lock_stage >= 2 else "LOCK_TP1" if lock_stage >= 1 else "SL"
                _close_entry(e, status, bar.time, stop_level, side, contract_size, stop_level)
            elif target_hit:
                _close_entry(e, config.final_target.upper(), bar.time, position.target_level,
                             side, contract_size)

        # 4. Stage advances happen after the stop/target check so a same-bar
        #    target touch cannot create a retroactive stop on the same candle.
        if config.lock_after_tp1 and position.stage < 1 and tp1_hit and position.open_entries():
            position.stage = 1
            position.stage1_time = bar.time
        if config.lock_after_tp2 and position.stage < 2 and tp2_hit and position.open_entries():
            position.stage = 2
            position.stage2_time = bar.time

    # 5. Time exit at first bar at/after the deadline; closes at bar close.
    if position.time_exit_deadline is not None and bar.time >= position.time_exit_deadline:
        for e in position.entries:
            if e.status == "OPEN":
                _close_entry(e, "TIME_EXIT", bar.time, c, side, contract_size)

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
