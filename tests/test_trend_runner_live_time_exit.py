"""Regression tests for live trend-runner max-hold and stop parity."""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta

from trading.xauusd import DEFAULT_CONFIG, ExecutionLog, Mt5Executor, open_position, parse_one_signal, signal_to_magic


class _Resp:
    def __init__(self, retcode=10009, comment="done", order=123):
        self.retcode = retcode
        self.comment = comment
        self.order = order


class _Sym:
    digits = 2
    filling_mode = 0

    def __init__(self, *, stops=0, freeze=0):
        self.trade_stops_level = stops
        self.trade_freeze_level = freeze
        self.freeze_level = freeze


class _Tick:
    def __init__(self, bid=4520.0, ask=4520.2):
        self.bid = bid
        self.ask = ask


class _FakeOrder:
    def __init__(self, *, ticket, magic):
        self.ticket = ticket
        self.magic = magic


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
    TRADE_ACTION_DEAL = 1
    TRADE_ACTION_REMOVE = 3
    TRADE_ACTION_SLTP = 6
    TRADE_RETCODE_DONE = 10009

    ORDER_TYPE_BUY = 0
    ORDER_TYPE_SELL = 1
    ORDER_FILLING_FOK = 0
    ORDER_FILLING_IOC = 1
    ORDER_FILLING_RETURN = 2

    POSITION_TYPE_BUY = 0
    POSITION_TYPE_SELL = 1

    def __init__(self, *, positions=None, orders=None, tick=None, stops=0, freeze=0):
        self._positions = list(positions or [])
        self._orders = list(orders or [])
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
        return list(self._orders)

    def order_send(self, request):
        self.requests.append(dict(request))
        if request.get("action") == self.TRADE_ACTION_DEAL:
            self._positions = [p for p in self._positions if p.ticket != request.get("position")]
        elif request.get("action") == self.TRADE_ACTION_REMOVE:
            self._orders = [o for o in self._orders if o.ticket != request.get("order")]
        elif request.get("action") == self.TRADE_ACTION_SLTP:
            for p in self._positions:
                if p.ticket == request.get("position"):
                    p.sl = request["sl"]
                    p.tp = request["tp"]
                    break
        return _Resp()

    def last_error(self):
        return (0, "ok")


class _FakeConn:
    def __init__(self, mt5):
        self.mt5 = mt5


RUNNER_CONFIG = replace(
    DEFAULT_CONFIG,
    trend_runner_enabled=True,
    trend_runner_override_max_hold=True,
    trailing_close_distance=0.0,
    lock_after_tp1=False,
    lock_after_tp2=False,
)


def _past_deadline_position(*, runner_active: bool):
    signal = parse_one_signal(
        "1. BUY XAUUSD 4518 - 4516 SL 4511 TP1 4526 TP2 4536 TP3 4551 8:00 AM",
        source_date="2026-06-02",
        source_offset=7,
    )
    pos = open_position(signal, equity=1000.0, config=RUNNER_CONFIG)
    fill_time = datetime(2026, 6, 2, 4, 3)
    pos.entries[0].status = "OPEN"
    pos.entries[0].fill_time = fill_time
    pos.first_fill_time = fill_time
    pos.time_exit_deadline = fill_time + timedelta(minutes=RUNNER_CONFIG.max_hold_minutes)

    # Keep expiry out of this regression so only the timeout block can cancel the
    # pending order. Wall-clock expiry is covered separately.
    pos.expiry_time = datetime(2099, 1, 1)

    if runner_active:
        pos.trend_runner_active = True
    return signal, pos


def test_runner_active_position_past_deadline_is_not_time_closed_or_timeout_cancelled():
    signal, pos = _past_deadline_position(runner_active=True)
    magic = signal_to_magic(signal.signal_key)
    mt5_pos = _FakePosition(
        ticket=1001,
        magic=magic,
        type_=_FakeMt5.POSITION_TYPE_BUY,
        sl=4511.0,
        tp=signal.tp3,
        comment=f"{signal.signal_key}.1",
    )
    mt5_order = _FakeOrder(ticket=2001, magic=magic)
    mt5 = _FakeMt5(positions=[mt5_pos], orders=[mt5_order])
    executor = Mt5Executor(_FakeConn(mt5), "XAUUSD")

    log = executor.manage_position(pos, RUNNER_CONFIG, datetime(2026, 6, 2, 6, 0))

    assert log.closed == 0
    assert not any(req.get("action") == mt5.TRADE_ACTION_DEAL for req in mt5.requests)
    assert not any(req.get("action") == mt5.TRADE_ACTION_REMOVE for req in mt5.requests)
    assert mt5.positions_get(symbol="XAUUSD") == [mt5_pos]
    assert mt5.orders_get(symbol="XAUUSD") == [mt5_order]


def test_non_runner_position_past_deadline_is_still_time_closed_and_timeout_cancelled():
    signal, pos = _past_deadline_position(runner_active=False)
    magic = signal_to_magic(signal.signal_key)
    mt5_pos = _FakePosition(
        ticket=1002,
        magic=magic,
        type_=_FakeMt5.POSITION_TYPE_BUY,
        sl=4511.0,
        tp=signal.tp3,
        comment=f"{signal.signal_key}.1",
    )
    mt5_order = _FakeOrder(ticket=2002, magic=magic)
    mt5 = _FakeMt5(positions=[mt5_pos], orders=[mt5_order])
    executor = Mt5Executor(_FakeConn(mt5), "XAUUSD")

    log = executor.manage_position(pos, RUNNER_CONFIG, datetime(2026, 6, 2, 6, 0))

    assert log.closed == 1
    assert any(req.get("action") == mt5.TRADE_ACTION_DEAL and req.get("position") == 1002 for req in mt5.requests)
    assert any(req.get("action") == mt5.TRADE_ACTION_REMOVE and req.get("order") == 2002 for req in mt5.requests)
    assert mt5.positions_get(symbol="XAUUSD") == []
    assert mt5.orders_get(symbol="XAUUSD") == []


def test_runner_active_pushes_atr_trailing_stop_even_when_fixed_trailing_close_is_disabled():
    signal, pos = _past_deadline_position(runner_active=True)
    pos.time_exit_deadline = datetime(2099, 1, 1)
    pos.stage = 3
    pos.stage3_time = pos.entries[0].fill_time + timedelta(minutes=5)
    pos.entries[0].trailing_stop = 4542.0

    expected_sl = round(pos.effective_stop_for(pos.entries[0], RUNNER_CONFIG), 2)
    assert expected_sl == 4542.0

    magic = signal_to_magic(signal.signal_key)
    mt5_pos = _FakePosition(
        ticket=1003,
        magic=magic,
        type_=_FakeMt5.POSITION_TYPE_BUY,
        sl=signal.tp1,
        tp=signal.tp3,
        comment=f"{signal.signal_key}.1",
    )
    mt5 = _FakeMt5(positions=[mt5_pos], tick=_Tick(bid=4550.0, ask=4550.2))
    executor = Mt5Executor(_FakeConn(mt5), "XAUUSD")

    log = executor.manage_position(pos, RUNNER_CONFIG, datetime(2026, 6, 2, 5, 0))

    sltp_requests = [req for req in mt5.requests if req.get("action") == mt5.TRADE_ACTION_SLTP]
    assert len(sltp_requests) == 1
    assert sltp_requests[0]["sl"] == expected_sl
    assert mt5_pos.sl == expected_sl
    assert log.modified == 1


def test_clamped_stop_does_not_trigger_external_sl_warning_but_unrelated_stop_does():
    signal, pos = _past_deadline_position(runner_active=True)
    pos.time_exit_deadline = datetime(2099, 1, 1)
    pos.stage = 3
    pos.stage3_time = pos.entries[0].fill_time + timedelta(minutes=5)
    pos.entries[0].trailing_stop = 4549.0

    raw_expected = round(pos.effective_stop_for(pos.entries[0], RUNNER_CONFIG), 2)
    assert raw_expected == 4549.0

    magic = signal_to_magic(signal.signal_key)
    mt5_pos = _FakePosition(
        ticket=1004,
        magic=magic,
        type_=_FakeMt5.POSITION_TYPE_BUY,
        sl=4540.0,
        tp=signal.tp3,
        comment=f"{signal.signal_key}.1",
    )
    mt5 = _FakeMt5(
        positions=[mt5_pos],
        tick=_Tick(bid=4550.0, ask=4550.2),
        stops=200,  # $2.00 minimum stop distance, so BUY SL 4549 clamps to 4548.
    )
    executor = Mt5Executor(_FakeConn(mt5), "XAUUSD")

    first = executor.manage_position(pos, RUNNER_CONFIG, datetime(2026, 6, 2, 5, 0))
    assert first.modified == 1
    assert mt5_pos.sl == 4548.0

    quiet_log = ExecutionLog()
    executor._warn_on_external_sl_change(pos, RUNNER_CONFIG, quiet_log)
    assert not any("external SL change detected" in warning for warning in quiet_log.warnings)

    mt5_pos.sl = 4547.0
    warning_log = ExecutionLog()
    executor._warn_on_external_sl_change(pos, RUNNER_CONFIG, warning_log)
    assert any("external SL change detected" in warning for warning in warning_log.warnings)
