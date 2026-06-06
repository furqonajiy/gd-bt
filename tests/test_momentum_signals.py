"""Tests for xauusd_trading.strategy.momentum_signals.generate_momentum_signals."""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime

from xauusd_trading import Bar, MomentumSignalConfig, generate_momentum_signals


def _bar(t: str, open_: float, high: float, low: float, close: float, spread: int = 25) -> Bar:
    return Bar(
        time=datetime.strptime(t, "%Y-%m-%d %H:%M:%S"),
        open=open_, high=high, low=low, close=close,
        spread_points=spread, spread_price=spread * 0.01,
    )


_CFG = MomentumSignalConfig(
    lookback_bars=3, min_body=1.0, min_bar_range=1.0, close_position=0.6,
    breakout_buffer=0.0, cooldown_minutes=0, same_zone_cooldown_minutes=0,
    zone_size=50.0, session_start_hour=None, session_end_hour=None,
    entry_range_width=2.0, sl_distance=5.0, tp1_distance=10.0, tp2_distance=20.0, tp3_distance=40.0,
)


def _base():
    # Three calm bars: recent high 101, recent low 99.
    return [
        _bar("2026-06-03 09:00:00", 100.0, 101.0, 99.0, 100.1),
        _bar("2026-06-03 09:01:00", 100.1, 101.0, 99.0, 100.0),
        _bar("2026-06-03 09:02:00", 100.0, 101.0, 99.0, 100.2),
    ]


def test_buy_on_strong_close_above_recent_high():
    bars = _base() + [_bar("2026-06-03 09:03:00", 101.5, 104.0, 101.2, 103.8)]
    signals = generate_momentum_signals(bars, _CFG)
    assert len(signals) == 1
    assert signals[0].side == "BUY"
    # closed-candle: signal fires the minute after the breakout bar
    assert signals[0].source_bar_time == datetime(2026, 6, 3, 9, 3)
    assert signals[0].signal_time_chart == datetime(2026, 6, 3, 9, 4)


def test_sell_on_strong_close_below_recent_low():
    bars = _base() + [_bar("2026-06-03 09:03:00", 98.5, 98.8, 96.0, 96.2)]
    signals = generate_momentum_signals(bars, _CFG)
    assert len(signals) == 1
    assert signals[0].side == "SELL"


def test_no_signal_without_a_breakout():
    # Final bar stays inside the prior range -> no breakout.
    bars = _base() + [_bar("2026-06-03 09:03:00", 100.0, 100.9, 99.1, 100.3)]
    assert generate_momentum_signals(bars, _CFG) == []


def test_no_signal_when_close_is_wick_heavy():
    # Pokes above recent high (104) but closes back near its low -> fails close_position.
    bars = _base() + [_bar("2026-06-03 09:03:00", 101.2, 104.0, 101.0, 101.4)]
    assert generate_momentum_signals(bars, _CFG) == []


def test_bar_minutes_fires_signal_at_bar_close():
    # M15: the close is 15 minutes after the bar's open timestamp -> no look-ahead.
    cfg = replace(_CFG, bar_minutes=15)
    bars = _base() + [_bar("2026-06-03 09:03:00", 101.5, 104.0, 101.2, 103.8)]
    signals = generate_momentum_signals(bars, cfg)
    assert len(signals) == 1
    assert signals[0].source_bar_time == datetime(2026, 6, 3, 9, 3)
    assert signals[0].signal_time_chart == datetime(2026, 6, 3, 9, 18)