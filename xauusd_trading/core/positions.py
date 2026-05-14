"""Position lifecycle.

A Position wraps one Signal and its entry slots (count driven by config).
State is advanced bar by bar via `advance_one_bar`, which mirrors the
validated backtest logic exactly:

    1. Try fills on PENDING entries during the pending window using the
       strict-touch arming rule (no marketable / no stale fills).
    2. After the pending window closes, mark unfilled entries NO_FILL.
    3. For OPEN entries, evaluate stop and target with same-bar worst-case
       priority (stop wins when both trigger).
    4. If TP1 was touched while at least one entry is OPEN, lock to stage 1
       (remaining and any future late-fill stops move to TP1).
    5. After max_hold_minutes from first fill, time-exit any still-OPEN
       entries at bar close.

This module is the only place that owns these state transitions. The engine
and backtest runner both call it.
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


# Terminal entry states (no further evolution possible).
TERMINAL = {"NO_FILL", "SL", "LOCK_TP1", "TP1", "TP2", "TP3", "TIME_EXIT"}


# ---------------------------------------------------------------------------
# data classes
# ---------------------------------------------------------------------------

@dataclass
class Entry:
    """One entry slot of a Position."""
    entry_index: int
    entry_price: float
    initial_sl: float            # planned SL applied while position is in stage 0
    lot: float
    status: str = "PENDING"      # PENDING | OPEN | NO_FILL | SL | LOCK_TP1 | TP2 | TIME_EXIT
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
    stage: int = 0                     # 0 = initial SL; 1 = locked at TP1
    first_fill_time: Optional[datetime] = None
    time_exit_deadline: Optional[datetime] = None
    last_processed_time: Optional[datetime] = None
    # Wall-clock placement time (chart tz, GMT+3). Pure metadata: not used by
    # `advance_one_bar` or any state-transition logic. Callers set it after
    # construction so live tooling can render "X min late" and compare an
    # ideal-execution replay (from activation_time) with an actual-execution
    # replay (from executed_at). Optional; backtest leaves it None.
    executed_at: Optional[datetime] = None

    def is_terminal(self) -> bool:
        return all(e.status in TERMINAL for e in self.entries)

    def filled_entries(self) -> list[Entry]:
        return [e for e in self.entries if e.fill_time is not None]

    def open_entries(self) -> list[Entry]:
        return [e for e in self.entries if e.status == "OPEN"]

    def realized_pnl(self) -> float:
        return sum(e.pnl for e in self.entries if e.pnl is not None)

    def effective_stop_for(self, entry: Entry, lock_after_tp1: bool) -> float:
        """The stop price currently protecting this entry."""
        if lock_after_tp1 and self.stage >= 1:
            return self.signal.tp1
        return entry.initial_sl


# ---------------------------------------------------------------------------
# sizing
# ---------------------------------------------------------------------------

def _floor_to_step(value: float, step: float) -> float:
    """FP-safe floor to a multiple of `step`. Returns a clean 2-decimal float
    when step == 0.01 (no 0.15 -> 0.14 floating-point dust). For other steps,
    rounds to 8 decimals.
    """
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
    """Return (lot_per_entry, base_stop_distance).

    Entries are computed from (signal, config) via compute_entries().
    All entries get the same lot, sized so total initial-SL price-risk across
    all planned entries equals risk_per_signal x equity, then floored to the
    broker lot step and clamped to the broker minimum.

    Lot rounding is UNCONDITIONAL: if `config.lot_step` is 0 (disabled), the
    engine still falls back to a 0.01 step so backtest output and live orders
    are always clean multiples. Same for `minimum_lot`. To use a coarser step
    (e.g. 0.1), set the config value above 0.01 -- it always wins.

    Realized loss can be smaller if not all entries fill before SL.
    """
    entries = compute_entries(signal, config)
    if not entries:
        return 0.0, 0.0

    first = entries[0]
    raw_distance = first - signal.sl if signal.side == "BUY" else signal.sl - first
    base_stop_distance = raw_distance * config.sl_multiplier

    risk_amount = equity * config.risk_per_signal
    total_price_risk = sum(
        abs(e - initial_stop_for_entry(signal.side, e, base_stop_distance))
        for e in entries
    )
    if total_price_risk <= 0:
        return 0.0, base_stop_distance
    lot = risk_amount / (total_price_risk * contract_size)

    # Always round to a step. Config can choose a larger step (e.g. 0.1) but
    # cannot disable rounding -- 0.01 is the floor everywhere.
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
# bar-by-bar advancement (the validated logic)
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
                    # Bar visited safe side; OHLC alone can't tell us in what
                    # order. Arm now and require an actual touch on a later
                    # bar. This avoids stale/marketable fills.
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

    # 3. Stop / target evaluation. Worst-case: stop wins same bar.
    open_entries = position.open_entries()
    if open_entries:
        tp1_hit = target_trigger(side, h, l, position.signal.tp1, sp)
        target_hit = target_trigger(side, h, l, position.target_level, sp)

        for e in list(open_entries):
            stop_level = (
                position.signal.tp1
                if (config.lock_after_tp1 and position.stage >= 1)
                else e.initial_sl
            )
            if stop_trigger(side, h, l, stop_level, sp):
                if (config.lock_after_tp1 and position.stage >= 1
                        and math.isclose(stop_level, position.signal.tp1, abs_tol=1e-9)):
                    status = "LOCK_TP1"
                else:
                    status = "SL"
                _close_entry(e, status, bar.time, stop_level, side, contract_size, stop_level)
            elif target_hit:
                _close_entry(e, config.final_target.upper(), bar.time, position.target_level,
                             side, contract_size)

        # 4. Stage advance: TP1 touched in stage 0 -> stage 1.
        if config.lock_after_tp1 and position.stage == 0 and tp1_hit:
            position.stage = 1

    # 5. Time exit: at first bar at/after the deadline, close at close.
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