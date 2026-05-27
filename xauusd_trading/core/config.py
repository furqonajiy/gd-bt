"""Strategy configuration.

Default strategy: high-growth provider execution contract.

This branch is currently optimized for provider-style VICTOR XAUUSD signals
filtered by ``high_growth_hour_side``.  The same defaults are used by backtest,
``decide``, ``manage``, and ``auto`` unless explicitly overridden.

Validated high-growth contract from the uploaded provider signal sample:

- filtered signal file using high_growth_hour_side
- initial capital: 10,000
- sizing: risk mode
- risk per signal: 12%
- 3 signal-range entries
- 2-minute activation delay
- 5-minute pending TIF
- 90-minute max hold
- SL x1.5
- TP3 final target
- TP1 and TP2 stop locks enabled

Observed snapshot from backtest on the uploaded sample:

- max drawdown stayed just below 50%
- average monthly return was above 20%
- this is aggressive and should be forward-tested before live size
"""
from __future__ import annotations
from dataclasses import dataclass, replace


CONTRACT_SIZE_OZ = 100.0       # 1.0 lot XAUUSD = 100 oz; 0.5 lot = $50 per $1 move
POINT_VALUE = 0.01             # 1 spread point = $0.01
CHART_TIMEZONE_OFFSET = 3      # MT5 CSV is GMT+3


@dataclass(frozen=True)
class StrategyConfig:
    initial_capital: float = 10_000.0

    # High-growth provider contract uses risk sizing. This makes live auto scale
    # lots from current MT5 equity and match the risk-mode backtest behavior.
    sizing_mode: str = "risk"              # "fixed" | "risk"
    lot_per_entry: float = 0.5
    risk_per_signal: float = 0.12
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
BEST_PNL_CONFIG = DEFAULT_CONFIG
BALANCED_LIVE_CONFIG = DEFAULT_CONFIG

# Safer reference for paper/live warm-up.
LOWER_RISK_PROVIDER_CONFIG = replace(
    DEFAULT_CONFIG,
    risk_per_signal=0.02,
)

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
