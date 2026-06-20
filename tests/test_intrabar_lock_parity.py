from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

from trading.xauusd import Bar, DEFAULT_CONFIG, advance_one_bar, open_position, parse_one_signal


CFG = replace(
    DEFAULT_CONFIG,
    initial_capital=1000.0,
    sizing_mode="risk",
    risk_per_signal=0.05575,
    entry_count=3,
    entry_ladder="range_to_sl",
    entry_sl_gap=2.0,
    activation_delay_minutes=3,
    pending_expiry_minutes=630,
    max_hold_minutes=90,
    sl_multiplier=1.61,
    final_target="TP3",
    lock_after_tp1=True,
    lock_after_tp2=False,
    profit_lock_mode="tp_levels",
)


def _position():
    sig = parse_one_signal(
        "2. BUY XAUUSD 4483 - 4481 SL 4476 TP1 4491 TP2 4501 TP3 4521 11:11 AM",
        source_date="2026-06-01",
        source_offset=3,
    )
    return open_position(sig, equity=5000.0, config=CFG)


def test_same_m1_fill_and_tp1_touch_does_not_retroactively_lock_entry():
    pos = _position()
    bar = Bar(
        time=pos.activation_time,
        open=4495.0,
        high=4492.0,
        low=4482.7,
        close=4485.0,
        spread_points=20,
        spread_price=0.20,
    )

    advance_one_bar(pos, bar, CFG)

    assert pos.entries[0].status == "OPEN"
    assert pos.entries[0].fill_time == bar.time
    assert pos.stage == 0
    assert pos.stage1_time is None
    assert pos.lock_stage_for(pos.entries[0], True, False) == 0


def test_lock_stage_is_per_entry_and_not_retroactive_after_tp1_touch():
    pos = _position()
    touch_time = pos.activation_time + timedelta(minutes=10)
    pos.stage = 1
    pos.stage1_time = touch_time

    pos.entries[0].status = "OPEN"
    pos.entries[0].fill_time = touch_time - timedelta(minutes=1)
    pos.entries[1].status = "OPEN"
    pos.entries[1].fill_time = touch_time + timedelta(minutes=1)

    assert pos.lock_stage_for(pos.entries[0], True, False) == 1
    assert pos.lock_stage_for(pos.entries[1], True, False) == 0
