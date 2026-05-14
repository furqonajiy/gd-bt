"""Spread-aware trigger predicates.

The chart is Bid; Ask = Bid + spread. A BUY exits or fills based on the Ask
side, a SELL on the Bid side. These predicates encode that asymmetry against
a bar's High/Low (which are Bid).

Exit prices for P&L are the level itself; spread is baked into the trigger,
not into the exit price.
"""
from __future__ import annotations


def fill_trigger(side: str, high: float, low: float, entry: float, spread_price: float) -> bool:
    """Limit-order fill condition for one bar.

    BUY  LIMIT fills when bar's Low (Bid) <= entry - spread (Ask reaches entry).
    SELL LIMIT fills when bar's High (Bid) >= entry.
    """
    if side == "BUY":
        return low <= entry - spread_price
    return high >= entry


def target_trigger(side: str, high: float, low: float, level: float, spread_price: float) -> bool:
    """Take-profit (TP) trigger.

    BUY  TP triggers when High (Bid) >= TP.
    SELL TP triggers when Low (Bid) <= TP - spread (Ask reaches TP).
    """
    if side == "BUY":
        return high >= level
    return low <= level - spread_price


def stop_trigger(side: str, high: float, low: float, level: float, spread_price: float) -> bool:
    """Stop-loss / stop-profit trigger.

    BUY  SL triggers when Low (Bid) <= SL.
    SELL SL triggers when High (Bid) >= SL - spread (Ask reaches SL).
    """
    if side == "BUY":
        return low <= level
    return high >= level - spread_price


def initial_stop_for_entry(side: str, entry: float, base_stop_distance: float) -> float:
    """Initial SL for one entry given the SL-multiplier-adjusted distance."""
    return entry - base_stop_distance if side == "BUY" else entry + base_stop_distance
