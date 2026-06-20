"""Parity tests between historical replay and live execution plans.

The live executor must not invent different entry, SL, TP, activation, expiry,
or sizing logic. Broker-only guards such as LIMIT side validity are tested in
separate live guard tests; this file verifies that the order payloads sent to
MT5 come from the same Position/NewSignalPlan model used by backtest replay.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta

from trading.engine import (
    Bar,
    DEFAULT_CONFIG,
    ManualPositionSource,
    Mt5Executor,
    decide,
    open_position,
    parse_one_signal,
)
from trading.engine.execution import mt5_executor_tp2


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


class _Chart:
    def __init__(self, bars: list[Bar]):
        self._bars = sorted(bars, key=lambda b: b.time)

    def first_time(self):
        return self._bars[0].time if self._bars else None

    def last_time(self):
        return self._bars[-1].time if self._bars else None

    def latest(self, at_or_before=None):
        if not self._bars:
            return None
        if at_or_before is None:
            return self._bars[-1]
        eligible = [b for b in self._bars if b.time <= at_or_before]
        return eligible[-1] if eligible else None

    def bars_between(self, start: datetime, end: datetime):
        return (b for b in self._bars if start <= b.time <= end)


class _Resp:
    def __init__(self, retcode=10009, comment="done", order=123):
        self.retcode = retcode
        self.comment = comment
        self.order = order


class _Sym:
    digits = 2
    # FOK-only market-execution broker (matches the live account), so the pending
    # placement resolves to ORDER_FILLING_FOK via _market_fill_mode().
    filling_mode = 1


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
    ORDER_FILLING_FOK = 0
    ORDER_FILLING_IOC = 1
    ORDER_FILLING_RETURN = 2
    SYMBOL_FILLING_IOC = 2
    SYMBOL_FILLING_FOK = 1

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


def _reset_executor_guards():
    Mt5Executor._session_skipped_inactive_signal_keys.clear()
    Mt5Executor._session_skipped_stale_entries.clear()
    Mt5Executor._session_skipped_expired_signal_keys.clear()
    Mt5Executor._session_failed_signal_keys.clear()


def test_live_plan_uses_same_activation_expiry_entries_stops_and_target_as_backtest_model(monkeypatch):
    _reset_executor_guards()
    signal = parse_one_signal(
        "1. BUY XAUUSD 4518 - 4516 SL 4511 TP1 4526 TP2 4536 TP3 4551 11:25 AM",
        source_date="2026-05-05",
        source_offset=7,
    )
    backtest_pos = open_position(signal, equity=1000.0, config=DD40_COMMAND_CONFIG)
    now = backtest_pos.activation_time + timedelta(minutes=1)
    chart = _Chart([
        Bar(
            time=now,
            open=4520.0,
            high=4520.5,
            low=4519.5,
            close=4520.0,
            spread_points=20,
            spread_price=0.20,
        )
    ])
    positions = ManualPositionSource(equity=1000.0, positions=[])

    rec = decide(signal, chart, positions, DD40_COMMAND_CONFIG, now=now)
    plan = rec.new_signal

    assert plan.action == "FOLLOW"
    assert plan.pending_activates_at == backtest_pos.activation_time
    assert plan.pending_expires_at == backtest_pos.expiry_time
    assert plan.final_target_price == backtest_pos.target_level
    assert [o.entry_price for o in plan.orders] == [e.entry_price for e in backtest_pos.entries]
    assert [o.initial_sl for o in plan.orders] == [e.initial_sl for e in backtest_pos.entries]
    assert [o.lot for o in plan.orders] == [e.lot for e in backtest_pos.entries]

    # BUY LIMIT entries are below Ask, so all planned orders are broker-side valid.
    _freeze_time = now
    monkeypatch.setattr(mt5_executor_tp2, "_wall_clock_chart_now", lambda: _freeze_time)
    mt5 = _FakeMt5(bid=4519.80, ask=4520.00)
    executor = Mt5Executor(_FakeConn(mt5), "XAUUSD")

    log = executor.place_signal(signal, plan)

    assert log.placed == len(backtest_pos.entries)
    assert len(mt5.requests) == len(backtest_pos.entries)
    for request, order, entry in zip(mt5.requests, plan.orders, backtest_pos.entries):
        assert request["action"] == mt5.TRADE_ACTION_PENDING
        assert request["type"] == mt5.ORDER_TYPE_BUY_LIMIT
        assert request["price"] == round(entry.entry_price, 2) == round(order.entry_price, 2)
        assert request["sl"] == round(entry.initial_sl, 2) == round(order.initial_sl, 2)
        assert request["tp"] == round(backtest_pos.target_level, 2) == round(plan.final_target_price, 2)
        assert request["volume"] == round(entry.lot, 2) == round(order.lot, 2)


def test_engine_replay_for_existing_position_matches_direct_backtest_replay():
    signal = parse_one_signal(
        "2. SELL XAUUSD 4543 - 4545 SL 4550 TP1 4535 TP2 4525 TP3 4510 11:59 AM",
        source_date="2026-05-05",
        source_offset=7,
    )
    direct = open_position(signal, equity=1000.0, config=DD40_COMMAND_CONFIG)
    chart = _Chart([
        Bar(
            time=direct.activation_time,
            open=4542.0,
            high=4544.0,
            low=4540.0,
            close=4541.0,
            spread_points=20,
            spread_price=0.20,
        ),
        Bar(
            time=direct.activation_time + timedelta(minutes=1),
            open=4544.0,
            high=4546.0,
            low=4534.0,
            close=4536.0,
            spread_points=20,
            spread_price=0.20,
        ),
    ])
    for bar in chart.bars_between(direct.activation_time, chart.last_time()):
        from trading.engine import advance_one_bar
        advance_one_bar(direct, bar, DD40_COMMAND_CONFIG)

    live_replay = open_position(signal, equity=1000.0, config=DD40_COMMAND_CONFIG)
    for bar in chart.bars_between(live_replay.activation_time, chart.last_time()):
        from trading.engine import advance_one_bar
        advance_one_bar(live_replay, bar, DD40_COMMAND_CONFIG)

    assert [(e.status, e.fill_time, e.exit_time, e.exit_price) for e in live_replay.entries] == [
        (e.status, e.fill_time, e.exit_time, e.exit_price) for e in direct.entries
    ]
    assert live_replay.stage == direct.stage
    assert live_replay.first_fill_time == direct.first_fill_time
    assert live_replay.time_exit_deadline == direct.time_exit_deadline