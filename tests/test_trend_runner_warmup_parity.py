from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta

import pandas as pd

from xauusd_trading import Bar, DEFAULT_CONFIG, advance_bars, open_position, parse_one_signal
from xauusd_trading.core.chart import iter_bars, slice_bars
from xauusd_trading.core.trend_runner import prewarm_indicators_from_dataframe


def _bar(t: datetime, o: float, h: float, l: float, c: float, spread: float = 0.0) -> Bar:
    return Bar(t, o, h, l, c, int(round(spread / 0.01)), spread)


def _df(bars: list[Bar]) -> pd.DataFrame:
    return pd.DataFrame([
        {
            "time": b.time,
            "open": b.open,
            "high": b.high,
            "low": b.low,
            "close": b.close,
            "spread": b.spread_points,
            "spread_price": b.spread_price,
        }
        for b in bars
    ])


def _advance_from(signal, chart_df, cfg, replay_start: datetime):
    pos = open_position(signal, 1000.0, cfg)
    prewarm_indicators_from_dataframe(pos, chart_df, cfg, replay_start=replay_start)
    advance_bars(
        pos,
        iter_bars(slice_bars(chart_df, replay_start, chart_df["time"].iloc[-1])),
        cfg,
    )
    return pos


def test_trend_runner_warmup_same_result_from_activation_or_later_start():
    signal = parse_one_signal(
        "1. SELL XAUUSD 4500 - 4502 SL 4508 TP1 4490 TP2 4480 TP3 4470 10:00 PM",
        source_date="2026-05-29",
        source_offset=0,
    )
    cfg = replace(
        DEFAULT_CONFIG,
        activation_delay_minutes=0,
        entry_count=1,
        trend_runner_enabled=True,
        trend_runner_ema_fast=2,
        trend_runner_ema_slow=4,
        trend_runner_atr_period=2,
        trend_runner_atr_multiplier=1.0,
        trend_runner_override_max_hold=True,
        max_hold_minutes=3,
    )
    activation = signal.signal_time_chart
    delayed = activation + timedelta(minutes=30)

    bars: list[Bar] = []
    # Fixed pre-activation downtrend warmup. With slow=4, the helper requires
    # 20 closed bars; provide more so both replay starts use the same anchor.
    for i in range(30, 0, -1):
        t = activation - timedelta(minutes=i)
        price = 4520 - (30 - i) * 0.5
        bars.append(_bar(t, price + 0.2, price + 0.4, price - 0.4, price))

    # Activation..delayed-1: no SELL entry fill because high remains below 4500.
    for i in range(30):
        t = activation + timedelta(minutes=i)
        bars.append(_bar(t, 4496.5, 4499.0, 4495.0, 4496.0))

    # Both replays start trading here: fill, hit TP3, hold runner, then trail out.
    bars.extend([
        _bar(delayed, 4499.0, 4500.0, 4498.0, 4498.0),
        _bar(delayed + timedelta(minutes=1), 4498.0, 4498.0, 4470.0, 4470.0),
        _bar(delayed + timedelta(minutes=2), 4470.0, 4470.0, 4450.0, 4452.0),
        _bar(delayed + timedelta(minutes=3), 4452.0, 4470.0, 4451.0, 4464.0),
    ])
    chart_df = _df(bars)

    from_activation = _advance_from(signal, chart_df, cfg, activation)
    from_delayed = _advance_from(signal, chart_df, cfg, delayed)

    a = from_activation.entries[0]
    b = from_delayed.entries[0]

    assert getattr(from_activation, "trend_runner_active", False) is True
    assert getattr(from_delayed, "trend_runner_active", False) is True
    assert a.status == b.status == "TRAILING_STOP"
    assert a.fill_time == b.fill_time == delayed
    assert a.exit_time == b.exit_time
    assert a.exit_price == b.exit_price
    assert a.stop_at_exit == b.stop_at_exit
    assert getattr(a, "trailing_stop", None) == getattr(b, "trailing_stop", None)