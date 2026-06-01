"""Strategy configuration.

Default strategy: bonus-aware provider execution contract.

The defaults in this branch remain the validated DD40-compatible provider
contract. Optional trailing-open, trailing-close, and trend-runner settings are
available for research/live parity, but default to disabled so existing backtests
and Auto runs keep their current behaviour unless explicitly enabled.

Environment variables supported by ``python -m xauusd_trading.cli auto``:

- ``XAUUSD_TRAILING_OPEN_DISTANCE``: virtual entry trail distance.
- ``XAUUSD_TRAILING_CLOSE_DISTANCE``: protective trailing-stop distance.
- ``XAUUSD_TREND_RUNNER_ENABLED``: hold TP3 winners while EMA trend agrees.
- ``XAUUSD_TREND_RUNNER_ATR_MULTIPLIER``: ATR trailing stop distance multiplier.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field


CONTRACT_SIZE_OZ = 100.0       # 1.0 lot XAUUSD = 100 oz; 0.5 lot = $50 per $1 move
POINT_VALUE = 0.01             # 1 spread point = $0.01
CHART_TIMEZONE_OFFSET = 3      # MT5 CSV is GMT+3


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return float(default)
    try:
        return float(raw)
    except ValueError:
        return float(default)


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return int(default)
    try:
        return int(raw)
    except ValueError:
        return int(default)


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return bool(default)
    return str(raw).strip().lower() in {"1", "true", "yes", "y", "on"}


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
    trailing_open_distance: float = field(
        default_factory=lambda: _env_float("XAUUSD_TRAILING_OPEN_DISTANCE", 0.0)
    )
    trailing_close_distance: float = field(
        default_factory=lambda: _env_float("XAUUSD_TRAILING_CLOSE_DISTANCE", 0.0)
    )

    # Optional trend-following runner. When enabled and a TP3 trade is already
    # profitable, the strategy can keep it open while EMA trend agrees and protect
    # it with an ATR trailing stop. Disabled by default until accepted by tests.
    trend_runner_enabled: bool = field(
        default_factory=lambda: _env_bool("XAUUSD_TREND_RUNNER_ENABLED", False)
    )
    trend_runner_ema_fast: int = field(
        default_factory=lambda: _env_int("XAUUSD_TREND_RUNNER_EMA_FAST", 21)
    )
    trend_runner_ema_slow: int = field(
        default_factory=lambda: _env_int("XAUUSD_TREND_RUNNER_EMA_SLOW", 55)
    )
    trend_runner_atr_period: int = field(
        default_factory=lambda: _env_int("XAUUSD_TREND_RUNNER_ATR_PERIOD", 14)
    )
    trend_runner_atr_multiplier: float = field(
        default_factory=lambda: _env_float("XAUUSD_TREND_RUNNER_ATR_MULTIPLIER", 3.0)
    )
    trend_runner_override_max_hold: bool = field(
        default_factory=lambda: _env_bool("XAUUSD_TREND_RUNNER_OVERRIDE_MAX_HOLD", True)
    )

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
