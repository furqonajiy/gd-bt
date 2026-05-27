"""Regression tests for TP2 live-execution parity.

The backtest/replay engine moves the effective stop to TP2 once TP2 has been
touched.  The public Mt5Executor should therefore move live broker SLs to TP2
when the replayed Position reaches stage 2.
"""
from __future__ import annotations
from datetime import datetime

from xauusd_trading import DEFAULT_CONFIG, Mt5Executor, open_position, parse_one_signal, signal_to_magic


class _Resp:
    def __init__(self, retcode=10009, comment="done", order=123):
        self.retcode = retcode
        self.comment = comment
        self.order = order


class _Sym:
    digits = 2


class _Tick:
    bid = 4500.0
    ask = 4500.2


class _FakePosition:
    def __init__(self, *, ticket, magic, type_, sl, tp, volume=0.5):
        self.ticket = ticket
        self.magic = magic
        self.type = type_
        self.sl = sl
        self.tp = tp
        self.volume = volume


class _FakeMt5:
    TRADE_ACTION_SLTP = 6
    TRADE_ACTION_DEAL = 1
    TRADE_RETCODE_DONE = 10009
    ORDER_TYPE_SELL = 1
    ORDER_TYPE_BUY = 0
    ORDER_FILLING_RETURN = 2
    POSITION_TYPE_BUY = 0
    POSITION_TYPE_SELL = 1

    def __init__(self, positions):
        self._positions = list(positions)
        self.requests = []

    def symbol_info(self, symbol):
        return _Sym()

    def symbol_info_tick(self, symbol):
        return _Tick()

    def positions_get(self, symbol=None):
        return list(self._positions)

    def orders_get(self, symbol=None):
        return []

    def order_send(self, request):
        self.requests.append(dict(request))
        if request.get("action") == self.TRADE_ACTION_SLTP:
            for p in self._positions:
                if p.ticket == request["position"]:
                    p.sl = request["sl"]
                    p.tp = request["tp"]
                    break
        return _Resp()

    def last_error(self):
        return (0, "ok")


class _FakeConn:
    def __init__(self, mt5):
        self.mt5 = mt5


def test_public_mt5_executor_moves_live_sl_to_tp2_when_engine_stage_is_2():
    signal = parse_one_signal(
        "1. BUY XAUUSD 4518 - 4516 SL 4511 TP1 4526 TP2 4536 TP3 4551 11:25 AM",
        source_date="2026-05-05",
        source_offset=7,
    )
    pos = open_position(signal, equity=1000.0, config=DEFAULT_CONFIG)
    pos.stage = 2
    pos.stage1_time = datetime(2026, 5, 5, 7, 30)
    pos.stage2_time = datetime(2026, 5, 5, 7, 35)
    pos.entries[0].status = "OPEN"
    pos.entries[0].fill_time = datetime(2026, 5, 5, 7, 27)

    mt5_pos = _FakePosition(
        ticket=777,
        magic=signal_to_magic(signal.signal_key),
        type_=_FakeMt5.POSITION_TYPE_BUY,
        sl=signal.tp1,
        tp=signal.tp3,
    )
    mt5 = _FakeMt5([mt5_pos])
    executor = Mt5Executor(_FakeConn(mt5), "XAUUSD")

    log = executor.manage_position(pos, DEFAULT_CONFIG, datetime(2026, 5, 5, 7, 36))

    assert mt5_pos.sl == signal.tp2
    assert any(req.get("action") == mt5.TRADE_ACTION_SLTP and req.get("sl") == signal.tp2 for req in mt5.requests)
    assert any("Locked SL" in action and "TP2" in action for action in log.actions)
