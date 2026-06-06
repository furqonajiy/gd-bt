"""Breakout-continuation (momentum) signal generation.

The mirror image of self_signals: instead of fading a wick at a recent extreme,
this trades WITH a bar that closes beyond a recent extreme. The BTC edge gate
showed rejection was anti-predictive (MFE/|MAE| < 1 at every horizon) -- price
tended to continue past the level rather than revert -- which is the empirical
case for testing momentum next.

No look-ahead: a signal fires at the close of the breakout bar (signal_time =
bar.time + 1 minute), and the geometry is built by the same _make_signal used for
rejections, so a momentum signal is the same GeneratedSignal a backtest/live
runner already knows how to consume.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Iterable

from xauusd_trading import Bar

# Reuse the rejection module's geometry builder and gates so entry/SL/TP
# construction and the session/zone logic stay identical across both signals.
from .self_signals import GeneratedSignal, _in_session, _make_signal, _zone_key


@dataclass(frozen=True)
class MomentumSignalConfig:
    lookback_bars: int = 20
    bar_minutes: int = 1            # bar duration; signal fires at bar close (= open + bar_minutes), so M15 must set 15
    min_body: float = 50.0          # directional conviction: large real body
    min_bar_range: float = 80.0     # range expansion: a real impulse, not drift
    close_position: float = 0.6     # close in the top (BUY) / bottom (SELL) fraction of its range
    breakout_buffer: float = 0.0    # how far beyond the recent extreme the close must sit
    cooldown_minutes: int = 20
    same_zone_cooldown_minutes: int = 120
    zone_size: float = 50.0
    max_spread_points: int | None = 5000
    session_start_hour: int | None = None
    session_end_hour: int | None = None
    entry_range_width: float = 40.0
    sl_distance: float = 120.0
    tp1_distance: float = 120.0
    tp2_distance: float = 240.0
    tp3_distance: float = 480.0
    price_digits: int = 2


def _validate_config(config: MomentumSignalConfig) -> None:
    if config.lookback_bars < 1:
        raise ValueError("lookback_bars must be >= 1")
    if config.bar_minutes < 1:
        raise ValueError("bar_minutes must be >= 1")
    if config.entry_range_width <= 0:
        raise ValueError("entry_range_width must be > 0")
    if config.sl_distance <= 0:
        raise ValueError("sl_distance must be > 0")
    if config.tp1_distance <= 0 or config.tp2_distance <= config.tp1_distance or config.tp3_distance <= config.tp2_distance:
        raise ValueError("TP distances must be positive and ordered TP1 < TP2 < TP3")
    if config.zone_size <= 0:
        raise ValueError("zone_size must be > 0")
    if not 0.0 <= config.close_position <= 1.0:
        raise ValueError("close_position must be between 0 and 1")
    for name, hour in (
            ("session_start_hour", config.session_start_hour),
            ("session_end_hour", config.session_end_hour),
    ):
        if hour is not None and not (0 <= hour <= 24):
            raise ValueError(f"{name} must be between 0 and 24, or None")


def generate_momentum_signals(
        bars: Iterable[Bar],
        config: MomentumSignalConfig = MomentumSignalConfig(),
) -> list[GeneratedSignal]:
    """Generate parser-compatible breakout-continuation signals from closed M1 bars.

    BUY: a strong bullish bar closes above the prior lookback window's high.
    SELL: a strong bearish bar closes below the prior window's low.
    """
    _validate_config(config)
    ordered = sorted(list(bars), key=lambda b: b.time)
    if len(ordered) <= config.lookback_bars:
        return []

    out: list[GeneratedSignal] = []
    last_any_signal = None
    last_zone_signal: dict[tuple[str, int], object] = {}

    for i in range(config.lookback_bars, len(ordered)):
        bar = ordered[i]
        # Signal fires at the bar's CLOSE (open + bar_minutes): on M15 the close is
        # 15 min after bar.time, so +1 would read a price the bar hasn't printed yet.
        signal_time = bar.time + timedelta(minutes=config.bar_minutes)

        if not _in_session(signal_time, config.session_start_hour, config.session_end_hour):
            continue
        if config.max_spread_points is not None and bar.spread_points > config.max_spread_points:
            continue

        previous = ordered[i - config.lookback_bars:i]
        recent_high = max(b.high for b in previous)
        recent_low = min(b.low for b in previous)

        bar_range = bar.high - bar.low
        if bar_range < config.min_bar_range:
            continue
        body = abs(bar.close - bar.open)
        if body < config.min_body:
            continue

        side: str | None = None
        recent_level = 0.0
        breakout_dist = 0.0
        # BUY: closed above the recent high, bullish, finishing near the high
        # (close_position guards against an upper-wick bar that merely poked through).
        if (
                bar.close > recent_high + config.breakout_buffer
                and bar.close > bar.open
                and (bar.close - bar.low) >= config.close_position * bar_range
        ):
            side, recent_level, breakout_dist = "BUY", recent_high, bar.close - recent_high
        # SELL: closed below the recent low, bearish, finishing near the low
        elif (
                bar.close < recent_low - config.breakout_buffer
                and bar.close < bar.open
                and (bar.high - bar.close) >= config.close_position * bar_range
        ):
            side, recent_level, breakout_dist = "SELL", recent_low, recent_low - bar.close

        if side is None:
            continue

        if last_any_signal is not None:
            if (signal_time - last_any_signal).total_seconds() / 60.0 < config.cooldown_minutes:
                continue

        entry_anchor = bar.close
        zone_key = _zone_key(side, entry_anchor, config.zone_size)
        last_same_zone = last_zone_signal.get(zone_key)
        if last_same_zone is not None:
            if (signal_time - last_same_zone).total_seconds() / 60.0 < config.same_zone_cooldown_minutes:
                continue

        reason = (
            f"close={bar.close:.2f}; broke {'high' if side == 'BUY' else 'low'}="
            f"{recent_level:.2f} by {breakout_dist:.2f}; body={body:.2f}; range={bar_range:.2f}"
        )
        signal = _make_signal(
            side=side,
            entry_anchor=entry_anchor,
            signal_time=signal_time,
            source_bar=bar,
            recent_level=recent_level,
            wick_size=breakout_dist,  # repurposed: distance the close cleared the level
            body_size=body,
            bar_range=bar_range,
            config=config,
            reason=reason,
        )
        out.append(signal)
        last_any_signal = signal_time
        last_zone_signal[zone_key] = signal_time

    return out