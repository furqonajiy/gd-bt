"""Tests for live stale LIMIT and wall-clock expiry protection.

These tests cover the bug where Auto can evaluate an old signal against stale
MT5 chart history and repeatedly try to place a LIMIT order after the real
pending window has already expired.
"""
from __future__ import annotations
from datetime import datetime, timedelta

from xauusd_trading import DEFAULT_CONFIG, Mt5Executor, NewSignalPlan, PlannedOrder, parse_one_signal
from xauusd_trading.execution import mt5_executor_tp2


class _Resp:
    def __init__(self, retcode=10009, comment="done", order=123):
        self.retcode = retcode
        self.comment = comment
        self.order = order


class _Sym:
    digits = 2


class _Tick:
    def __init__(self, bid: float, ask: float):
        self.bid = bid
        self.ask = ask


class _FakeMt5:
    TRADE_ACTION_PENDING = 5
    TRADE_RETCODE_DONE = 10009
    ORDER_TYPE_BUY_LIMIT = 2
    ORDER_TYPE_SELL_LIMIT = 3
    ORDER_TIME_GTC = 0
    ORDER_FILLING_RETURN = 2

    def __init__(self, *, bid: float, ask: float):
        self._tick = _Tick(bid, ask)
        self.requests = []

    def symbol_info(self, symbol):
        return _Sym()

    def symbol_info_tick(self, symbol):
        return self._tick

    def positions_get(self, symbol=None):
        return []

    def orders_get(self, symbol=None):
        return []

    def order_send(self, request):
        self.requests.append(dict(request))
        return _Resp(order=1000 + len(self.requests))

    def last_error(self):
        return (0, "ok")


class _FakeConn:
    def __init__(self, mt5):
        self.mt5 = mt5


def _reset_executor_guards():
    Mt5Executor._session_skipped_inactive_signal_keys.clear()
    Mt5Executor._session_skipped_stale_entries.clear()
    Mt5Executor._session_skipped_expired_signal_keys.clear()
    Mt5Executor._session_failed_signal_keys.clear()


def _freeze_wall_clock(monkeypatch, when: datetime):
    monkeypatch.setattr(mt5_executor_tp2, "_wall_clock_chart_now", lambda: when)


def _make_plan(signal, *, entry_price: float, initial_sl: float, target: float):
    activation = signal.signal_time_chart + timedelta(minutes=DEFAULT_CONFIG.activation_delay_minutes)
    risk_dollars = 10.0
    return NewSignalPlan(
        signal=signal,
        action="FOLLOW",
        rationale="test",
        orders=[
            PlannedOrder(
                entry_index=0,
                side=signal.side,
                entry_price=entry_price,
                initial_sl=initial_sl,
                lot=0.13,
                risk_dollars=risk_dollars,
            )
        ],
        pending_expires_at=activation + timedelta(minutes=DEFAULT_CONFIG.pending_expiry_minutes),
        final_target_label="TP3",
        final_target_price=target,
        total_initial_risk_dollars=risk_dollars,
    )


def test_inactive_signal_waits_for_activation_before_order_send(monkeypatch):
    _reset_executor_guards()
    signal = parse_one_signal(
        "5. SELL XAUUSD 4510 - 4512 SL 4518 TP1 4500 TP2 4490 TP3 4480 03:53 PM",
        source_date="2026-05-28",
        source_offset=3,
    )
    _freeze_wall_clock(monkeypatch, signal.signal_time_chart + timedelta(minutes=1))
    plan = _make_plan(signal, entry_price=4510.0, initial_sl=4522.08, target=4480.0)
    mt5 = _FakeMt5(bid=4504.58, ask=4504.85)
    executor = Mt5Executor(_FakeConn(mt5), "XAUUSD")

    log = executor.place_signal(signal, plan)

    assert log.placed == 0
    assert mt5.requests == []
    assert any("waiting for activation" in action for action in log.actions)


def test_expired_signal_is_skipped_before_order_send_even_if_limit_side_is_valid(monkeypatch):
    _reset_executor_guards()
    signal = parse_one_signal(
        "5. SELL XAUUSD 4510 - 4512 SL 4518 TP1 4500 TP2 4490 TP3 4480 03:53 PM",
        source_date="2026-05-28",
        source_offset=3,
    )
    plan = _make_plan(signal, entry_price=4510.0, initial_sl=4522.08, target=4480.0)
    _freeze_wall_clock(monkeypatch, plan.pending_expires_at + timedelta(minutes=1))
    mt5 = _FakeMt5(bid=4504.58, ask=4504.85)
    executor = Mt5Executor(_FakeConn(mt5), "XAUUSD")

    log = executor.place_signal(signal, plan)

    assert log.placed == 0
    assert mt5.requests == []
    assert any("skipped expired by wall-clock" in action for action in log.actions)


def test_stale_sell_limit_below_live_bid_is_skipped_before_order_send(monkeypatch):
    _reset_executor_guards()
    signal = parse_one_signal(
        "5. SELL XAUUSD 4418 - 4420 SL 4425.5 TP1 4410 TP2 4400 TP3 4380 03:53 PM",
        source_date="2026-05-28",
        source_offset=3,
    )
    _freeze_wall_clock(monkeypatch, signal.signal_time_chart + timedelta(minutes=DEFAULT_CONFIG.activation_delay_minutes + 1))
    plan = _make_plan(signal, entry_price=4418.0, initial_sl=4430.08, target=4380.0)
    mt5 = _FakeMt5(bid=4504.58, ask=4504.85)
    executor = Mt5Executor(_FakeConn(mt5), "XAUUSD")

    log = executor.place_signal(signal, plan)

    assert log.placed == 0
    assert mt5.requests == []
    assert any("skipped stale SELL LIMIT 4418" in action for action in log.actions)


def test_valid_sell_limit_above_live_bid_is_sent_to_mt5(monkeypatch):
    _reset_executor_guards()
    signal = parse_one_signal(
        "5. SELL XAUUSD 4510 - 4512 SL 4518 TP1 4500 TP2 4490 TP3 4480 03:53 PM",
        source_date="2026-05-28",
        source_offset=3,
    )
    _freeze_wall_clock(monkeypatch, signal.signal_time_chart + timedelta(minutes=DEFAULT_CONFIG.activation_delay_minutes + 1))
    plan = _make_plan(signal, entry_price=4510.0, initial_sl=4522.08, target=4480.0)
    mt5 = _FakeMt5(bid=4504.58, ask=4504.85)
    executor = Mt5Executor(_FakeConn(mt5), "XAUUSD")

    log = executor.place_signal(signal, plan)

    assert log.placed == 1
    assert len(mt5.requests) == 1
    assert mt5.requests[0]["type"] == mt5.ORDER_TYPE_SELL_LIMIT
    assert mt5.requests[0]["price"] == 4510.0


def test_stale_buy_limit_above_live_ask_is_skipped_before_order_send(monkeypatch):
    _reset_executor_guards()
    signal = parse_one_signal(
        "6. BUY XAUUSD 4510 - 4508 SL 4502 TP1 4520 TP2 4530 TP3 4540 03:53 PM",
        source_date="2026-05-28",
        source_offset=3,
    )
    _freeze_wall_clock(monkeypatch, signal.signal_time_chart + timedelta(minutes=DEFAULT_CONFIG.activation_delay_minutes + 1))
    plan = _make_plan(signal, entry_price=4510.0, initial_sl=4498.0, target=4540.0)
    mt5 = _FakeMt5(bid=4504.58, ask=4504.85)
    executor = Mt5Executor(_FakeConn(mt5), "XAUUSD")

    log = executor.place_signal(signal, plan)

    assert log.placed == 0
    assert mt5.requests == []
    assert any("skipped stale BUY LIMIT 4510" in action for action in log.actions)
