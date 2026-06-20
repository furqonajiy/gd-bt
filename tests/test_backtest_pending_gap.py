"""Backtest pending-expiry behavior across market-data gaps.

When a signal's pending expiry falls inside a gap with no M1 candles, such as a
Friday close -> Monday open gap, backtest replay still needs to terminalize
unfilled entries as NO_FILL once replay time has reached the expiry.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd

from trading.xauusd import DEFAULT_CONFIG, parse_one_signal
from trading.xauusd.strategy.backtest import position_status, replay_signal


def _bar(t: datetime, *, open_: float, high: float, low: float, close: float):
    return {
        "time": t,
        "open": open_,
        "high": high,
        "low": low,
        "close": close,
        "spread": 20,
        "spread_price": 0.20,
    }


def test_pending_entries_expire_even_when_no_bar_exists_after_expiry():
    signal = parse_one_signal(
        "4. SELL XAUUSD 4408 - 4410 SL 4415.5 TP1 4400 TP2 4390 TP3 4370 03:46 PM",
        source_date="2026-05-29",
        source_offset=3,
    )
    activation = signal.signal_time_chart + timedelta(minutes=DEFAULT_CONFIG.activation_delay_minutes)
    replay_end = activation + timedelta(
        minutes=DEFAULT_CONFIG.pending_expiry_minutes + DEFAULT_CONFIG.max_hold_minutes + 5
    )

    # Simulate a Friday/session gap: there are candles before expiry and chart
    # data after the logical replay end, but no candle with timestamp > expiry
    # inside the signal's replay window. Price never touches the SELL entries.
    chart_df = pd.DataFrame([
        _bar(activation, open_=4500, high=4501, low=4499, close=4500),
        _bar(signal.signal_time_chart.replace(hour=23, minute=59), open_=4510, high=4512, low=4508, close=4510),
        _bar(replay_end + timedelta(days=2), open_=4515, high=4516, low=4514, close=4515),
    ])

    pos = replay_signal(signal, chart_df, equity=1000.0, config=DEFAULT_CONFIG)
    status, pnl = position_status(pos)

    assert status == "NO_FILL"
    assert pnl == 0.0
    assert [e.status for e in pos.entries] == ["NO_FILL", "NO_FILL", "NO_FILL"]
