"""Trailing-open STOP placement race: between the cycle tick and order_send the
market can cross the trigger, making the STOP invalid (a BUY STOP must sit above
Ask). The virtual trailing entry HAS fired at that point -- the backtest fills it
at the trigger -- so the executor must fall back to a market DEAL instead of
dropping the leg. The fallback fires ONLY when a fresh tick confirms the trigger
was genuinely passed; any other rejection keeps the legacy all-or-nothing path,
because an early market fill below the trigger would open a trade the model
never had.
"""
from __future__ import annotations

from datetime import timedelta

from xauusd_trading import Mt5Executor, NewSignalPlan, PlannedOrder, parse_one_signal
from xauusd_trading.execution import mt5_executor_trailing


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
    TRADE_ACTION_DEAL = 1
    TRADE_ACTION_PENDING = 5
    TRADE_ACTION_MODIFY = 6
    TRADE_ACTION_SLTP = 7
    TRADE_ACTION_REMOVE = 8
    TRADE_RETCODE_DONE = 10009
    ORDER_TYPE_BUY = 0
    ORDER_TYPE_SELL = 1
    ORDER_TYPE_BUY_LIMIT = 2
    ORDER_TYPE_SELL_LIMIT = 3
    ORDER_TYPE_BUY_STOP = 4
    ORDER_TYPE_SELL_STOP = 5
    POSITION_TYPE_BUY = 0
    POSITION_TYPE_SELL = 1
    ORDER_TIME_GTC = 0
    ORDER_FILLING_RETURN = 2
    ORDER_FILLING_IOC = 1
    SYMBOL_FILLING_IOC = 2
    SYMBOL_FILLING_FOK = 1

    def __init__(self, *, ticks, reject_pending=True, reject_deal=False):
        # `ticks` is consumed one per symbol_info_tick call; the last one repeats.
        # This models the race: the placement-cycle tick differs from the fresh
        # tick the fallback re-reads after the STOP reject.
        self._ticks = list(ticks)
        self.reject_pending = reject_pending
        self.reject_deal = reject_deal
        self.requests = []
        self._orders = []
        self._positions = []

    def symbol_info(self, symbol):
        return _Sym()

    def symbol_info_tick(self, symbol):
        if len(self._ticks) > 1:
            return self._ticks.pop(0)
        return self._ticks[0]

    def positions_get(self, symbol=None):
        return list(self._positions)

    def orders_get(self, symbol=None):
        return list(self._orders)

    def order_send(self, request):
        self.requests.append(dict(request))
        if request["action"] == self.TRADE_ACTION_PENDING and self.reject_pending:
            return _Resp(retcode=10015, comment="Invalid price", order=0)
        if request["action"] == self.TRADE_ACTION_DEAL and self.reject_deal:
            return _Resp(retcode=10018, comment="Market closed", order=0)
        return _Resp(order=1000 + len(self.requests))

    def last_error(self):
        return (1, "generic fail")


class _FakeConn:
    def __init__(self, mt5):
        self.mt5 = mt5


def _buy_plan(signal, activation):
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


def _buy_signal():
    return parse_one_signal(
        "1. BUY XAUUSD 4750 - 4748 SL 4744 TP1 4760 TP2 4770 TP3 4780 10:00 AM",
        source_date="2026-06-01",
        source_offset=3,
    )


def test_rejected_buy_stop_falls_back_to_market_when_trigger_passed(monkeypatch):
    signal = _buy_signal()
    activation = signal.signal_time_chart
    monkeypatch.setattr(
        mt5_executor_trailing, "_wall_clock_chart_now",
        lambda: activation + timedelta(minutes=1),
    )
    # Cycle tick arms the entry (Ask 4740.2 -> trigger 4742.2); by the time the
    # STOP is rejected the fresh tick shows Ask 4743.0 >= trigger.
    mt5 = _FakeMt5(ticks=[_Tick(4740.0, 4740.2), _Tick(4742.8, 4743.0)])
    executor = Mt5Executor(_FakeConn(mt5), "XAUUSD")
    executor._session_announced_triggers.clear()

    log = executor.place_signal(signal, _buy_plan(signal, activation))

    assert log.placed == 1
    assert log.placed_entry_indices == [0]
    assert len(mt5.requests) == 2
    stop_req, deal_req = mt5.requests
    assert stop_req["action"] == mt5.TRADE_ACTION_PENDING
    assert stop_req["type"] == mt5.ORDER_TYPE_BUY_STOP
    assert deal_req["action"] == mt5.TRADE_ACTION_DEAL
    assert deal_req["type"] == mt5.ORDER_TYPE_BUY
    assert deal_req["price"] == 4743.0
    # The leg keeps its planned stop DISTANCE anchored on the actual fill:
    # 4750 - 4740.34 = 9.66 below 4743.0.
    assert deal_req["sl"] == 4733.34
    assert deal_req["tp"] == 4780.0
    assert "deviation" in deal_req
    assert any("FILLED AT MARKET" in a for a in log.actions)
    # The reconcile dedup key is pre-registered so the fill isn't announced twice.
    assert f"{signal.signal_key}|0" in executor._session_announced_triggers


def test_rejected_buy_stop_does_not_market_fill_when_trigger_not_passed(monkeypatch):
    signal = _buy_signal()
    activation = signal.signal_time_chart
    monkeypatch.setattr(
        mt5_executor_trailing, "_wall_clock_chart_now",
        lambda: activation + timedelta(minutes=1),
    )
    # Rejection for some other reason: fresh Ask 4740.2 is still below the
    # trigger 4742.2, so a market fill would be a trade the model never had.
    mt5 = _FakeMt5(ticks=[_Tick(4740.0, 4740.2)])
    executor = Mt5Executor(_FakeConn(mt5), "XAUUSD")

    log = executor.place_signal(signal, _buy_plan(signal, activation))

    assert log.placed == 0
    assert [r["action"] for r in mt5.requests] == [mt5.TRADE_ACTION_PENDING]
    assert any("FAILED" in a for a in log.actions)
    assert any("no registry entry" in a for a in log.actions)


def test_market_fallback_failure_keeps_legacy_all_or_nothing(monkeypatch):
    signal = _buy_signal()
    activation = signal.signal_time_chart
    monkeypatch.setattr(
        mt5_executor_trailing, "_wall_clock_chart_now",
        lambda: activation + timedelta(minutes=1),
    )
    mt5 = _FakeMt5(
        ticks=[_Tick(4740.0, 4740.2), _Tick(4742.8, 4743.0)],
        reject_deal=True,
    )
    executor = Mt5Executor(_FakeConn(mt5), "XAUUSD")

    log = executor.place_signal(signal, _buy_plan(signal, activation))

    assert log.placed == 0
    assert [r["action"] for r in mt5.requests] == [mt5.TRADE_ACTION_PENDING, mt5.TRADE_ACTION_DEAL]
    assert any("also FAILED" in a for a in log.actions)
    assert any("no registry entry" in a for a in log.actions)


def test_rejected_sell_stop_falls_back_to_market_when_trigger_passed(monkeypatch):
    signal = parse_one_signal(
        "1. SELL XAUUSD 4750 - 4752 SL 4756 TP1 4742 TP2 4732 TP3 4722 10:00 AM",
        source_date="2026-06-01",
        source_offset=3,
    )
    activation = signal.signal_time_chart
    monkeypatch.setattr(
        mt5_executor_trailing, "_wall_clock_chart_now",
        lambda: activation + timedelta(minutes=1),
    )
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
    # Cycle tick arms the SELL (Bid 4759.8 -> trigger 4757.8); the fresh tick
    # shows Bid 4757.0 <= trigger, i.e. the pullback already traded through.
    mt5 = _FakeMt5(ticks=[_Tick(4759.8, 4760.0), _Tick(4757.0, 4757.2)])
    executor = Mt5Executor(_FakeConn(mt5), "XAUUSD")
    executor._session_announced_triggers.clear()

    log = executor.place_signal(signal, plan)

    assert log.placed == 1
    stop_req, deal_req = mt5.requests
    assert stop_req["type"] == mt5.ORDER_TYPE_SELL_STOP
    assert deal_req["action"] == mt5.TRADE_ACTION_DEAL
    assert deal_req["type"] == mt5.ORDER_TYPE_SELL
    assert deal_req["price"] == 4757.0
    # Planned stop distance 4756 - 4750 = 6.0 above the actual fill.
    assert deal_req["sl"] == 4763.0
