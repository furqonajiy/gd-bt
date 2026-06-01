"""EMA/ATR trend-runner helpers for TP3 winners.

Disabled unless StrategyConfig.trend_runner_enabled is true.
"""
from __future__ import annotations

from .chart import Bar
from .positions import Entry, Position
from .config import StrategyConfig


def _ema(prev: float | None, value: float, period: int) -> float:
    if prev is None:
        return float(value)
    alpha = 2.0 / (max(1, int(period)) + 1.0)
    return prev + alpha * (float(value) - prev)


def update_indicators(position: Position, bar: Bar, config: StrategyConfig) -> None:
    if not bool(getattr(config, "trend_runner_enabled", False)):
        return
    prev_close = getattr(position, "trend_prev_close", None)
    true_range = bar.high - bar.low
    if prev_close is not None:
        pc = float(prev_close)
        true_range = max(true_range, abs(bar.high - pc), abs(bar.low - pc))
    position.trend_ema_fast = _ema(
        getattr(position, "trend_ema_fast", None),
        bar.close,
        getattr(config, "trend_runner_ema_fast", 21),
    )
    position.trend_ema_slow = _ema(
        getattr(position, "trend_ema_slow", None),
        bar.close,
        getattr(config, "trend_runner_ema_slow", 55),
    )
    position.trend_atr = _ema(
        getattr(position, "trend_atr", None),
        true_range,
        getattr(config, "trend_runner_atr_period", 14),
    )
    position.trend_prev_close = bar.close


def trend_agrees(position: Position, config: StrategyConfig) -> bool:
    fast = getattr(position, "trend_ema_fast", None)
    slow = getattr(position, "trend_ema_slow", None)
    if fast is None or slow is None:
        return False
    return fast > slow if position.signal.side == "BUY" else fast < slow


def runner_can_hold(position: Position, config: StrategyConfig) -> bool:
    return (
        bool(getattr(config, "trend_runner_enabled", False))
        and config.final_target.upper() == "TP3"
        and trend_agrees(position, config)
    )


def update_runner_stop(position: Position, bar: Bar, config: StrategyConfig) -> None:
    atr = getattr(position, "trend_atr", None)
    if atr is None or atr <= 0:
        return
    distance = float(atr) * float(getattr(config, "trend_runner_atr_multiplier", 3.0) or 3.0)
    if position.signal.side == "BUY":
        candidate = max(bar.high - distance, position.signal.tp2)
    else:
        candidate = min(bar.low + distance, position.signal.tp2)
    for entry in position.open_entries():
        if entry.fill_time is None or entry.fill_time >= bar.time:
            continue
        current = getattr(entry, "trailing_stop", None)
        if position.signal.side == "BUY":
            entry.trailing_stop = candidate if current is None else max(float(current), candidate)
        else:
            entry.trailing_stop = candidate if current is None else min(float(current), candidate)
    position.stage = max(position.stage, 3)
    if position.stage3_time is None:
        position.stage3_time = bar.time
    position.trend_runner_active = True


def should_skip_time_exit(position: Position, config: StrategyConfig) -> bool:
    return bool(
        getattr(config, "trend_runner_override_max_hold", True)
        and getattr(position, "trend_runner_active", False)
    )


def stop_status_for(entry: Entry, stop_level: float, fallback: str) -> str:
    trailing_stop = getattr(entry, "trailing_stop", None)
    if trailing_stop is not None and abs(stop_level - float(trailing_stop)) < 1e-9:
        return "TRAILING_STOP"
    return fallback
