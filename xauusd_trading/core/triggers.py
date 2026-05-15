"""Spread-aware trigger predicates.

Chart is Bid; Ask = Bid + spread. BUY uses the Ask side, SELL uses the
Bid side. Predicates encode that asymmetry against a bar's High/Low
(which are Bid). Exit prices for P&L are the level itself — spread is
baked into the trigger, not the exit price.
"""
from __future__ import annotations


def fill_trigger(side: str, high: float, low: float, entry: float, spread_price: float) -> bool:
    """Limit-order fill condition for one bar."""
    if side == "BUY":
        return low <= entry - spread_price
    return high >= entry


def target_trigger(side: str, high: float, low: float, level: float, spread_price: float) -> bool:
    """Take-profit trigger."""
    if side == "BUY":
        return high >= level
    return low <= level - spread_price


def stop_trigger(side: str, high: float, low: float, level: float, spread_price: float) -> bool:
    """Stop-loss / stop-profit trigger."""
    if side == "BUY":
        return low <= level
    return high >= level - spread_price


def initial_stop_for_entry(side: str, entry: float, base_stop_distance: float) -> float:
    return entry - base_stop_distance if side == "BUY" else entry + base_stop_distance