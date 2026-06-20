from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

from trading.engine import (
    Bar,
    DEFAULT_CONFIG,
    advance_one_bar,
    open_position,
    parse_one_signal,
)


def _bar(t, open_, high, low, close, spread=0.20):
    return Bar(
        time=t,
        open=open_,
        high=high,
        low=low,
        close=close,
        spread_points=int(round(spread / 0.01)),
        spread_price=spread,
    )


def _fixed_config(**overrides):
    params = dict(
        sizing_mode="fixed",
        lot_per_entry=0.10,
        entry_count=3,
        entry_ladder="range_to_sl",
        entry_sl_gap=2.0,
        activation_delay_minutes=3,
        pending_expiry_minutes=630,
        max_hold_minutes=90,
        final_target="TP3",
        lock_after_tp1=True,
        lock_after_tp2=False,
        tp1_lock_delay_minutes=0,
    )
    params.update(overrides)
    return replace(DEFAULT_CONFIG, **params)


def test_same_bar_fill_and_tp1_touch_does_not_create_retroactive_lock():
    signal = parse_one_signal(
        "2. BUY XAUUSD 4483 - 4481 SL 4476 TP1 4491 TP2 4501 TP3 4521 11:11 AM",
        source_date="2026-06-01",
        source_offset=3,
    )
    config = _fixed_config()
    pos = open_position(signal, equity=1000.0, config=config)

    # The candle touches entry #1 and TP1, but OHLC cannot prove TP1 happened
    # after the fill. The replay must not arm TP1 lock from this candle.
    advance_one_bar(
        pos,
        _bar(pos.activation_time, 4495.0, 4492.0, 4482.70, 4485.0),
        config,
    )
    assert pos.entries[0].status == "OPEN"
    assert pos.stage == 0
    assert pos.stage1_time is None

    # Lower ladder entries fill later. The old model would have inherited the
    # previous candle's TP1 lock and closed all entries at LOCK_TP1 here.
    advance_one_bar(
        pos,
        _bar(pos.activation_time + timedelta(minutes=1), 4485.0, 4488.0, 4477.0, 4480.0),
        config,
    )

    assert [entry.status for entry in pos.entries] == ["OPEN", "OPEN", "OPEN"]
    assert pos.stage == 0
    assert pos.stage1_time is None


def test_tp1_lock_does_not_apply_to_entries_filled_after_tp1_touch():
    signal = parse_one_signal(
        "2. BUY XAUUSD 4483 - 4481 SL 4476 TP1 4491 TP2 4501 TP3 4521 11:11 AM",
        source_date="2026-06-01",
        source_offset=3,
    )
    config = _fixed_config()
    pos = open_position(signal, equity=1000.0, config=config)

    t0 = pos.activation_time
    advance_one_bar(pos, _bar(t0, 4495.0, 4488.0, 4482.70, 4485.0), config)
    assert [entry.status for entry in pos.entries] == ["OPEN", "PENDING", "PENDING"]

    t1 = t0 + timedelta(minutes=1)
    advance_one_bar(pos, _bar(t1, 4485.0, 4492.0, 4484.0, 4490.0), config)
    assert pos.stage == 1
    assert pos.stage1_time == t1

    t2 = t0 + timedelta(minutes=2)
    advance_one_bar(pos, _bar(t2, 4490.0, 4490.0, 4477.0, 4480.0), config)

    assert pos.entries[0].status == "LOCK_TP1"
    assert pos.entries[0].exit_price == signal.tp1
    assert [entry.status for entry in pos.entries[1:]] == ["OPEN", "OPEN"]
    assert [pos.lock_stage_for(entry, config.lock_after_tp1, config.lock_after_tp2) for entry in pos.entries[1:]] == [0, 0]


def test_sell_time_exit_uses_ask_side_from_bid_chart_close():
    signal = parse_one_signal(
        "1. SELL XAUUSD 100 - 101 SL 105 TP1 95 TP2 90 TP3 85 11:00 AM",
        source_date="2026-06-01",
        source_offset=3,
    )
    config = _fixed_config(entry_count=1, max_hold_minutes=1)
    pos = open_position(signal, equity=1000.0, config=config)

    t0 = pos.activation_time
    advance_one_bar(pos, _bar(t0, 99.0, 100.5, 98.0, 99.0), config)
    assert pos.entries[0].status == "OPEN"

    t1 = t0 + timedelta(minutes=1)
    advance_one_bar(pos, _bar(t1, 99.0, 99.5, 98.5, 99.0, spread=0.20), config)

    assert pos.entries[0].status == "TIME_EXIT"
    assert pos.entries[0].exit_price == 99.20
