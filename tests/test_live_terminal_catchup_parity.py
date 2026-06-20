from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

from trading.engine import DEFAULT_CONFIG, Mt5Executor, open_position, parse_one_signal
from trading.engine.execution import mt5_executor_tp2


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


class _Mt5Position:
    def __init__(self, *, ticket, magic, comment, time, price_open, volume=0.10, sl=0.0, tp=0.0, type_=0):
        self.ticket = ticket
        self.magic = magic
        self.comment = comment
        self.time = time
        self.price_open = price_open
        self.volume = volume
        self.sl = sl
        self.tp = tp
        self.type = type_


class _FakeMt5:
    TRADE_ACTION_DEAL = 1
    TRADE_ACTION_SLTP = 6
    TRADE_RETCODE_DONE = 10009
    ORDER_TYPE_BUY = 0
    ORDER_TYPE_SELL = 1
    ORDER_FILLING_RETURN = 2
    ORDER_FILLING_IOC = 1
    ORDER_FILLING_FOK = 0
    SYMBOL_FILLING_IOC = 2
    SYMBOL_FILLING_FOK = 1
    POSITION_TYPE_BUY = 0
    POSITION_TYPE_SELL = 1

    def __init__(self, *, bid=4500.0, ask=4500.2, positions=None):
        self._tick = _Tick(bid, ask)
        self._positions = list(positions or [])
        self.requests = []

    def symbol_info(self, symbol):
        return _Sym()

    def symbol_info_tick(self, symbol):
        return self._tick

    def positions_get(self, symbol=None):
        return list(self._positions)

    def orders_get(self, symbol=None):
        return []

    def order_send(self, request):
        self.requests.append(dict(request))
        if request["action"] == self.TRADE_ACTION_DEAL:
            ticket = request.get("position")
            self._positions = [p for p in self._positions if p.ticket != ticket]
            return _Resp(order=ticket or 0)
        if request["action"] == self.TRADE_ACTION_SLTP:
            for p in self._positions:
                if p.ticket == request["position"]:
                    p.sl = request["sl"]
                    p.tp = request["tp"]
                    break
            return _Resp(order=request.get("position", 0))
        return _Resp()

    def last_error(self):
        return (0, "ok")


class _FakeConn:
    def __init__(self, mt5):
        self.mt5 = mt5


def _fixed_config():
    return replace(
        DEFAULT_CONFIG,
        sizing_mode="fixed",
        lot_per_entry=0.10,
        entry_count=3,
        entry_ladder="range_to_sl",
        entry_sl_gap=2.0,
        activation_delay_minutes=3,
        pending_expiry_minutes=630,
        max_hold_minutes=90,
        final_target="TP3",
        lock_after_tp1=True,
        lock_after_tp2=False,
    )


def test_manage_closes_live_position_when_replay_entry_is_terminal():
    signal = parse_one_signal(
        "2. BUY XAUUSD 4483 - 4481 SL 4476 TP1 4491 TP2 4501 TP3 4521 11:11 AM",
        source_date="2026-06-01",
        source_offset=3,
    )
    config = _fixed_config()
    pos = open_position(signal, equity=1000.0, config=config)
    pos.entries[0].status = "TP3"
    pos.entries[0].fill_time = pos.activation_time
    pos.entries[0].exit_time = pos.activation_time + timedelta(minutes=10)
    pos.entries[0].exit_price = signal.tp3
    pos.first_fill_time = pos.activation_time
    pos.time_exit_deadline = pos.activation_time + timedelta(minutes=config.max_hold_minutes)

    magic = mt5_executor_tp2.signal_to_magic(signal.signal_key)
    mt5_pos = _Mt5Position(
        ticket=4001,
        magic=magic,
        comment="2026-06-01#02.1",
        time=1_779_978_300,
        price_open=4483.25,
        volume=0.10,
        type_=_FakeMt5.POSITION_TYPE_BUY,
    )
    mt5 = _FakeMt5(bid=4520.0, ask=4520.2, positions=[mt5_pos])
    executor = Mt5Executor(_FakeConn(mt5), "XAUUSD")

    log = executor.manage_position(pos, config, pos.activation_time + timedelta(minutes=11))

    assert log.closed == 1
    assert mt5.positions_get() == []
    assert any("TP3 catch-up closed" in action for action in log.actions)
