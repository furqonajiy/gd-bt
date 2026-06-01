from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta

from xauusd_trading import Bar, DEFAULT_CONFIG, advance_bars, open_position, parse_one_signal


def _bar(t: datetime, o: float, h: float, l: float, c: float, spread: float = 0.0) -> Bar:
    return Bar(t, o, h, l, c, int(round(spread / 0.01)), spread)


def test_sell_trend_runner_holds_after_tp3_and_closes_on_atr_trailing_stop():
    sig = parse_one_signal(
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
    pos = open_position(sig, 1000.0, cfg)
    t = sig.signal_time_chart

    # Fill the short, pass TP3 in a strong downtrend, then keep riding lower.
    advance_bars(pos, [
        _bar(t, 4501, 4501, 4499, 4500),
        _bar(t + timedelta(minutes=1), 4500, 4500, 4490, 4492),
        _bar(t + timedelta(minutes=2), 4492, 4492, 4470, 4470),
        _bar(t + timedelta(minutes=3), 4470, 4470, 4450, 4452),
    ], cfg)

    entry = pos.entries[0]
    assert entry.status == "OPEN"
    assert getattr(pos, "trend_runner_active", False) is True
    assert entry.exit_time is None
    assert entry.trailing_stop is not None
    assert entry.trailing_stop <= sig.tp2

    # Reversal into the runner stop closes later with more profit than TP3.
    advance_bars(pos, [
        _bar(t + timedelta(minutes=4), 4452, 4465, 4451, 4464),
    ], cfg)

    assert entry.status == "TRAILING_STOP"
    assert entry.exit_price < sig.tp3
    assert entry.pnl is not None and entry.pnl > (entry.entry_price - sig.tp3) * entry.lot * 100


def test_trend_runner_disabled_keeps_normal_tp3_close():
    sig = parse_one_signal(
        "1. SELL XAUUSD 4500 - 4502 SL 4508 TP1 4490 TP2 4480 TP3 4470 10:00 PM",
        source_date="2026-05-29",
        source_offset=0,
    )
    cfg = replace(DEFAULT_CONFIG, activation_delay_minutes=0, entry_count=1, trend_runner_enabled=False)
    pos = open_position(sig, 1000.0, cfg)
    t = sig.signal_time_chart

    advance_bars(pos, [
        _bar(t, 4501, 4501, 4499, 4500),
        _bar(t + timedelta(minutes=1), 4500, 4500, 4470, 4470),
    ], cfg)

    assert pos.entries[0].status == "TP3"
    assert pos.entries[0].exit_price == sig.tp3


def test_trend_runner_does_not_override_max_hold_before_tp3():
    sig = parse_one_signal(
        "1. BUY XAUUSD 4500 - 4498 SL 4494 TP1 4510 TP2 4520 TP3 4530 10:00 PM",
        source_date="2026-05-29",
        source_offset=0,
    )
    cfg = replace(
        DEFAULT_CONFIG,
        activation_delay_minutes=0,
        entry_count=1,
        trend_runner_enabled=True,
        max_hold_minutes=1,
    )
    pos = open_position(sig, 1000.0, cfg)
    t = sig.signal_time_chart

    advance_bars(pos, [
        _bar(t, 4501, 4502, 4499, 4501),
        _bar(t + timedelta(minutes=1), 4501, 4505, 4500, 4504),
    ], cfg)

    assert pos.entries[0].status == "TIME_EXIT"


def test_active_runner_stop_tightened_from_current_bar_only_triggers_next_bar():
    sig = parse_one_signal(
        "1. SELL XAUUSD 4500 - 4502 SL 4508 TP1 4490 TP2 4480 TP3 4470 10:00 PM",
        source_date="2026-05-29",
        source_offset=0,
    )
    cfg = replace(
        DEFAULT_CONFIG,
        activation_delay_minutes=0,
        entry_count=1,
        trend_runner_enabled=True,
        trend_runner_atr_period=1,
        trend_runner_atr_multiplier=1.0,
        trend_runner_override_max_hold=True,
        max_hold_minutes=90,
    )
    pos = open_position(sig, 1000.0, cfg)
    t = sig.signal_time_chart
    entry = pos.entries[0]
    fill_time = t - timedelta(minutes=5)

    entry.status = "OPEN"
    entry.fill_time = fill_time
    entry.trailing_stop = sig.tp2
    pos.first_fill_time = fill_time
    pos.time_exit_deadline = fill_time + timedelta(minutes=cfg.max_hold_minutes)
    pos.stage = 3
    pos.stage3_time = t - timedelta(minutes=4)
    pos.trend_runner_active = True
    pos.trend_prev_close = 4455.0

    # Same-bar look-ahead bug: this bar's low tightens SELL stop from 4480 to
    # 4470, and its high touches 4470. The prior 4480 stop is not touched, so the
    # runner must survive and only arm 4470 for the next bar.
    advance_bars(pos, [
        _bar(t, 4455, 4470, 4450, 4455),
    ], cfg)

    assert entry.status == "OPEN"
    assert entry.exit_time is None
    assert entry.trailing_stop == 4470.0

    advance_bars(pos, [
        _bar(t + timedelta(minutes=1), 4455, 4470, 4454, 4460),
    ], cfg)

    assert entry.status == "TRAILING_STOP"
    assert entry.exit_price == 4470.0
