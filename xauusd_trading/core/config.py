"""Strategy configuration — v2 baseline.

Values are locked to tests/test_smoke.py. Changing them invalidates the
smoke test; re-run the parameter sweep and update the test before
deploying any change.
"""
from __future__ import annotations
from dataclasses import dataclass


CONTRACT_SIZE_OZ = 100.0       # 1.0 lot XAUUSD = 100 oz; 0.5 lot = $50 per $1 move
POINT_VALUE = 0.01             # 1 spread point = $0.01
CHART_TIMEZONE_OFFSET = 3      # MT5 CSV is GMT+3


@dataclass(frozen=True)
class StrategyConfig:
    initial_capital: float = 1000.0
    risk_per_signal: float = 0.05
    entry_count: int = 5
    entry_ladder: str = "range_uniform"
    entry_sl_gap: float = 1.0
    activation_delay_minutes: int = 0
    pending_expiry_minutes: int = 20
    max_hold_minutes: int = 90
    sl_multiplier: float = 1.0
    final_target: str = "TP3"
    lock_after_tp1: bool = True
    minimum_lot: float = 0.01
    lot_step: float = 0.01


DEFAULT_CONFIG = StrategyConfig()