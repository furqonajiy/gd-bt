from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

from trading.engine import Mt5Executor, NewSignalPlan, PlannedOrder, parse_one_signal
from trading.engine.execution import mt5_executor_trailing
from trading.engine.execution.mt5_executor import ExecutionLog, signal_to_magic


class _Resp:
    def __init__(self, retcode=10009, comment="done", order=123):
        self.retcode = retcode
        self.comment = comment
        self.order = order


class _Sym:
    digits = 2
    filling_mode = 1


class _Tick:
    def __init__(self, bid: float, ask: float):
        self.bid = bid
        self.ask = ask


class _FakeMt5:
    TRADE_ACTION_PENDING = 5
    TRADE_ACTION_SLTP = 7
    TRADE_RETCODE_DONE = 10009
    ORDER_TYPE_BUY_STOP = 4
    ORDER_TYPE_SELL_STOP = 5
    ORDER_TIME_GTC = 0
    ORDER_FILLING_FOK = 0
    ORDER_FILLING_IOC = 1
    ORDER_FILLING_RETURN = 2
    SYMBOL_FILLING_FOK = 1
    SYMBOL_FILLING_IOC = 2

    def __init__(self, *, bid: float = 4740.0, ask: float = 4740.2):
        self._tick = _Tick(bid, ask)
        self.requests = []
        self._orders = []
        self._positions = []

    def symbol_info(self, symbol):
        return _Sym()

    def symbol_info_tick(self, symbol):
        return self._tick

    def positions_get(self, symbol=None):
        return list(self._positions)

    def orders_get(self, symbol=None):
        return list(self._orders)

    def order_send(self, request):
        self.requests.append(dict(request))
        return _Resp(order=1000 + len(self.requests))

    def last_error(self):
        return (0, "ok")


class _FakeConn:
    def __init__(self, mt5):
        self.mt5 = mt5


def _signal():
    return parse_one_signal(
        "1. BUY XAUUSD 4750 - 4748 SL 4744 TP1 4760 TP2 4770 TP3 4780 10:00 AM",
        source_date="2026-06-01",
        source_offset=3,
    )


def test_trailing_close_placement_sends_no_broker_tp(monkeypatch):
    signal = _signal()
    activation = signal.signal_time_chart
    monkeypatch.setattr(
        mt5_executor_trailing,
        "_wall_clock_chart_now",
        lambda: activation + timedelta(minutes=1),
    )
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
        trailing_open_distance=2.0,
    )
    setattr(plan, "trailing_close_distance", 0.5)

    mt5 = _FakeMt5()
    executor = Mt5Executor(_FakeConn(mt5), "XAUUSD")

    log = executor.place_signal(signal, plan)

    assert log.placed == 1
    assert mt5.requests[0]["tp"] == 0.0
    assert any("TP=none" in action for action in log.actions)


def test_trailing_close_manage_removes_existing_broker_tp():
    signal = _signal()
    magic = signal_to_magic(signal.signal_key)
    mt5 = _FakeMt5()
    mt5._positions = [
        SimpleNamespace(ticket=42, magic=magic, sl=4744.0, tp=4780.0),
    ]
    executor = Mt5Executor(_FakeConn(mt5), "XAUUSD")
    engine_pos = SimpleNamespace(signal=signal)
    log = ExecutionLog()

    executor._remove_existing_broker_take_profits(engine_pos, log)

    assert log.modified == 1
    assert mt5.requests[0]["action"] == mt5.TRADE_ACTION_SLTP
    assert mt5.requests[0]["position"] == 42
    assert mt5.requests[0]["sl"] == 4744.0
    assert mt5.requests[0]["tp"] == 0.0
