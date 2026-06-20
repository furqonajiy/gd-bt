from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta

from trading.xauusd import Bar, DEFAULT_CONFIG, Mt5Executor, NewSignalPlan, PlannedOrder
from trading.xauusd import advance_bars, open_position, parse_one_signal
from trading.xauusd.execution import mt5_executor_trailing


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