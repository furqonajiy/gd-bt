"""Regression tests for DD40 live-execution stop locking."""
from __future__ import annotations
from dataclasses import replace
from datetime import datetime

from xauusd_trading import DEFAULT_CONFIG, Mt5Executor, open_position, parse_one_signal, signal_to_magic


DD40_COMMAND_CONFIG = replace(
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
    lock_after_tp2=False,
    bonus_per_closed_lot=3.0,
)


class _Resp:
    def __init__(self, retcode=10009, comment="done", order=123):
        self.retcode = retcode
        self.comment = comment
        self.order = order


class _Sym:
    digits = 2
    trade_stops_level = 0
    trade_freeze_level = 0
    freeze_level = 0

    def __init__(self, *, stops=0, freeze=0):
        self.trade_stops_level = stops
        self.trade_freeze_level = freeze
        self.freeze_level = freeze


class _Tick:
    def __init__(self, bid=4500.0, ask=4500.2):
        self.bid = bid
        self.ask = ask


class _FakePosition:
    def __init__(self, *, ticket, magic, type_, sl, tp, volume=0.5, comment="", time=0):
        self.ticket = ticket
        self.magic = magic
        self.type = type_
        self.sl = sl
        self.tp = tp
        self.volume = volume
        self.comment = comment
        self.time = time


class _FakeMt5:
    TRADE_ACTION_SLTP = 6
    TRADE_ACTION_DEAL = 1
    TRADE_RETCODE_DONE = 10009
    ORDER_TYPE_SELL = 1
    ORDER_TYPE_BUY = 0
    ORDER_FILLING_RETURN = 2
    POSITION_TYPE_BUY = 0
    POSITION_TYPE_SELL = 1

    def __init__(self, positions, *, tick=None, stops=0, freeze=0):
        self._positions = list(positions)
        self._tick = tick or _Tick()
        self._sym = _Sym(stops=stops, freeze=freeze)
        self.requests = []

    def symbol_info(self, symbol):
        return self._sym

    def symbol_info_tick(self, symbol):
        return self._tick

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


def test_dd40_does_not_move_live_sl_to_tp2_when_engine_stage_is_2():
    signal = parse_one_signal(
        "1. BUY XAUUSD 4518 - 4516 SL 4511 TP1 4526 TP2 4536 TP3 4551 11:25 AM",
        source_date="2026-05-05",
        source_offset=7,
    )
    pos = open_position(signal, equity=1000.0, config=DD40_COMMAND_CONFIG)
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

    log = executor.manage_position(pos, DD40_COMMAND_CONFIG, datetime(2026, 5, 5, 7, 36))

    assert mt5_pos.sl == signal.tp1
    assert not any(req.get("action") == mt5.TRADE_ACTION_SLTP and req.get("sl") == signal.tp2 for req in mt5.requests)
    assert not any("TP2" in action for action in log.actions)


def test_tp1_lock_clamps_sl_to_broker_stops_level_before_modify():
    signal = parse_one_signal(
        "1. BUY XAUUSD 4518 - 4516 SL 4511 TP1 4526 TP2 4536 TP3 4551 11:25 AM",
        source_date="2026-05-05",
        source_offset=7,
    )
    cfg = replace(DD40_COMMAND_CONFIG, lock_after_tp1=True)
    pos = open_position(signal, equity=1000.0, config=cfg)
    pos.stage = 1
    pos.stage1_time = datetime(2026, 5, 5, 7, 30)
    pos.entries[0].status = "OPEN"
    pos.entries[0].fill_time = datetime(2026, 5, 5, 7, 27)

    mt5_pos = _FakePosition(
        ticket=888,
        magic=signal_to_magic(signal.signal_key),
        type_=_FakeMt5.POSITION_TYPE_BUY,
        sl=4510.0,
        tp=signal.tp3,
        comment=f"{signal.signal_key}.1",
    )
    mt5 = _FakeMt5([mt5_pos], tick=_Tick(bid=4526.0, ask=4526.2), stops=50)
    executor = Mt5Executor(_FakeConn(mt5), "XAUUSD")

    log = executor.manage_position(pos, cfg, datetime(2026, 5, 5, 7, 31))

    sltp_requests = [req for req in mt5.requests if req.get("action") == mt5.TRADE_ACTION_SLTP]
    assert len(sltp_requests) == 1
    assert sltp_requests[0]["sl"] == 4525.5
    assert mt5_pos.sl == 4525.5
    assert any("Clamped TP1 SL" in action and "requested 4526" in action and "4525.5" in action for action in log.actions)
    assert any("Locked SL on #888 to TP1 4525.5" in action for action in log.actions)
