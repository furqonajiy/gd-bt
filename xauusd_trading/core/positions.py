"""Position lifecycle — the only place that owns state transitions.

`advance_one_bar` mirrors the validated backtest logic exactly:

    1. Try fills on PENDING entries during the pending window using the
       strict-touch arming rule (no marketable / no stale fills).
    2. After the pending window closes, mark unfilled entries NO_FILL.
    3. For OPEN entries, evaluate stop and target with same-bar worst-case
       priority (stop wins when both trigger).
    4. Apply configurable stop-lock model.
    5. After max_hold_minutes from first fill, time-exit any still-OPEN
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


TERMINAL = {
    "NO_FILL", "SL", "BEP", "LOCK_HALF_TP1", "LOCK_TP1", "LOCK_TP2",
    "TP1", "TP2", "TP3", "TIME_EXIT",
}


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
    status: str = "PENDING"      # PENDING | OPEN | NO_FILL | SL | BEP | LOCK_HALF_TP1 | LOCK_TP1 | LOCK_TP2 | TP1 | TP2 | TP3 | TIME_EXIT
    fill_time: Optional[datetime] = None
    exit_time: Optional[datetime] = None
    exit_price: Optional[float] = None
    pnl: Optional[float] = None
    stop_at_exit: Optional[float] = None
    armed_for_touch: bool = False
    bep_armed: bool = False


@dataclass
class Position:
    """All state for one Signal in flight."""
    signal: Signal
    entries: list[Entry]
    base_stop_distance: float          # raw |entry-SL| * sl_multiplier
    target_level: float                # the configured final target's price
    activation_time: datetime
    expiry_time: datetime              # pending orders die after this
    stage: int = 0                     # tp_levels: 0=SL,1=TP1,2=TP2,3=TP3 runner lock
    stage1_time: Optional[datetime] = None  # bar time TP1 was first touched
    stage2_time: Optional[datetime] = None  # bar time TP2 was first touched
    stage3_time: Optional[datetime] = None  # bar time TP3 was touched in runner mode
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

    def _stage_touch_applies_to(self, entry: Entry, touch_time: Optional[datetime]) -> bool:
        """Whether a global target touch can protect this specific entry.

        M1 OHLC cannot prove event order inside the candle.  A TP1/TP2 touch is
        therefore only allowed to protect entries that were already filled on a
        strictly earlier bar (or, in live reconciliation, before the touch
        timestamp).  This prevents later ladder fills from inheriting a profit
        lock that happened before they existed.
        """
        return (
            entry.fill_time is not None
            and touch_time is not None
            and entry.fill_time < touch_time
        )

    def lock_stage_for(self, entry: Entry, lock_after_tp1: bool, lock_after_tp2: bool) -> int:
        """Highest stop-lock stage that applies to this entry.

        Target touches are global, but they cannot be applied retroactively to
        entries that filled on the same M1 candle or after the target touch.
        """
        if entry.fill_time is None:
            return 0
        if self.stage >= 3 and self._stage_touch_applies_to(entry, self.stage3_time):
            return 3
        if (
            lock_after_tp2
            and self.stage >= 2
            and self._stage_touch_applies_to(entry, self.stage2_time)
        ):
            return 2
        if (
            lock_after_tp1
            and self.stage >= 1
            and self._stage_touch_applies_to(entry, self.stage1_time)
        ):
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
        # User-requested runner rule: after TP3, lock stop profit to TP2.
        return self.signal.tp2

    def effective_stop_for(self, entry: Entry, config_or_lock_after_tp1, lock_after_tp2: bool = False) -> float:
        """The stop price currently protecting this entry.

        Backwards-compatible call styles:
        - effective_stop_for(entry, config)
        - effective_stop_for(entry, lock_after_tp1, lock_after_tp2)
        """
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
                return self._tp3_runner_stop(config)
            if stage >= 2:
                return self._tp2_lock_stop(config)
            if stage >= 1:
                return self._half_tp1_stop_for(entry, config.tp1_lock_fraction)
            if entry.bep_armed:
                return entry.entry_price
            return entry.initial_sl

        if stage >= 3:
            return self.signal.tp2
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


def _bep_triggered(side: str, entry: Entry, h: float, l: float, sp: float, trigger_distance: float) -> bool:
    if trigger_distance <= 0:
        return True
    level = entry.entry_price + trigger_distance if side == "BUY" else entry.entry_price - trigger_distance
    return target_trigger(side, h, l, level, sp)


def _stop_status(lock_stage: int, stop_level: float, entry: Entry, config: StrategyConfig) -> str:
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
    if first_touch is None:
        return False
    return current_time >= first_touch + timedelta(minutes=max(0, int(delay_minutes)))


def _time_exit_price(side: str, close_bid: float, spread_price: float) -> float:
    """Market-equivalent time-exit price from a bid-based chart bar."""
    return close_bid if side == "BUY" else close_bid + spread_price


def advance_one_bar(
        position: Position, bar: Bar, config: StrategyConfig,
        contract_size: float = CONTRACT_SIZE_OZ,
) -> None:
    """Mutate `position` state to reflect one minute of price action."""
    side = position.signal.side
    sp = bar.spread_price
    h, l, c = bar.high, bar.low, bar.close

    # Entries that were open before this M1 candle can legitimately respond to
    # this candle's target touches. Entries filled inside this same candle cannot:
    # OHLC data cannot prove whether target happened before or after the fill.
    def _entry_open_before_bar(entry: Entry) -> bool:
        return entry.status == "OPEN" and entry.fill_time is not None and entry.fill_time < bar.time

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
        tp1_hit, tp2_hit, tp3_hit, target_hit = _target_levels_hit(position, side, h, l, sp)
        runner_after_tp3 = bool(getattr(config, "runner_after_tp3", False)) and config.final_target.upper() == "TP3"

        # Early BEP is applied before stop evaluation. Same-bar worst-case still
        # applies: if +3 and BEP are both possible inside one M1 bar, BEP stop wins.
        if getattr(config, "profit_lock_mode", "tp_levels") == "bep_plus_half_tp1":
            for e in open_entries:
                if (
                    _entry_open_before_bar(e)
                    and not e.bep_armed
                    and _bep_triggered(side, e, h, l, sp, config.bep_trigger_distance)
                ):
                    e.bep_armed = True

        for e in list(open_entries):
            stop_level = position.effective_stop_for(e, config)
            lock_stage = position.lock_stage_for(e, config.lock_after_tp1, config.lock_after_tp2)
            if stop_trigger(side, h, l, stop_level, sp):
                status = _stop_status(lock_stage, stop_level, e, config)
                _close_entry(e, status, bar.time, stop_level, side, contract_size, stop_level)
            elif target_hit and not runner_after_tp3 and _entry_open_before_bar(e):
                _close_entry(e, config.final_target.upper(), bar.time, position.target_level,
                             side, contract_size)

        stageable_entries = [e for e in position.open_entries() if _entry_open_before_bar(e)]

        # 4. Stage touch times are remembered only when at least one still-open
        #    entry existed before this M1 candle. This avoids retroactively
        #    granting TP1/TP2 locks from target touches that may have happened
        #    before same-bar or later ladder fills.
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

    # 5. Time exit at first bar at/after the deadline; closes at market-side bar close.
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
