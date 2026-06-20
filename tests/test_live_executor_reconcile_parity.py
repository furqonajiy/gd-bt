from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta

from trading.xauusd import (
    Bar,
    DEFAULT_CONFIG,
    Mt5Executor,
    NewSignalPlan,
    PlannedOrder,
    open_position,
    parse_one_signal,
)
from trading.xauusd.execution import mt5_executor_tp2


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
    TRADE_ACTION_PENDING = 5
    TRADE_ACTION_REMOVE = 8
    TRADE_ACTION_DEAL = 1
    TRADE_ACTION_SLTP = 6
    TRADE_RETCODE_DONE = 10009
    ORDER_TYPE_BUY_LIMIT = 2
    ORDER_TYPE_SELL_LIMIT = 3
    ORDER_TYPE_BUY = 0
    ORDER_TYPE_SELL = 1
    ORDER_TIME_GTC = 0
    ORDER_FILLING_RETURN = 2
    ORDER_FILLING_IOC = 1
    ORDER_FILLING_FOK = 0
    SYMBOL_FILLING_IOC = 2
    SYMBOL_FILLING_FOK = 1
    POSITION_TYPE_BUY = 0
    POSITION_TYPE_SELL = 1

    def __init__(self, *, bid=4500.0, ask=4500.2, fail_after=0, positions=None):
        self._tick = _Tick(bid, ask)
        self._positions = list(positions or [])
        self.requests = []
        self.fail_after = fail_after
        self._pending_success_count = 0

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
        if request["action"] == self.TRADE_ACTION_PENDING:
            self._pending_success_count += 1
            if self.fail_after and self._pending_success_count > self.fail_after:
                return _Resp(retcode=10015, comment="invalid price", order=0)
            return _Resp(order=1000 + self._pending_success_count)
        if request["action"] == self.TRADE_ACTION_REMOVE:
            return _Resp(order=request.get("order", 0))
        return _Resp()

    def last_error(self):
        return (0, "ok")


class _FakeConn:
    def __init__(self, mt5):
        self.mt5 = mt5


class _Chart:
    def __init__(self, bars):
        self._bars = list(bars)

    def bars_between(self, start, end):
        return (b for b in self._bars if start <= b.time <= end)


def _bar(t, open_, high, low, close, spread=0.20):
    return Bar(
        time=t,
        open=open_,
        high=high,
        low=low,
        close=close,
        spread_points=int(round(spread / 0.01)),
        spread_price=spread,
    )


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


def _plan_for_signal(signal, config):
    pos = open_position(signal, equity=1000.0, config=config)
    orders = [
        PlannedOrder(
            entry_index=e.entry_index,
            side=signal.side,
            entry_price=e.entry_price,
            initial_sl=e.initial_sl,
            lot=e.lot,
            risk_dollars=10.0,
        )
        for e in pos.entries
    ]
    return NewSignalPlan(
        signal=signal,
        action="FOLLOW",
        rationale="test",
        orders=orders,
        pending_expires_at=pos.expiry_time,
        final_target_label="TP3",
        final_target_price=pos.target_level,
        total_initial_risk_dollars=30.0,
        replay_position=pos,
        pending_activates_at=pos.activation_time,
    )


def test_partial_broker_placement_is_rolled_back_and_not_reported_as_placed(monkeypatch):
    signal = parse_one_signal(
        "2. BUY XAUUSD 4483 - 4481 SL 4476 TP1 4491 TP2 4501 TP3 4521 11:11 AM",
        source_date="2026-06-01",
        source_offset=3,
    )
    config = _fixed_config()
    plan = _plan_for_signal(signal, config)
    monkeypatch.setattr(mt5_executor_tp2, "_wall_clock_chart_now", lambda: plan.pending_activates_at + timedelta(minutes=1))
    mt5 = _FakeMt5(bid=4490.0, ask=4490.2, fail_after=1)
    executor = Mt5Executor(_FakeConn(mt5), "XAUUSD")

    log = executor.place_signal(signal, plan)

    assert log.placed == 0
    assert log.cancelled == 1
    assert any(req["action"] == mt5.TRADE_ACTION_REMOVE for req in mt5.requests)
    assert any("partial placement rolled back" in action for action in log.actions)


def test_reconcile_maps_mt5_positions_by_entry_comment_suffix_not_fill_order():
    signal = parse_one_signal(
        "2. BUY XAUUSD 4483 - 4481 SL 4476 TP1 4491 TP2 4501 TP3 4521 11:11 AM",
        source_date="2026-06-01",
        source_offset=3,
    )
    config = _fixed_config()
    pos = open_position(signal, equity=1000.0, config=config)
    magic = mt5_executor_tp2.signal_to_magic(signal.signal_key)

    # Broker returns entry #2 first chronologically. The comment suffix must map
    # it to engine entry index 1, not to slot 0.
    p2 = _Mt5Position(
        ticket=2002,
        magic=magic,
        comment="2026-06-01#02.2",
        time=1_779_978_300,
        price_open=4480.55,
        volume=0.10,
    )
    p1 = _Mt5Position(
        ticket=2001,
        magic=magic,
        comment="2026-06-01#02.1",
        time=1_779_978_360,
        price_open=4483.35,
        volume=0.10,
    )
    mt5 = _FakeMt5(positions=[p2, p1])
    executor = Mt5Executor(_FakeConn(mt5), "XAUUSD")
    chart = _Chart([
        _bar(pos.activation_time, 4490.0, 4490.0, 4479.0, 4482.0),
        _bar(pos.activation_time + timedelta(minutes=1), 4482.0, 4485.0, 4480.0, 4481.0),
    ])

    log = executor.reconcile_with_mt5(pos, config, chart, pos.activation_time + timedelta(minutes=1))

    assert log.actions
    assert pos.entries[0].entry_price == 4483.35
    assert pos.entries[1].entry_price == 4480.55
    assert pos.entries[2].status == "PENDING"
