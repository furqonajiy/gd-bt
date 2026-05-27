"""Strategy configuration.

Default strategy: Balanced Live Candidate.

The default mirrors the optimized live execution candidate used by this
project: 3 range entries, 2-minute activation delay, 5-minute pending TIF,
90-minute max hold, SL x1.5, TP3 runner, TP1 and TP2 stop locks, and fixed
0.5 lot per entry.
"""
from __future__ import annotations
from dataclasses import dataclass, replace


CONTRACT_SIZE_OZ = 100.0       # 1.0 lot XAUUSD = 100 oz; 0.5 lot = $50 per $1 move
POINT_VALUE = 0.01             # 1 spread point = $0.01
CHART_TIMEZONE_OFFSET = 3      # MT5 CSV is GMT+3


@dataclass(frozen=True)
class StrategyConfig:
    initial_capital: float = 1000.0

    # Sizing. Default is fixed-lot because strategy comparison should not be
    # distorted by compounding or changing SL distance. Set sizing_mode="risk"
    # to use risk_per_signal instead.
    sizing_mode: str = "fixed"             # "fixed" | "risk"
    lot_per_entry: float = 0.5
    risk_per_signal: float = 0.05
    minimum_lot: float = 0.01
    lot_step: float = 0.01

    # Entry plan.
    entry_count: int = 3
    entry_ladder: str = "signal_range_3"   # "signal_range_3" | "range_uniform" | "range_to_sl"
    entry_sl_gap: float = 2.0               # only used when entry_ladder="range_to_sl"

    # Execution timing.
    activation_delay_minutes: int = 2
    pending_expiry_minutes: int = 5
    max_hold_minutes: int = 90

    # Stop/target management.
    sl_multiplier: float = 1.5
    final_target: str = "TP3"
    lock_after_tp1: bool = True
    lock_after_tp2: bool = True


DEFAULT_CONFIG = StrategyConfig()
BALANCED_LIVE_CONFIG = DEFAULT_CONFIG

# Reference variants that should always be compared in sweeps.
HIGHEST_PROFIT_CONFIG = replace(
    DEFAULT_CONFIG,
    entry_count=3,
    activation_delay_minutes=0,
    pending_expiry_minutes=20,
    max_hold_minutes=30,
    sl_multiplier=1.5,
    final_target="TP3",
    lock_after_tp1=True,
    lock_after_tp2=True,
)

LOWER_EXPOSURE_CONFIG = replace(
    HIGHEST_PROFIT_CONFIG,
    entry_count=2,
)
