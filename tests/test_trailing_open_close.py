from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from trading.engine import Bar, DEFAULT_CONFIG, Mt5Executor, NewSignalPlan, PlannedOrder
from trading.engine import advance_bars, open_position, parse_one_signal, signal_to_magic
from trading.engine.execution import mt5_executor_live, mt5_executor_trailing


@pytest.fixture(autouse=True)
def _reset_trailing_dedup():
    """Each test is a fresh 'session'. The executor's once-per-session log dedup
    uses process-local class-level sets (so live ``auto`` doesn't re-log the same
    decision every cycle); clear them between tests so a once-only line isn't
    suppressed by a prior test that parsed the same signal_key."""
    for name in ("_session_skipped_partial_signal_keys",
                 "_session_trailing_open_waiting_keys",
                 "_session_skipped_inactive_signal_keys",
                 "_session_skipped_expired_signal_keys"):
        getattr(Mt5Executor, name, set()).clear()
    yield


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


class _FakeMt5:
    TRADE_ACTION_PENDING = 5
    TRADE_ACTION_MODIFY = 6
    TRADE_ACTION_SLTP = 7
    TRADE_ACTION_DEAL = 1
    TRADE_RETCODE_DONE = 10009
    ORDER_TYPE_BUY_LIMIT = 2
    ORDER_TYPE_SELL_LIMIT = 3
    ORDER_TYPE_BUY_STOP = 4
    ORDER_TYPE_SELL_STOP = 5
    ORDER_TYPE_BUY = 0
    ORDER_TYPE_SELL = 1
    POSITION_TYPE_BUY = 0
    POSITION_TYPE_SELL = 1
    ORDER_TIME_GTC = 0
    ORDER_FILLING_RETURN = 2
    ORDER_FILLING_IOC = 1
    SYMBOL_FILLING_IOC = 2
    SYMBOL_FILLING_FOK = 1
    DEAL_ENTRY_IN = 0
    DEAL_ENTRY_OUT = 1
    DEAL_REASON_CLIENT = 0
    DEAL_REASON_MOBILE = 1
    DEAL_REASON_WEB = 2
    DEAL_REASON_EXPERT = 3
    DEAL_REASON_SL = 4
    DEAL_REASON_TP = 5

    def __init__(self, *, bid: float, ask: float, history_deals=None):
        self._tick = _Tick(bid, ask)
        self.requests = []
        self._orders = []
        self._positions = []
        self._history = list(history_deals or [])

    def symbol_info(self, symbol):
        return _Sym()

    def symbol_info_tick(self, symbol):
        return self._tick

    def positions_get(self, symbol=None):
        return list(self._positions)

    def orders_get(self, symbol=None):
        return list(self._orders)

    def history_deals_get(self, date_from, date_to):
        return list(self._history)

    def order_send(self, request):
        self.requests.append(dict(request))
        return _Resp(order=1000 + len(self.requests))

    def last_error(self):
        return (0, "ok")


class _FakeConn:
    def __init__(self, mt5):
        self.mt5 = mt5


def _bar(t: datetime, o: float, h: float, l: float, c: float, spread: float = 0.0) -> Bar:
    return Bar(t, o, h, l, c, int(round(spread / 0.01)), spread)


def test_trailing_open_does_not_fill_buy_limit_while_price_keeps_dropping():
    sig = parse_one_signal(
        "1. BUY XAUUSD 4750 - 4748 SL 4744 TP1 4760 TP2 4770 TP3 4780 10:00 AM",
        source_date="2026-06-01",
        source_offset=3,
    )
    cfg = replace(
        DEFAULT_CONFIG,
        entry_count=1,
        trailing_open_distance=2.0,
        activation_delay_minutes=0,
    )
    pos = open_position(sig, 1000.0, cfg)
    t = sig.signal_time_chart

    advance_bars(pos, [
        _bar(t, 4755, 4755, 4749, 4750),
        _bar(t + timedelta(minutes=1), 4750, 4751, 4740, 4741),
    ], cfg)

    assert pos.entries[0].status == "PENDING"
    assert pos.entries[0].fill_time is None
    assert pos.entries[0].trailing_open_extreme == 4740


def test_trailing_open_fills_buy_after_rebound_from_low():
    sig = parse_one_signal(
        "1. BUY XAUUSD 4750 - 4748 SL 4744 TP1 4760 TP2 4770 TP3 4780 10:00 AM",
        source_date="2026-06-01",
        source_offset=3,
    )
    cfg = replace(
        DEFAULT_CONFIG,
        entry_count=1,
        trailing_open_distance=2.0,
        activation_delay_minutes=0,
    )
    pos = open_position(sig, 1000.0, cfg)
    t = sig.signal_time_chart

    advance_bars(pos, [
        _bar(t, 4755, 4755, 4740, 4741),
        _bar(t + timedelta(minutes=1), 4741, 4742, 4740, 4742),
    ], cfg)

    assert pos.entries[0].status == "OPEN"
    assert pos.entries[0].fill_time == t + timedelta(minutes=1)
    assert pos.entries[0].entry_price == 4742
    assert pos.entries[0].initial_sl == 4742 - pos.base_stop_distance


def test_trailing_open_two_buy_entries_arm_from_each_entry_minus_distance():
    sig = parse_one_signal(
        "1. BUY XAUUSD 4750 - 4748 SL 4744 TP1 4760 TP2 4770 TP3 4780 10:00 AM",
        source_date="2026-06-01",
        source_offset=3,
    )
    cfg = replace(
        DEFAULT_CONFIG,
        entry_count=2,
        entry_ladder="range_to_sl",
        entry_sl_gap=2.0,
        trailing_open_distance=2.0,
        activation_delay_minutes=0,
    )
    pos = open_position(sig, 1000.0, cfg)
    t = sig.signal_time_chart

    assert [e.entry_price for e in pos.entries] == [4750.0, 4746.0]

    advance_bars(pos, [
        _bar(t, 4755, 4755, 4748, 4752),
    ], cfg)

    assert pos.entries[0].status == "PENDING"
    assert pos.entries[0].trailing_open_touched_at == t
    assert pos.entries[0].trailing_open_extreme == 4748
    assert pos.entries[1].status == "PENDING"
    assert pos.entries[1].trailing_open_touched_at is None
    assert pos.entries[1].trailing_open_extreme is None


def test_trailing_open_two_buy_entries_can_fill_at_different_prices_and_times():
    sig = parse_one_signal(
        "1. BUY XAUUSD 4750 - 4748 SL 4744 TP1 4760 TP2 4770 TP3 4780 10:00 AM",
        source_date="2026-06-01",
        source_offset=3,
    )
    cfg = replace(
        DEFAULT_CONFIG,
        entry_count=2,
        entry_ladder="range_to_sl",
        entry_sl_gap=2.0,
        trailing_open_distance=2.0,
        activation_delay_minutes=0,
    )
    pos = open_position(sig, 1000.0, cfg)
    t = sig.signal_time_chart

    advance_bars(pos, [
        _bar(t, 4755, 4755, 4748, 4750),
        _bar(t + timedelta(minutes=1), 4750, 4750, 4748, 4750),
        _bar(t + timedelta(minutes=2), 4750, 4750, 4744, 4745),
        _bar(t + timedelta(minutes=3), 4745, 4746, 4744, 4746),
    ], cfg)

    first, second = pos.entries
    assert first.status == "OPEN"
    assert first.fill_time == t + timedelta(minutes=1)
    assert first.entry_price == 4750
    assert first.initial_sl == 4750 - pos.base_stop_distance
    assert second.status == "OPEN"
    assert second.fill_time == t + timedelta(minutes=3)
    assert second.entry_price == 4746
    assert second.initial_sl == 4746 - pos.base_stop_distance


def test_trailing_open_ladder_keeps_deeper_entry_no_fill_on_shallow_bounce():
    sig = parse_one_signal(
        "1. BUY XAUUSD 4750 - 4748 SL 4744 TP1 4760 TP2 4770 TP3 4780 10:00 AM",
        source_date="2026-06-01",
        source_offset=3,
    )
    cfg = replace(
        DEFAULT_CONFIG,
        entry_count=2,
        entry_ladder="range_to_sl",
        entry_sl_gap=2.0,
        trailing_open_distance=2.0,
        activation_delay_minutes=0,
    )
    pos = open_position(sig, 1000.0, cfg)
    t = sig.signal_time_chart

    advance_bars(pos, [
        _bar(t, 4755, 4755, 4748, 4750),
        _bar(t + timedelta(minutes=1), 4750, 4750, 4748, 4750),
        _bar(t + timedelta(minutes=2), 4750, 4750, 4744, 4745),
        _bar(t + timedelta(minutes=3), 4745, 4745, 4744, 4745),
        _bar(t + timedelta(minutes=91), 4745, 4745, 4745, 4745),
        _bar(t + timedelta(minutes=631), 4745, 4745, 4745, 4745),
    ], cfg)

    first, second = pos.entries
    assert first.status == "TIME_EXIT"
    assert first.fill_time == t + timedelta(minutes=1)
    assert first.entry_price == 4750
    assert second.status == "NO_FILL"
    assert second.fill_time is None
    assert second.trailing_open_touched_at == t + timedelta(minutes=2)
    assert second.trailing_open_extreme == 4744


def test_trailing_open_two_buy_entries_do_not_fill_on_arming_bar():
    sig = parse_one_signal(
        "1. BUY XAUUSD 4750 - 4748 SL 4744 TP1 4760 TP2 4770 TP3 4780 10:00 AM",
        source_date="2026-06-01",
        source_offset=3,
    )
    cfg = replace(
        DEFAULT_CONFIG,
        entry_count=2,
        entry_ladder="range_to_sl",
        entry_sl_gap=2.0,
        trailing_open_distance=2.0,
        activation_delay_minutes=0,
    )
    pos = open_position(sig, 1000.0, cfg)
    t = sig.signal_time_chart

    advance_bars(pos, [
        _bar(t, 4755, 4755, 4744, 4755),
    ], cfg)

    assert [e.status for e in pos.entries] == ["PENDING", "PENDING"]
    assert [e.trailing_open_touched_at for e in pos.entries] == [t, t]
    assert [e.trailing_open_extreme for e in pos.entries] == [4744, 4744]

    advance_bars(pos, [
        _bar(t + timedelta(minutes=1), 4755, 4755, 4744, 4755),
    ], cfg)

    assert [e.status for e in pos.entries] == ["OPEN", "OPEN"]
    assert [e.entry_price for e in pos.entries] == [4746, 4746]
    assert [e.fill_time for e in pos.entries] == [t + timedelta(minutes=1), t + timedelta(minutes=1)]


def test_trailing_close_advances_stop_and_can_close_later():
    sig = parse_one_signal(
        "1. BUY XAUUSD 4750 - 4748 SL 4744 TP1 4760 TP2 4770 TP3 4780 10:00 AM",
        source_date="2026-06-01",
        source_offset=3,
    )
    cfg = replace(
        DEFAULT_CONFIG,
        entry_count=1,
        trailing_close_distance=3.0,
        activation_delay_minutes=0,
    )
    pos = open_position(sig, 1000.0, cfg)
    t = sig.signal_time_chart

    advance_bars(pos, [
        _bar(t, 4752, 4752, 4749, 4751),
        _bar(t + timedelta(minutes=1), 4751, 4757, 4751, 4756),
        _bar(t + timedelta(minutes=2), 4756, 4756, 4754, 4754),
    ], cfg)

    assert pos.entries[0].status == "TRAILING_STOP"
    assert pos.entries[0].exit_price == 4754
    assert pos.entries[0].stop_at_exit == 4754


def test_live_executor_places_buy_stop_not_buy_limit_when_trailing_open_enabled(monkeypatch):
    signal = parse_one_signal(
        "1. BUY XAUUSD 4750 - 4748 SL 4744 TP1 4760 TP2 4770 TP3 4780 10:00 AM",
        source_date="2026-06-01",
        source_offset=3,
    )
    activation = signal.signal_time_chart
    monkeypatch.setattr(mt5_executor_trailing, "_wall_clock_chart_now", lambda: activation + timedelta(minutes=1))
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

    mt5 = _FakeMt5(bid=4740.0, ask=4740.2)
    executor = Mt5Executor(_FakeConn(mt5), "XAUUSD")

    log = executor.place_signal(signal, plan)

    assert log.placed == 1
    assert len(mt5.requests) == 1
    request = mt5.requests[0]
    assert request["type"] == mt5.ORDER_TYPE_BUY_STOP
    assert request["price"] == 4742.2
    assert request["price"] != 4750.0
    assert request["sl"] == 4732.54
    assert any("placed trailing-open STOP" in action for action in log.actions)


class _FakeDeal:
    def __init__(self, magic, *, entry, reason):
        self.magic = magic
        self.entry = entry
        self.reason = reason


def _trailing_plan(signal):
    activation = signal.signal_time_chart
    return NewSignalPlan(
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


def test_trailing_place_gated_by_system_close(monkeypatch):
    """A magic the broker/engine FINISHED (SL/TP hit, stop-out, engine close) is
    'already traded' -- the trailing-open is NOT re-placed, so a stopped-out
    signal never churns back in."""
    signal = parse_one_signal(
        "1. BUY XAUUSD 4750 - 4748 SL 4744 TP1 4760 TP2 4770 TP3 4780 10:00 AM",
        source_date="2026-06-01", source_offset=3,
    )
    activation = signal.signal_time_chart
    monkeypatch.setattr(mt5_executor_trailing, "_wall_clock_chart_now",
                        lambda: activation + timedelta(minutes=1))
    magic = signal_to_magic(signal.signal_key)
    mt5 = _FakeMt5(bid=4740.0, ask=4740.2, history_deals=[
        _FakeDeal(magic, entry=_FakeMt5.DEAL_ENTRY_IN, reason=_FakeMt5.DEAL_REASON_EXPERT),
        _FakeDeal(magic, entry=_FakeMt5.DEAL_ENTRY_OUT, reason=_FakeMt5.DEAL_REASON_SL),
    ])
    executor = Mt5Executor(_FakeConn(mt5), "XAUUSD")

    log = executor.place_signal(signal, _trailing_plan(signal))

    assert log.placed == 0
    assert mt5.requests == []
    assert any("already traded" in a and "SL/TP/engine close" in a for a in log.actions)


def test_trailing_place_rearms_after_manual_close(monkeypatch):
    """A leg the OPERATOR closed BY HAND (deal reason CLIENT/MOBILE/WEB) does NOT
    gate: the trailing-open STOP is re-armed so it can re-enter on the next
    pullback (operator rule 2026-06-26)."""
    signal = parse_one_signal(
        "1. BUY XAUUSD 4750 - 4748 SL 4744 TP1 4760 TP2 4770 TP3 4780 10:00 AM",
        source_date="2026-06-01", source_offset=3,
    )
    activation = signal.signal_time_chart
    monkeypatch.setattr(mt5_executor_trailing, "_wall_clock_chart_now",
                        lambda: activation + timedelta(minutes=1))
    magic = signal_to_magic(signal.signal_key)
    mt5 = _FakeMt5(bid=4740.0, ask=4740.2, history_deals=[
        _FakeDeal(magic, entry=_FakeMt5.DEAL_ENTRY_IN, reason=_FakeMt5.DEAL_REASON_EXPERT),
        _FakeDeal(magic, entry=_FakeMt5.DEAL_ENTRY_OUT, reason=_FakeMt5.DEAL_REASON_CLIENT),
    ])
    executor = Mt5Executor(_FakeConn(mt5), "XAUUSD")

    log = executor.place_signal(signal, _trailing_plan(signal))

    assert log.placed == 1
    assert len(mt5.requests) == 1
    assert mt5.requests[0]["type"] == mt5.ORDER_TYPE_BUY_STOP   # re-armed, not gated
    assert not any("already traded" in a for a in log.actions)


def test_live_executor_waits_when_price_has_not_moved_far_enough_for_trailing_open(monkeypatch):
    signal = parse_one_signal(
        "1. BUY XAUUSD 4750 - 4748 SL 4744 TP1 4760 TP2 4770 TP3 4780 10:00 AM",
        source_date="2026-06-01",
        source_offset=3,
    )
    activation = signal.signal_time_chart
    monkeypatch.setattr(mt5_executor_trailing, "_wall_clock_chart_now", lambda: activation + timedelta(minutes=1))
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

    mt5 = _FakeMt5(bid=4749.5, ask=4749.8)
    executor = Mt5Executor(_FakeConn(mt5), "XAUUSD")

    log = executor.place_signal(signal, plan)

    assert log.placed == 0
    assert mt5.requests == []
    waiting_line = next(a for a in log.actions if "trailing-open waiting" in a)
    # Names the arm threshold and STOP direction; the old wording said "LIMIT".
    assert "arms when Ask<=4748" in waiting_line
    assert "planned 4750-2" in waiting_line
    assert "BUY STOP" in waiting_line
    assert "rebound" in waiting_line
    assert "#1" in waiting_line
    assert "LIMIT" not in waiting_line


def test_live_executor_waiting_line_names_sell_stop_and_pullback(monkeypatch):
    signal = parse_one_signal(
        "1. SELL XAUUSD 4750 - 4752 SL 4756 TP1 4742 TP2 4732 TP3 4722 10:00 AM",
        source_date="2026-06-01",
        source_offset=3,
    )
    activation = signal.signal_time_chart
    monkeypatch.setattr(mt5_executor_trailing, "_wall_clock_chart_now", lambda: activation + timedelta(minutes=1))
    plan = NewSignalPlan(
        signal=signal,
        action="FOLLOW",
        rationale="test",
        orders=[PlannedOrder(0, signal.side, 4750.0, 4756.0, 0.10, 96.6)],
        pending_expires_at=activation + timedelta(minutes=630),
        final_target_label="TP3",
        final_target_price=4722.0,
        total_initial_risk_dollars=96.6,
        pending_activates_at=activation,
        trailing_open_distance=2.0,
    )

    # Bid 4750.5 < planned 4750 + 2 -> still waiting to arm.
    mt5 = _FakeMt5(bid=4750.5, ask=4750.8)
    executor = Mt5Executor(_FakeConn(mt5), "XAUUSD")

    log = executor.place_signal(signal, plan)

    assert log.placed == 0
    waiting_line = next(a for a in log.actions if "trailing-open waiting" in a)
    assert "arms when Bid>=4752" in waiting_line
    assert "planned 4750+2" in waiting_line
    assert "SELL STOP" in waiting_line
    assert "pullback" in waiting_line
    assert "LIMIT" not in waiting_line


def test_trailing_open_waiting_line_is_multiline_for_multiple_entries(monkeypatch):
    signal = parse_one_signal(
        "1. BUY XAUUSD 4750 - 4746 SL 4742 TP1 4760 TP2 4770 TP3 4780 10:00 AM",
        source_date="2026-06-01",
        source_offset=3,
    )
    activation = signal.signal_time_chart
    monkeypatch.setattr(mt5_executor_trailing, "_wall_clock_chart_now", lambda: activation + timedelta(minutes=1))
    plan = NewSignalPlan(
        signal=signal,
        action="FOLLOW",
        rationale="test",
        orders=[
            PlannedOrder(0, signal.side, 4750.0, 4742.0, 0.10, 96.6),
            PlannedOrder(1, signal.side, 4749.0, 4741.0, 0.10, 96.6),
            PlannedOrder(2, signal.side, 4748.0, 4740.0, 0.10, 96.6),
        ],
        pending_expires_at=activation + timedelta(minutes=630),
        final_target_label="TP3",
        final_target_price=4780.0,
        total_initial_risk_dollars=96.6,
        pending_activates_at=activation,
        trailing_open_distance=2.0,
    )

    # Ask above every entry's arm threshold (planned - 2) -> all four waiting.
    mt5 = _FakeMt5(bid=4751.0, ask=4751.2)
    executor = Mt5Executor(_FakeConn(mt5), "XAUUSD")

    log = executor.place_signal(signal, plan)

    assert log.placed == 0
    waiting = next(a for a in log.actions if "trailing-open waiting" in a)
    lines = waiting.split("\n")
    assert len(lines) == 4  # 1 header + 3 per-entry lines
    assert lines[0].startswith("Signal") and "BUY STOP" in lines[0] and "rebound" in lines[0]
    assert lines[1].strip().startswith("#1 arms when Ask<=4748")
    assert lines[2].strip().startswith("#2 arms when Ask<=4747")
    assert lines[3].strip().startswith("#3 arms when Ask<=4746")
    assert "LIMIT" not in waiting


def test_trailing_open_waiting_line_logs_once_across_cycles(monkeypatch):
    """auto rebuilds the executor every watch interval; the 'waiting' block must
    log once per session, not every cycle (mirrors the non-trailing dedup)."""
    signal = parse_one_signal(
        "1. BUY XAUUSD 4750 - 4746 SL 4742 TP1 4760 TP2 4770 TP3 4780 10:00 AM",
        source_date="2026-06-01",
        source_offset=3,
    )
    activation = signal.signal_time_chart
    monkeypatch.setattr(mt5_executor_trailing, "_wall_clock_chart_now",
                        lambda: activation + timedelta(minutes=1))
    plan = NewSignalPlan(
        signal=signal,
        action="FOLLOW",
        rationale="test",
        orders=[PlannedOrder(0, signal.side, 4750.0, 4742.0, 0.10, 96.6)],
        pending_expires_at=activation + timedelta(minutes=630),
        final_target_label="TP3",
        final_target_price=4780.0,
        total_initial_risk_dollars=96.6,
        pending_activates_at=activation,
        trailing_open_distance=2.0,
    )
    mt5 = _FakeMt5(bid=4751.0, ask=4751.2)  # above arm threshold -> waiting

    # Each cycle builds a FRESH executor (as auto does); the class-level dedup
    # set persists across them, so only the first cycle logs the waiting block.
    log1 = Mt5Executor(_FakeConn(mt5), "XAUUSD").place_signal(signal, plan)
    log2 = Mt5Executor(_FakeConn(mt5), "XAUUSD").place_signal(signal, plan)

    assert any("trailing-open waiting" in a for a in log1.actions)
    assert not any("trailing-open waiting" in a for a in log2.actions)
    assert log1.placed == 0 and log2.placed == 0

def _partial_trailing_plan(signal, activation):
    """A trailing-open plan whose replay holds 2 entries but only entry #0 is
    still PENDING (the other already played out) -> a partial ladder."""
    return NewSignalPlan(
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
        replay_position=SimpleNamespace(entries=[object(), object()]),
    )


def test_trailing_open_partial_ladder_skipped_without_reopen(monkeypatch):
    """Default (no --reopen-missing-positions): a partial trailing ladder is
    skipped wholesale, as the live registry is signal-level."""
    signal = parse_one_signal(
        "1. BUY XAUUSD 4750 - 4748 SL 4744 TP1 4760 TP2 4770 TP3 4780 10:00 AM",
        source_date="2026-06-01", source_offset=3,
    )
    activation = signal.signal_time_chart
    monkeypatch.setattr(mt5_executor_trailing, "_wall_clock_chart_now",
                        lambda: activation + timedelta(minutes=1))
    mt5 = _FakeMt5(bid=4740.0, ask=4740.2)  # armed, but should still be skipped
    executor = Mt5Executor(_FakeConn(mt5), "XAUUSD")

    log = executor.place_signal(signal, _partial_trailing_plan(signal, activation))

    assert log.placed == 0
    assert len(mt5.requests) == 0
    assert any("skipped partial placement" in a for a in log.actions)


def test_trailing_open_partial_ladder_placed_under_reopen(monkeypatch):
    """With --reopen-missing-positions, the still-PENDING trailing-open legs are
    placed (entry-level), matching the backtest replay and the non-trailing path."""
    signal = parse_one_signal(
        "1. BUY XAUUSD 4750 - 4748 SL 4744 TP1 4760 TP2 4770 TP3 4780 10:00 AM",
        source_date="2026-06-01", source_offset=3,
    )
    activation = signal.signal_time_chart
    monkeypatch.setattr(mt5_executor_trailing, "_wall_clock_chart_now",
                        lambda: activation + timedelta(minutes=1))
    mt5 = _FakeMt5(bid=4740.0, ask=4740.2)  # armed -> BUY STOP placeable
    executor = Mt5Executor(_FakeConn(mt5), "XAUUSD")
    executor._allow_partial_placement = True  # set by --reopen-missing-positions

    log = executor.place_signal(signal, _partial_trailing_plan(signal, activation))

    assert log.placed == 1
    assert len(mt5.requests) == 1
    assert mt5.requests[0]["type"] == mt5.ORDER_TYPE_BUY_STOP
    assert not any("skipped partial placement" in a for a in log.actions)


def _reopen_pos(signal, *, distance=2.0):
    """Engine position whose single leg the replay holds OPEN -- used to drive
    the trailing-aware reopen. The leg is 'missing' from MT5 (positions/orders
    empty), so reopen must restore it."""
    cfg = replace(
        DEFAULT_CONFIG,
        entry_count=1,
        trailing_open_distance=distance,
        activation_delay_minutes=0,
    )
    pos = open_position(signal, 1000.0, cfg)
    pos.entries[0].status = "OPEN"
    pos.entries[0].fill_time = pos.activation_time
    pos.time_exit_deadline = None  # don't let the time-exit cycle claim the leg
    return pos, cfg


def test_trailing_reopen_rearms_buy_stop_not_buy_limit(monkeypatch):
    """INVARIANT: a trailing-open strategy NEVER reopens with a LIMIT. When the
    replay still holds a leg OPEN but it is missing from MT5 (e.g. hand-closed),
    reopen re-arms the trailing-open STOP at the original levels and waits for the
    rebound -- exactly like the first entry -- instead of resting a flat BUY LIMIT
    that gives a worse basis (the 2026-06-25 TOC5 give-back)."""
    signal = parse_one_signal(
        "1. BUY XAUUSD 4750 - 4748 SL 4744 TP1 4760 TP2 4770 TP3 4780 10:00 AM",
        source_date="2026-06-01", source_offset=3,
    )
    pos, cfg = _reopen_pos(signal)
    activation = signal.signal_time_chart
    # _reopen_candidate_legs reads the wall clock from the live module.
    monkeypatch.setattr(mt5_executor_live, "_wall_clock_chart_now",
                        lambda: activation + timedelta(minutes=1))
    mt5 = _FakeMt5(bid=4740.0, ask=4740.2)  # pulled back -> trailing-open armed
    executor = Mt5Executor(_FakeConn(mt5), "XAUUSD")

    log = executor.reopen_missing_open_positions(pos, cfg)

    assert log.placed == 1
    assert len(mt5.requests) == 1
    req = mt5.requests[0]
    assert req["action"] == mt5.TRADE_ACTION_PENDING
    assert req["type"] == mt5.ORDER_TYPE_BUY_STOP          # re-armed STOP
    assert req["type"] != mt5.ORDER_TYPE_BUY_LIMIT         # never a flat LIMIT
    assert req["price"] == 4742.2                          # rebound trigger = ask + distance
    assert any("Re-armed trailing-open" in a for a in log.actions)


def test_trailing_reopen_falls_back_to_base_limit_when_distance_zero(monkeypatch):
    """trailing_open_distance == 0 -> the base LIMIT/market reopen still applies
    (no trailing arm), so the gate is purely on the distance."""
    signal = parse_one_signal(
        "1. BUY XAUUSD 4750 - 4748 SL 4744 TP1 4760 TP2 4770 TP3 4780 10:00 AM",
        source_date="2026-06-01", source_offset=3,
    )
    pos, _ = _reopen_pos(signal, distance=2.0)
    cfg0 = replace(DEFAULT_CONFIG, entry_count=1, trailing_open_distance=0.0)
    activation = signal.signal_time_chart
    monkeypatch.setattr(mt5_executor_live, "_wall_clock_chart_now",
                        lambda: activation + timedelta(minutes=1))
    # Favorable price (ask <= entry) -> base reopens at market, no trailing STOP.
    mt5 = _FakeMt5(bid=4749.8, ask=4750.0)
    executor = Mt5Executor(_FakeConn(mt5), "XAUUSD")

    log = executor.reopen_missing_open_positions(pos, cfg0)

    assert not any("Re-armed trailing-open" in a for a in log.actions)
    assert all(r["type"] not in (mt5.ORDER_TYPE_BUY_STOP,) for r in mt5.requests)


# ---------------------------------------------------------------------------
# Partial trailing-open arming (V017 2026-07-01 fix)
#
# A laddered trailing-open signal must ARM the legs whose trigger the market has
# reached, even while deeper legs are still waiting for a further move -- it must
# NOT hold the whole ladder hostage to its deepest un-armed leg. This reproduces
# the 2026-07-01 V017 miss: Ask bottomed at ~3970.3 (enough for the top of the
# ladder) but the 8-leg BUY placed nothing because the 3968.5 leg never armed.
# ---------------------------------------------------------------------------

def _v017_ladder_plan(signal, activation, orders, *, target=3990.0, label="TP2"):
    return NewSignalPlan(
        signal=signal,
        action="FOLLOW",
        rationale="test",
        orders=orders,
        pending_expires_at=activation + timedelta(minutes=180),
        final_target_label=label,
        final_target_price=target,
        total_initial_risk_dollars=sum(o.risk_dollars for o in orders),
        pending_activates_at=activation,
        trailing_open_distance=0.5,
    )


def test_trailing_open_arms_reachable_buy_legs_while_deeper_legs_wait(monkeypatch):
    """At Ask 3970.33, V017 places BUY STOPs for #4 (3972.21) and #5 (3971.29)
    while #6/#7/#8 (3970.36/3969.43/3968.50) remain waiting -- the partial-arming
    fix. Before the fix the whole ladder was blocked by the un-armed deep legs."""
    signal = parse_one_signal(
        "2. BUY XAUUSD 3975 - 3973 SL 3968 TP1 3980 TP2 3990 TP3 4005 7:25 AM",
        source_date="2026-07-01", source_offset=3,
    )
    activation = signal.signal_time_chart
    monkeypatch.setattr(mt5_executor_trailing, "_wall_clock_chart_now",
                        lambda: activation + timedelta(minutes=1))
    plan = _v017_ladder_plan(signal, activation, orders=[
        PlannedOrder(3, signal.side, 3972.21, 3966.0, 0.10, 62.1),  # #4 arms Ask<=3971.71
        PlannedOrder(4, signal.side, 3971.29, 3965.0, 0.10, 62.1),  # #5 arms Ask<=3970.79
        PlannedOrder(5, signal.side, 3970.36, 3964.0, 0.10, 62.1),  # #6 arms Ask<=3969.86
        PlannedOrder(6, signal.side, 3969.43, 3963.0, 0.10, 62.1),  # #7 arms Ask<=3968.93
        PlannedOrder(7, signal.side, 3968.50, 3962.0, 0.10, 62.1),  # #8 arms Ask<=3968.00
    ])
    mt5 = _FakeMt5(bid=3970.11, ask=3970.33)
    executor = Mt5Executor(_FakeConn(mt5), "XAUUSD")

    log = executor.place_signal(signal, plan)

    # Only the two reachable legs armed (#4, #5); nothing deeper.
    assert log.placed == 2
    assert sorted(log.placed_entry_indices) == [3, 4]
    assert len(mt5.requests) == 2
    # Both are BUY STOP orders (not LIMIT), resting at Ask + distance (above market).
    assert all(req["type"] == _FakeMt5.ORDER_TYPE_BUY_STOP for req in mt5.requests)
    assert all(abs(req["price"] - (3970.33 + 0.5)) < 1e-6 for req in mt5.requests)
    # #6 / #7 / #8 are listed in the waiting log, not placed.
    waiting = next(a for a in log.actions if "trailing-open waiting" in a)
    assert "#6 arms when Ask<=3969.86" in waiting
    assert "#7 arms when Ask<=3968.93" in waiting
    assert "#8 arms when Ask<=3968" in waiting
    assert "#4 arms" not in waiting and "#5 arms" not in waiting


def test_trailing_open_places_nothing_when_no_buy_leg_is_armable(monkeypatch):
    """Ask above every arm threshold -> nothing placed and the waiting log lists
    the un-armable legs (the correct all-waiting case is preserved)."""
    signal = parse_one_signal(
        "2. BUY XAUUSD 3975 - 3973 SL 3968 TP1 3980 TP2 3990 TP3 4005 7:25 AM",
        source_date="2026-07-01", source_offset=3,
    )
    activation = signal.signal_time_chart
    monkeypatch.setattr(mt5_executor_trailing, "_wall_clock_chart_now",
                        lambda: activation + timedelta(minutes=1))
    plan = _v017_ladder_plan(signal, activation, orders=[
        PlannedOrder(3, signal.side, 3972.21, 3966.0, 0.10, 62.1),  # arms Ask<=3971.71
        PlannedOrder(4, signal.side, 3971.29, 3965.0, 0.10, 62.1),  # arms Ask<=3970.79
    ])
    # Ask 3973.00 is above both arm thresholds -> all waiting, nothing armed.
    mt5 = _FakeMt5(bid=3972.9, ask=3973.0)
    executor = Mt5Executor(_FakeConn(mt5), "XAUUSD")

    log = executor.place_signal(signal, plan)

    assert log.placed == 0
    assert mt5.requests == []
    assert any("trailing-open waiting" in a for a in log.actions)


def test_trailing_open_arms_reachable_sell_legs_while_deeper_legs_wait(monkeypatch):
    """Mirror of the BUY case: at Bid 3974.67 a SELL ladder arms #1/#2/#3
    (3972/3973/3974) while #4/#5 (3975/3976) wait; the STOPs are SELL STOPs at
    Bid - distance."""
    signal = parse_one_signal(
        "1. SELL XAUUSD 3972 - 3976 SL 3982 TP1 3966 TP2 3956 TP3 3946 7:25 AM",
        source_date="2026-07-01", source_offset=3,
    )
    activation = signal.signal_time_chart
    monkeypatch.setattr(mt5_executor_trailing, "_wall_clock_chart_now",
                        lambda: activation + timedelta(minutes=1))
    plan = _v017_ladder_plan(signal, activation, target=3956.0, orders=[
        PlannedOrder(0, signal.side, 3972.0, 3982.0, 0.10, 100.0),  # #1 arms Bid>=3972.5
        PlannedOrder(1, signal.side, 3973.0, 3983.0, 0.10, 100.0),  # #2 arms Bid>=3973.5
        PlannedOrder(2, signal.side, 3974.0, 3984.0, 0.10, 100.0),  # #3 arms Bid>=3974.5
        PlannedOrder(3, signal.side, 3975.0, 3985.0, 0.10, 100.0),  # #4 arms Bid>=3975.5
        PlannedOrder(4, signal.side, 3976.0, 3986.0, 0.10, 100.0),  # #5 arms Bid>=3976.5
    ])
    mt5 = _FakeMt5(bid=3974.67, ask=3974.89)
    executor = Mt5Executor(_FakeConn(mt5), "XAUUSD")

    log = executor.place_signal(signal, plan)

    assert log.placed == 3
    assert sorted(log.placed_entry_indices) == [0, 1, 2]
    assert len(mt5.requests) == 3
    assert all(req["type"] == _FakeMt5.ORDER_TYPE_SELL_STOP for req in mt5.requests)
    assert all(abs(req["price"] - (3974.67 - 0.5)) < 1e-6 for req in mt5.requests)
    waiting = next(a for a in log.actions if "trailing-open waiting" in a)
    assert "#4 arms when Bid>=3975.5" in waiting
    assert "#5 arms when Bid>=3976.5" in waiting
    assert "#1 arms" not in waiting and "#3 arms" not in waiting
