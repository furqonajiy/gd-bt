"""Strategy configuration. Single source of truth for the parameters that
produced the validated tuned-strategy result (v2 baseline).

These values define the strategy. Touching them changes behavior and
invalidates the smoke test. Don't change without re-running the parameter
sweep and updating tests/test_smoke.py.

History
-------
v1 (2026-05): original tuned baseline, $1,000 -> $8,748.88 on validation CSVs
  (range_uniform entries, 20-min pending expiry, 1.25x SL multiplier).

v2 (2026-05): re-tuned via 3-stage parameter sweep over ~7,900 configs on
  full broker data (Jan-May 2026):
  - range_to_sl ladder with $2 gap to SL (deepest entry is $2 above signal SL)
  - 240-min pending expiry (4-hour fill window)
  - 1.0x SL multiplier (tighter individual stops)
  - 5% risk, 3 entries, 90-min max hold, TP2 final target, lock to TP1 unchanged
  Backtest performance on broker data:
    full $1.13M, IS $13,854, OOS $66,797, max DD -42.8%, win rate 55.9%
  Forward expectation: 2-10x per month is the realistic range. The headline
  numbers reflect the favorable Apr-May 2026 regime.
"""
from __future__ import annotations
from dataclasses import dataclass


# Constants of the instrument and the chart format. Not strategy choices.
CONTRACT_SIZE_OZ = 100.0       # 1.0 lot XAUUSD = 100 oz; 0.5 lot = $50 per $1 move
POINT_VALUE = 0.01             # 1 spread point = $0.01
CHART_TIMEZONE_OFFSET = 3      # MT5 CSV is GMT+3


@dataclass(frozen=True)
class StrategyConfig:
    """Tuned strategy parameters (v2 baseline)."""
    initial_capital: float = 1000.0
    risk_per_signal: float = 0.05
    entry_count: int = 3
    entry_ladder: str = "range_to_sl"        # "range_uniform" | "range_to_sl"
    entry_sl_gap: float = 2.0                # only used when entry_ladder="range_to_sl"
    activation_delay_minutes: int = 0
    pending_expiry_minutes: int = 240
    max_hold_minutes: int = 90
    sl_multiplier: float = 1.0
    final_target: str = "TP2"
    lock_after_tp1: bool = True
    minimum_lot: float = 0.01
    lot_step: float = 0.01


DEFAULT_CONFIG = StrategyConfig()
