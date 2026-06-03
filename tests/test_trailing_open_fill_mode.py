"""The pending trailing-open STOP becomes a market DEAL when it triggers, so on a
market-execution broker it is bound by the symbol's market filling rule. Hard-coding
ORDER_FILLING_RETURN strands the fill (retcode 10030) on FOK/IOC-only brokers; the
placement must derive the mode from the symbol's advertised filling_mode bitmask.
"""
from __future__ import annotations

from datetime import timedelta

from xauusd_trading import Mt5Executor, NewSignalPlan, PlannedOrder, parse_one_signal
from xauusd_trading.execution import mt5_executor_trailing


class _Resp:
    def __init__(self, retcode=10009, comment="done", order=123):
        self.retcode = retcode
        self.comment = comment
        self.order = order


class _SymFOK:
    # Market-execution, FOK-only broker: SYMBOL_FILLING_FOK bit set, IOC clear.
    digits = 2
    filling_mode = 1


class _Tick:
    def __init__(self, bid: float, ask: float):
        self.bid = bid
        self.ask = ask


class _FakeMt5:
    TRADE_ACTION_PENDING = 5
    TRADE_ACTION_MODIFY = 6
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

    def __init__(self, *, bid: float, ask: float):
        self._tick = _Tick(bid, ask)
        self.requests = []
        self._orders = []
        self._positions = []

    def symbol_info(self, symbol):
        return _SymFOK()

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


def test_trailing_open_stop_uses_broker_fill_mode_not_return(monkeypatch):
    signal = parse_one_signal(
        "1. BUY XAUUSD 4750 - 4748 SL 4744 TP1 4760 TP2 4770 TP3 4780 10:00 AM",
        source_date="2026-06-01",
        source_offset=3,
    )
    activation = signal.signal_time_chart
    monkeypatch.setattr(
        mt5_executor_trailing, "_wall_clock_chart_now",
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

    # Ask 4740.2 is >= distance (2.0) below the planned entry, so the BUY STOP arms.
    mt5 = _FakeMt5(bid=4740.0, ask=4740.2)
    executor = Mt5Executor(_FakeConn(mt5), "XAUUSD")

    log = executor.place_signal(signal, plan)

    assert log.placed == 1
    assert len(mt5.requests) == 1
    request = mt5.requests[0]
    assert request["type"] == mt5.ORDER_TYPE_BUY_STOP
    # The whole point: a FOK-only market broker must get FOK, never RETURN.
    assert request["type_filling"] == mt5.ORDER_FILLING_FOK
    assert request["type_filling"] != mt5.ORDER_FILLING_RETURN