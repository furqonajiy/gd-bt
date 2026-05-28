"""Strategy configuration.

Default strategy: bonus-aware high-growth provider execution contract.

This branch is optimized for provider-style VICTOR XAUUSD signals filtered by
``high_growth_hour_side``. The same defaults are used by backtest, ``decide``,
``manage``, and ``auto`` unless explicitly overridden.

Current best bonus-aware candidate from the uploaded provider signal sample:

- filtered signal file using high_growth_hour_side
- initial capital: 10,000
- sizing: risk mode
- risk per signal: 10%
- 4 range-uniform entries
- 2-minute activation delay
- 5-minute pending TIF
- 90-minute max hold
- SL x1.5
- TP3 final target
- TP1 and TP2 stop locks enabled
- closed-lot bonus/rebate: $3 per closed lot

Observed snapshot from local validation on the uploaded sample:

- net profit including bonus: about +$1.475M
- trading P&L: about +$1.457M
- closed-lot bonus: about +$18.4k
- max drawdown: about -39.75%

This is extremely aggressive and should be paper/forward-tested before live
size. Use LOWER_RISK_PROVIDER_CONFIG for warm-up.
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
    risk_per_signal: float = 0.10
    minimum_lot: float = 0.01
    lot_step: float = 0.01

    # Bonus/rebate. Broker bonus is modeled as cash received for every lot that
    # closes. Set to 0.0 to reproduce pure trading P&L.
    bonus_per_closed_lot: float = 3.0

    # Entry plan.
    entry_count: int = 4
    entry_ladder: str = "range_uniform"    # "signal_range_3" | "range_uniform" | "range_to_sl"
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

    # Profit-lock model:
    # - "tp_levels": old validated rule; after TP1 lock stop at TP1, after TP2
    #   lock stop at TP2.
    # - "bep_plus_half_tp1": research rule; after price moves +N from each
    #   entry, lock that entry at BEP. After TP1, lock remaining entries at
    #   entry + fraction*(TP1-entry). After TP2, lock remaining entries at TP1.
    profit_lock_mode: str = "tp_levels"
    bep_trigger_distance: float = 3.0
    tp1_lock_fraction: float = 0.5
    tp2_lock_target: str = "TP1"            # "TP1" | "TP2"


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
    entry_ladder="signal_range_3",
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
