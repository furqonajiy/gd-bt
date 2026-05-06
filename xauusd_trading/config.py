"""Strategy configuration. Single source of truth for the parameters that
produced the validated backtest result (61.22% win rate, ~8.7x return).

These values define the strategy. Touching them changes behavior and
invalidates the backtest reference. Don't change without re-validating.
"""
from __future__ import annotations
from dataclasses import dataclass


# Constants of the instrument and the chart format. Not strategy choices.
CONTRACT_SIZE_OZ = 100.0       # 1.0 lot XAUUSD = 100 oz; 0.5 lot = $50 per $1 move
POINT_VALUE = 0.01             # 1 spread point = $0.01
CHART_TIMEZONE_OFFSET = 3      # MT5 CSV is GMT+3


@dataclass(frozen=True)
class StrategyConfig:
    """Validated parameters of the optimized strategy."""
    initial_capital: float = 1000.0
    risk_per_signal: float = 0.05
    entry_count: int = 3
    activation_delay_minutes: int = 0
    pending_expiry_minutes: int = 20
    max_hold_minutes: int = 90
    sl_multiplier: float = 1.25      # effective SL = 1.25 x raw distance
    final_target: str = "TP2"        # close all at TP2; TP1 only triggers lock
    lock_after_tp1: bool = True      # after TP1 touched, remaining stops -> TP1
    minimum_lot: float = 0.0         # 0 disables broker rounding
    lot_step: float = 0.0


DEFAULT_CONFIG = StrategyConfig()
