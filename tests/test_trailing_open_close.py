from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta

from xauusd_trading import Bar, DEFAULT_CONFIG, Mt5Executor, NewSignalPlan, PlannedOrder
from xauusd_trading import advance_bars, open_position, parse_one_signal
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


def _bar(t: datetime, o: float, h: float, l: float, c: float, spread: float = 0.0) -> Bar:
    return Bar(t, o, h, l, c, int(round(spread / 0.01)), spread)


def test_trailing_open_does_not_fill_buy_limit_while_price_keeps_dropping():
    sig = parse_one_signal(
        "1. BUY XAUUSD 4750 - 4748 SL 4744 TP1 4760 TP2 4770 TP3 4780 10:00 AM",
        source_date="2026-06-01",
        source_offset=3,
    )
    cfg = replace(
        DEFAULT_CONFIG,
        entry_count=1,
        trailing_open_distance=2.0,
        activation_delay_minutes=0,
    )
    pos = open_position(sig, 1000.0, cfg)
    t = sig.signal_time_chart

    advance_bars(pos, [
        _bar(t, 4755, 4755, 4749, 4750),
        _bar(t + timedelta(minutes=1), 4750, 4751, 4740, 4741),
    ], cfg)

    assert pos.entries[0].status == "PENDING"
    assert pos.entries[0].fill_time is None


def test_trailing_open_fills_buy_after_rebound_from_low():
    sig = parse_one_signal(
        "1. BUY XAUUSD 4750 - 4748 SL 4744 TP1 4760 TP2 4770 TP3 4780 10:00 AM",
        source_date="2026-06-01",
        source_offset=3,
    )
    cfg = replace(
        DEFAULT_CONFIG,
        entry_count=1,
        trailing_open_distance=2.0,
        activation_delay_minutes=0,
    )
    pos = open_position(sig, 1000.0, cfg)
    t = sig.signal_time_chart

    advance_bars(pos, [
        _bar(t, 4755, 4755, 4740, 4741),
        _bar(t + timedelta(minutes=1), 4741, 4742, 4740, 4742),
    ], cfg)

    assert pos.entries[0].status == "OPEN"
    assert pos.entries[0].fill_time == t + timedelta(minutes=1)
    assert pos.entries[0].entry_price == 4742


def test_trailing_close_advances_stop_and_can_close_later():
    sig = parse_one_signal(
        "1. BUY XAUUSD 4750 - 4748 SL 4744 TP1 4760 TP2 4770 TP3 4780 10:00 AM",
        source_date="2026-06-01",
        source_offset=3,
    )
    cfg = replace(
        DEFAULT_CONFIG,
        entry_count=1,
        trailing_close_distance=3.0,
        activation_delay_minutes=0,
    )
    pos = open_position(sig, 1000.0, cfg)
    t = sig.signal_time_chart

    advance_bars(pos, [
        _bar(t, 4752, 4752, 4749, 4751),
        _bar(t + timedelta(minutes=1), 4751, 4757, 4751, 4756),
        _bar(t + timedelta(minutes=2), 4756, 4756, 4754, 4754),
    ], cfg)

    assert pos.entries[0].status == "SL"
    assert pos.entries[0].exit_price == 4754
    assert pos.entries[0].stop_at_exit == 4754


def test_live_executor_does_not_place_normal_limit_when_trailing_open_enabled(monkeypatch):
    signal = parse_one_signal(
        "1. BUY XAUUSD 4750 - 4748 SL 4744 TP1 4760 TP2 4770 TP3 4780 10:00 AM",
        source_date="2026-06-01",
        source_offset=3,
    )
    activation = signal.signal_time_chart
    monkeypatch.setattr(mt5_executor_tp2, "_wall_clock_chart_now", lambda: activation + timedelta(minutes=1))
    plan = NewSignalPlan(
        signal=signal,
        action="FOLLOW",
        rationale="test",
        orders=[PlannedOrder(0, signal.side, 4750.0, 4740.34, 0.10, 96.6)],
        pending_expires_at=activation + timedelta(minutes=630),
        final_target_label="TP3",
        final_target_price=4780.0,
        total_initial_risk_dollars=96.6,
        pending_activates_at=activation,
    )
    plan.trailing_open_distance = 2.0

    mt5 = _FakeMt5(bid=4740.0, ask=4740.2)
    executor = Mt5Executor(_FakeConn(mt5), "XAUUSD")

    log = executor.place_signal(signal, plan)

    assert log.placed == 0
    assert mt5.requests == []
    assert any("normal broker LIMIT orders are not placed" in action for action in log.actions)
