"""EMA/ATR trend-runner helpers for TP3 winners.

Disabled unless StrategyConfig.trend_runner_enabled is true.  Indicator state is
pre-warmed from a fixed pre-activation lookback so backtest replay and live
tracked replay reach the same EMA/ATR values even when live execution starts
after activation.
"""
from __future__ import annotations

from typing import Iterable

from .chart import Bar, iter_bars
from .positions import Entry, Position
from .config import StrategyConfig


def _ema(prev: float | None, value: float, period: int) -> float:
    if prev is None:
        return float(value)
    alpha = 2.0 / (max(1, int(period)) + 1.0)
    return prev + alpha * (float(value) - prev)


def trend_runner_enabled(config: StrategyConfig) -> bool:
    return bool(getattr(config, "trend_runner_enabled", False))


def warmup_bar_count(config: StrategyConfig) -> int:
    slow = max(1, int(getattr(config, "trend_runner_ema_slow", 55) or 55))
    atr = max(1, int(getattr(config, "trend_runner_atr_period", 14) or 14))
    return max(5 * slow, atr)


def reset_indicators(position: Position) -> None:
    for attr in (
        "trend_prev_close",
        "trend_ema_fast",
        "trend_ema_slow",
        "trend_atr",
        "trend_warmup_bar_count",
        "trend_warmup_until",
    ):
        if hasattr(position, attr):
            delattr(position, attr)


def update_indicators(position: Position, bar: Bar, config: StrategyConfig) -> None:
    if not trend_runner_enabled(config):
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


def prewarm_indicators(position: Position, bars: Iterable[Bar], config: StrategyConfig) -> None:
    """Feed closed bars into EMA/ATR only; never mutate fills/exits/stage."""
    if not trend_runner_enabled(config):
        return
    count = 0
    last_time = None
    for bar in bars:
        update_indicators(position, bar, config)
        count += 1
        last_time = bar.time
    position.trend_warmup_bar_count = count
    position.trend_warmup_until = last_time


def prewarm_indicators_from_dataframe(
        position: Position,
        chart_df,
        config: StrategyConfig,
        *,
        replay_start=None,
) -> None:
    """Warm up from exact chart rows before a lifecycle replay.

    The fixed anchor is activation_time.  We always feed the last N closed bars
    before activation_time, where N=max(5*slow_ema, atr_period).  If the actual
    lifecycle starts after activation, we then feed activation..replay_start-1 as
    indicator-only bridge bars so EMA/ATR at replay_start is identical to an
    activation-start replay.
    """
    if not trend_runner_enabled(config):
        return
    replay_start = replay_start or position.activation_time
    reset_indicators(position)
    lookback = warmup_bar_count(config)
    before_activation = chart_df[chart_df["time"] < position.activation_time].tail(lookback)
    prewarm_indicators(position, iter_bars(before_activation), config)
    if replay_start > position.activation_time:
        bridge = chart_df[
            (chart_df["time"] >= position.activation_time)
            & (chart_df["time"] < replay_start)
        ]
        prewarm_indicators(position, iter_bars(bridge), config)


def trend_agrees(position: Position, config: StrategyConfig) -> bool:
    fast = getattr(position, "trend_ema_fast", None)
    slow = getattr(position, "trend_ema_slow", None)
    if fast is None or slow is None:
        return False
    return fast > slow if position.signal.side == "BUY" else fast < slow


def runner_can_hold(position: Position, config: StrategyConfig) -> bool:
    return (
        trend_runner_enabled(config)
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
