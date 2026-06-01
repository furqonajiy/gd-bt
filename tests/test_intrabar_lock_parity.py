"""Regression tests for M1 intrabar lock/live parity.

These tests protect the DD40 rule that TP1/TP2 touches cannot be applied
retroactively to entries that filled on the same M1 candle or after the touch.
They also verify live MT5 management only modifies the mapped entries that the
shared replay says are protected.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from types import SimpleNamespace

from xauusd_trading import Bar, DEFAULT_CONFIG, advance_one_bar, open_position, parse_one_signal
from xauusd_trading.execution import mt5_executor_tp2
from xauusd_trading import Mt5Executor


DD40_CONFIG = replace(
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


class _Resp:
    retcode = 10009
    comment = "done"


class _FakeMt5:
    TRADE_RETCODE_DONE = 10009
    TRADE_ACTION_REMOVE = 1
    TRADE_ACTION_SLTP = 6
    TRADE_ACTION_DEAL = 7
    POSITION_TYPE_BUY = 0
    POSITION_TYPE_SELL = 1
    ORDER_TYPE_BUY = 0
    ORDER_TYPE_SELL = 1
    ORDER_FILLING_IOC = 1
    ORDER_FILLING_FOK = 0
    ORDER_FILLING_RETURN = 2
    SYMBOL_FILLING_IOC = 2
    SYMBOL_FILLING_FOK = 1

    def __init__(self, positions):
        self._positions = positions
        self.requests = []

    def symbol_info(self, symbol):
        return SimpleNamespace(digits=2, filling_mode=self.SYMBOL_FILLING_IOC)

    def symbol_info_tick(self, symbol):
        return SimpleNamespace(bid=4490.0, ask=4490.2)

    def positions_get(self, symbol=None):
        return list(self._positions)

    def orders_get(self, symbol=None):
        return []

    def order_send(self, request):
        self.requests.append(dict(request))
        return _Resp()

    def last_error(self):
        return (0, "ok")


class _FakeConn:
    def __init__(self, mt5):
        self.mt5 = mt5


def _signal():
    return parse_one_signal(
        "2. BUY XAUUSD 4483 - 4481 SL 4476 TP1 4491 TP2 4501 TP3 4521 11:11 AM",
        source_date="2026-06-01",
        source_offset=3,
    )


def test_same_m1_fill_and_tp1_touch_does_not_lock_or_close_entry():
    pos = open_position(_signal(), equity=5000.0, config=DD40_CONFIG)
    bar = Bar(
        time=pos.activation_time,
        open=4495.00,
        high=4492.00,   # TP1 was inside this candle
        low=4482.70,    # BUY entry #1 fills inside the same candle
        close=4485.00,
        spread_points=20,
        spread_price=0.20,
    )

    advance_one_bar(pos, bar, DD40_CONFIG)

    assert pos.entries[0].status == "OPEN"
    assert pos.entries[0].fill_time == bar.time
    assert pos.stage == 0
    assert pos.stage1_time is None
    assert pos.lock_stage_for(pos.entries[0], True, False) == 0


def test_live_sl_lock_only_modifies_entries_protected_by_replay(monkeypatch):
    pos = open_position(_signal(), equity=5000.0, config=DD40_CONFIG)
    touch_time = pos.activation_time + timedelta(minutes=10)
    pos.stage = 1
    pos.stage1_time = touch_time
    pos.first_fill_time = pos.activation_time
    pos.time_exit_deadline = touch_time + timedelta(minutes=90)

    # Entry #1 existed before TP1 touch and may be locked.
    pos.entries[0].status = "OPEN"
    pos.entries[0].fill_time = touch_time - timedelta(minutes=1)

    # Entry #2 filled after TP1 was already seen and must not inherit TP1 lock.
    pos.entries[1].status = "OPEN"
    pos.entries[1].fill_time = touch_time + timedelta(minutes=1)

    p1 = SimpleNamespace(
        ticket=101,
        comment=f"{pos.signal.signal_key}.1",
        time=0,
        price_open=pos.entries[0].entry_price,
        volume=pos.entries[0].lot,
        sl=pos.entries[0].initial_sl,
        tp=pos.target_level,
        type=_FakeMt5.POSITION_TYPE_BUY,
    )
    p2 = SimpleNamespace(
        ticket=102,
        comment=f"{pos.signal.signal_key}.2",
        time=1,
        price_open=pos.entries[1].entry_price,
        volume=pos.entries[1].lot,
        sl=pos.entries[1].initial_sl,
        tp=pos.target_level,
        type=_FakeMt5.POSITION_TYPE_BUY,
    )
    mt5 = _FakeMt5([p1, p2])
    monkeypatch.setattr(mt5_executor_tp2, "_wall_clock_chart_now", lambda: touch_time + timedelta(minutes=2))

    executor = Mt5Executor(_FakeConn(mt5), "XAUUSD")
    log = executor.manage_position(pos, DD40_CONFIG, chart_now=touch_time + timedelta(minutes=2))

    sltp_requests = [r for r in mt5.requests if r["action"] == mt5.TRADE_ACTION_SLTP]
    assert log.modified == 1
    assert len(sltp_requests) == 1
    assert sltp_requests[0]["position"] == 101
    assert sltp_requests[0]["sl"] == round(pos.signal.tp1, 2)
