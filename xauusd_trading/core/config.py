"""Strategy configuration.

Default strategy: bonus-aware provider execution contract.

The defaults in this branch remain the validated DD40-compatible provider
contract. Optional trailing-open / trailing-close distances are available for
research and live execution, but default to 0.0 so existing backtests and Auto
runs keep their current behaviour unless explicitly enabled.
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

    # Optional trailing behaviour. 0.0 disables each feature.
    # trailing_open_distance:
    #   Use virtual entries until the whole ladder is passed and price rebounds
    #   by this distance. Example BUY LIMIT 4750 with distance=2.0 will not open
    #   while Ask is dumping to 4740; it opens only after the observed low Ask
    #   rebounds by 2.0.
    # trailing_close_distance:
    #   Trail the protective stop by this distance after an entry is open.
    trailing_open_distance: float = 0.0
    trailing_close_distance: float = 0.0

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
