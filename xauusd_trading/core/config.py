"""Strategy configuration.

Default strategy: bonus-aware provider execution contract.

The defaults in this branch are aligned with the current best 50% max-drawdown
research candidate from the uploaded provider signal sample. Use the filtered
``generated/live_provider_high_growth.txt`` signal file for parity between
backtest and auto execution.

Current default contract:

- sizing: risk mode
- risk per signal: 0.14222
- entries: 3
- entry ladder: signal_range_3
- activation delay: 0 minutes
- pending expiry: 45 minutes
- max hold: 280 minutes
- SL multiplier: 2.5
- final target: TP3
- TP1 lock delay: 8 minutes
- TP2 lock delay: 4 minutes
- closed-lot bonus/rebate: 3.0 per closed lot

This is a research configuration and should be forward-tested before real size.
"""
from __future__ import annotations
from dataclasses import dataclass, replace


CONTRACT_SIZE_OZ = 100.0       # 1.0 lot XAUUSD = 100 oz; 0.5 lot = $50 per $1 move
POINT_VALUE = 0.01             # 1 spread point = $0.01
CHART_TIMEZONE_OFFSET = 3      # MT5 CSV is GMT+3


@dataclass(frozen=True)
class StrategyConfig:
    initial_capital: float = 10_000.0

    sizing_mode: str = "risk"              # "fixed" | "risk"
    lot_per_entry: float = 0.5
    risk_per_signal: float = 0.14222
    minimum_lot: float = 0.01
    lot_step: float = 0.01

    # Bonus/rebate. Broker bonus is modeled as cash received for every lot that
    # closes. Set to 0.0 to reproduce pure trading P&L.
    bonus_per_closed_lot: float = 3.0

    # Entry plan.
    entry_count: int = 3
    entry_ladder: str = "signal_range_3"   # "signal_range_3" | "range_uniform" | "range_to_sl"
    entry_sl_gap: float = 2.0               # only used when entry_ladder="range_to_sl"

    # Execution timing.
    activation_delay_minutes: int = 0
    pending_expiry_minutes: int = 45
    max_hold_minutes: int = 280

    # Stop/target management.
    sl_multiplier: float = 2.5
    final_target: str = "TP3"
    lock_after_tp1: bool = True
    lock_after_tp2: bool = True

    # Delayed stop-lock timing. 0 keeps the old behavior: TP1/TP2 lock is
    # applied right after the target-touch candle is processed. Positive values
    # wait N full minutes after first touch before raising the stop.
    tp1_lock_delay_minutes: int = 8
    tp2_lock_delay_minutes: int = 4

    # Profit-lock model:
    # - "tp_levels": after TP1/TP2 lock stops to the configured TP levels.
    # - "bep_plus_half_tp1": research rule for BEP and partial TP1 locking.
    profit_lock_mode: str = "tp_levels"
    bep_trigger_distance: float = 3.0
    tp1_lock_fraction: float = 0.5
    tp2_lock_target: str = "TP1"            # "TP1" | "TP2"
    runner_after_tp3: bool = False
    tp3_lock_target: str = "TP2"


DEFAULT_CONFIG = StrategyConfig()
BEST_PNL_CONFIG = DEFAULT_CONFIG
BALANCED_LIVE_CONFIG = DEFAULT_CONFIG

LOWER_RISK_PROVIDER_CONFIG = replace(
    DEFAULT_CONFIG,
    risk_per_signal=0.02,
)

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
