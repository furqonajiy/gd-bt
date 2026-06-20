"""Live executor wall-clock expiry parity tests.

Backtest pending orders expire at activation + pending_expiry even if that expiry
falls inside a market-data gap. Live Auto must cancel already-placed GTC pending
orders by the same real chart-time expiry, not only by latest MT5 M1 bar time.
"""
from __future__ import annotations

from datetime import timedelta

from trading.engine import DEFAULT_CONFIG, Mt5Executor, open_position, parse_one_signal, signal_to_magic


class _Resp:
    def __init__(self, retcode=10009, comment="done"):
        self.retcode = retcode
        self.comment = comment


class _Sym:
    digits = 2


class _Tick:
    bid = 4500.0
    ask = 4500.2


class _FakeOrder:
    def __init__(self, *, ticket, magic):
        self.ticket = ticket
        self.magic = magic
        self.comment = "test"


class _FakeMt5:
    TRADE_ACTION_REMOVE = 8
    TRADE_ACTION_SLTP = 6
    TRADE_ACTION_DEAL = 1
    TRADE_RETCODE_DONE = 10009
    ORDER_TYPE_SELL = 1
    ORDER_TYPE_BUY = 0
    ORDER_FILLING_RETURN = 2
    POSITION_TYPE_BUY = 0
    POSITION_TYPE_SELL = 1

    def __init__(self, orders):
        self._orders = list(orders)
        self.requests = []

    def symbol_info(self, symbol):
        return _Sym()

    def symbol_info_tick(self, symbol):
        return _Tick()

    def positions_get(self, symbol=None):
        return []

    def orders_get(self, symbol=None):
        return list(self._orders)

    def order_send(self, request):
        self.requests.append(dict(request))
        if request.get("action") == self.TRADE_ACTION_REMOVE:
            self._orders = [o for o in self._orders if o.ticket != request.get("order")]
        return _Resp()

    def last_error(self):
        return (0, "ok")


class _FakeConn:
    def __init__(self, mt5):
        self.mt5 = mt5


def test_live_manage_cancels_pending_by_wall_clock_even_when_chart_time_is_stale():
    signal = parse_one_signal(
        "5. SELL XAUUSD 4510 - 4512 SL 4518 TP1 4500 TP2 4490 TP3 4480 03:53 PM",
        source_date="2026-05-28",
        source_offset=3,
    )
    pos = open_position(signal, equity=1000.0, config=DEFAULT_CONFIG)

    # Simulate stale chart replay: base executor would not cancel because this is
    # still before strategy expiry, but real wall-clock time is past May 2026.
    stale_chart_now = pos.expiry_time - timedelta(minutes=30)

    mt5 = _FakeMt5([_FakeOrder(ticket=12345, magic=signal_to_magic(signal.signal_key))])
    executor = Mt5Executor(_FakeConn(mt5), "XAUUSD")

    log = executor.manage_position(pos, DEFAULT_CONFIG, stale_chart_now)

    assert log.cancelled == 1
    assert mt5.orders_get() == []
    assert any("Cancelled wall-clock expired pending" in action for action in log.actions)
    assert any(req.get("action") == mt5.TRADE_ACTION_REMOVE for req in mt5.requests)
