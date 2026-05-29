"""Strategy configuration.

Default strategy: bonus-aware provider execution contract.

The defaults in this branch are aligned with the current best 40% max-drawdown
candidate found from the uploaded provider signal sample. Use the filtered
``generated/live_provider_high_growth.txt`` signal file for parity between
backtest and auto execution.

Current default contract:

- initial capital: 1000
- sizing: risk mode
- risk per signal: 0.05575
- entries: 3
- entry ladder: range_to_sl
- entry-to-SL gap: 2.0
- activation delay: 3 minutes
- pending expiry: 630 minutes
- max hold: 90 minutes
- SL multiplier: 1.61
- final target: TP3
- lock after TP1: true
- lock after TP2: false
- TP1 lock delay: 0 minutes
- TP2 lock delay: 0 minutes
- closed-lot bonus/rebate: 3.0 per closed lot

Observed local validation snapshot on uploaded provider signals/charts:

- net profit including bonus: about +$22.609T
- trading P&L: about +$22.436T
- closed-lot bonus: about +$174.0B
- max drawdown: about -39.94%

This is an extremely aggressive research configuration. The very long pending
expiry is the main reason live/backtest parity requires MT5 to keep orders alive
for the full 630-minute window.
"""
from __future__ import annotations
from dataclasses import dataclass


CONTRACT_SIZE_OZ = 100.0       # 1.0 lot XAUUSD = 100 oz; 0.5 lot = $50 per $1 move
POINT_VALUE = 0.01             # 1 spread point = $0.01
CHART_TIMEZONE_OFFSET = 3      # MT5 CSV is GMT+3


@dataclass(frozen=True)
class StrategyConfig:
    initial_capital: float = 1_000.0

    sizing_mode: str = "risk"              # "fixed" | "risk"
    lot_per_entry: float = 0.5
    risk_per_signal: float = 0.05575
    minimum_lot: float = 0.01
    lot_step: float = 0.01

    # Bonus/rebate. Broker bonus is modeled as cash received for every lot that
    # closes. Set to 0.0 to reproduce pure trading P&L.
    bonus_per_closed_lot: float = 3.0

    # Entry plan.
    entry_count: int = 3
    entry_ladder: str = "range_to_sl"      # "signal_range_3" | "range_uniform" | "range_to_sl"
    entry_sl_gap: float = 2.0               # only used when entry_ladder="range_to_sl"

    # Execution timing.
    activation_delay_minutes: int = 3
    pending_expiry_minutes: int = 630
    max_hold_minutes: int = 90

    # Stop/target management.
    sl_multiplier: float = 1.61
    final_target: str = "TP3"
    lock_after_tp1: bool = True
    lock_after_tp2: bool = False

    # Delayed stop-lock timing. 0 keeps the standard behavior: TP1/TP2 lock is
    # applied right after the target-touch candle is processed.
    tp1_lock_delay_minutes: int = 0
    tp2_lock_delay_minutes: int = 0

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
