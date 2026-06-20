"""--trailing-close-min-step throttles the executor-owned trailing-close SL
modifies: the MT5 SLTP request goes out only once the recomputed stop improves
on the broker's current SL by at least the step (the first protective set always
goes out). 0.0 keeps the legacy send-every-improvement behavior. The engine
still trails continuously -- this is purely a broker-traffic throttle.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta

from trading.xauusd import Bar, DEFAULT_CONFIG, Mt5Executor, advance_bars, open_position, parse_one_signal
from trading.xauusd.execution.mt5_executor import mt5_entry_comment, signal_to_magic


class _Resp:
    def __init__(self, retcode=10009, comment="done", order=123):
        self.retcode = retcode
        self.comment = comment
        self.order = order


class _Sym:
    digits = 2
    filling_mode = 2


class _Tick:
    def __init__(self, bid: float, ask: float):
        self.bid = bid
        self.ask = ask


class _Position:
    def __init__(self, *, ticket, magic, comment, sl, tp, ptype=0, volume=0.10):
        self.ticket = ticket
        self.magic = magic
        self.comment = comment
        self.sl = sl
        self.tp = tp
        self.type = ptype
        self.volume = volume
        self.time = 0
        self.price_open = 4750.0


class _FakeMt5:
    TRADE_ACTION_DEAL = 1
    TRADE_ACTION_PENDING = 5
    TRADE_ACTION_MODIFY = 6
    TRADE_ACTION_SLTP = 7
    TRADE_ACTION_REMOVE = 8
    TRADE_RETCODE_DONE = 10009
    ORDER_TYPE_BUY = 0
    ORDER_TYPE_SELL = 1
    ORDER_TYPE_BUY_STOP = 4
    ORDER_TYPE_SELL_STOP = 5
    POSITION_TYPE_BUY = 0
    POSITION_TYPE_SELL = 1
    ORDER_TIME_GTC = 0
    ORDER_FILLING_RETURN = 2
    ORDER_FILLING_IOC = 1
    SYMBOL_FILLING_IOC = 2
    SYMBOL_FILLING_FOK = 1

    def __init__(self, *, bid: float, ask: float):
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


def _bar(t: datetime, o: float, h: float, l: float, c: float) -> Bar:
    return Bar(t, o, h, l, c, 0, 0.0)


def _trailing_pos(min_step: float):
    sig = parse_one_signal(
        "1. BUY XAUUSD 4750 - 4748 SL 4744 TP1 4760 TP2 4770 TP3 4780 10:00 AM",
        source_date="2026-06-01",
        source_offset=3,
    )
    cfg = replace(
        DEFAULT_CONFIG,
        entry_count=1,
        trailing_close_distance=3.0,
        trailing_close_min_step=min_step,
        activation_delay_minutes=0,
    )
    pos = open_position(sig, 1000.0, cfg)
    t = sig.signal_time_chart
    advance_bars(pos, [
        _bar(t, 4752, 4752, 4749, 4751),
        _bar(t + timedelta(minutes=1), 4751, 4757, 4755, 4756),
    ], cfg)
    entry = pos.entries[0]
    assert entry.status == "OPEN"
    assert entry.trailing_stop == 4754.0  # high 4757 - distance 3
    return sig, cfg, pos


def _executor_with_position(sig, broker_sl: float):
    mt5 = _FakeMt5(bid=4756.0, ask=4756.2)
    mt5._positions = [_Position(
        ticket=501,
        magic=signal_to_magic(sig.signal_key),
        comment=mt5_entry_comment(sig.signal_key, 0),
        sl=broker_sl,
        tp=4780.0,
    )]
    return Mt5Executor(_FakeConn(mt5), "XAUUSD"), mt5


def _sltp_requests(mt5):
    return [r for r in mt5.requests if r["action"] == mt5.TRADE_ACTION_SLTP]


def test_min_step_suppresses_modify_below_threshold():
    sig, cfg, pos = _trailing_pos(min_step=0.5)
    # Broker SL 4753.8 -> target 4754 improves by only 0.2 < 0.5: hold.
    executor, mt5 = _executor_with_position(sig, broker_sl=4753.8)

    executor._apply_trailing_close_stops(pos, cfg)

    assert _sltp_requests(mt5) == []


def test_min_step_sends_modify_at_or_above_threshold():
    sig, cfg, pos = _trailing_pos(min_step=0.5)
    # Broker SL 4753.0 -> target 4754 improves by 1.0 >= 0.5: send.
    executor, mt5 = _executor_with_position(sig, broker_sl=4753.0)

    executor._apply_trailing_close_stops(pos, cfg)

    reqs = _sltp_requests(mt5)
    assert len(reqs) == 1
    assert reqs[0]["sl"] == 4754.0


def test_zero_min_step_keeps_legacy_every_improvement():
    sig, cfg, pos = _trailing_pos(min_step=0.0)
    executor, mt5 = _executor_with_position(sig, broker_sl=4753.8)

    executor._apply_trailing_close_stops(pos, cfg)

    reqs = _sltp_requests(mt5)
    assert len(reqs) == 1
    assert reqs[0]["sl"] == 4754.0


def test_first_protective_set_ignores_min_step():
    sig, cfg, pos = _trailing_pos(min_step=5.0)
    # No broker SL yet: the first protective set must always go out.
    executor, mt5 = _executor_with_position(sig, broker_sl=0.0)

    executor._apply_trailing_close_stops(pos, cfg)

    reqs = _sltp_requests(mt5)
    assert len(reqs) == 1
    assert reqs[0]["sl"] == 4754.0
