"""A pending LIMIT inside the broker stop/freeze band is DEFERRED, not 10015'd.

Regression for the 2026-06-18 live failure on VIC-2026-06-18#09: a SELL ladder
where the near legs were price-passed and one fresh leg sat just above the bid,
inside the broker freeze level. That leg was order_sent as a LIMIT, rejected with
retcode 10015 (invalid price), and the rejection rolled back + abandoned the WHOLE
signal so it never got tracked -- and the replay-driven reopen pass (which opens
price-passed legs at market with the original stop+target) never got to run.

The fix: defer any leg that is price-passed OR within the broker stops/freeze
distance of the market instead of order_sending a doomed LIMIT, so the placeable
legs still place, the signal is tracked, and reopen_missing_open_positions mirrors
the rest. This test pins that a within-band leg is never sent to the broker while a
genuinely placeable leg in the same ladder still places.
"""
from __future__ import annotations
from datetime import timedelta

from trading.engine import DEFAULT_CONFIG, Mt5Executor, NewSignalPlan, PlannedOrder, parse_one_signal
from trading.engine.execution import mt5_executor_live


class _Resp:
    def __init__(self, retcode=10009, comment="done", order=123):
        self.retcode = retcode
        self.comment = comment
        self.order = order


class _Sym:
    digits = 2
    trade_stops_level = 50   # 50 points * POINT_VALUE(0.01) = $0.50 min distance


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
    ORDER_FILLING_RETURN = 2

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


def _freeze_wall_clock(monkeypatch, when):
    monkeypatch.setattr(mt5_executor_live, "_wall_clock_chart_now", lambda: when)


def _multi_leg_plan(signal, *, entries, initial_sl, target):
    activation = signal.signal_time_chart + timedelta(minutes=DEFAULT_CONFIG.activation_delay_minutes)
    orders = [
        PlannedOrder(entry_index=i, side=signal.side, entry_price=p,
                     initial_sl=initial_sl, lot=0.13, risk_dollars=10.0)
        for i, p in enumerate(entries)
    ]
    return NewSignalPlan(
        signal=signal, action="FOLLOW", rationale="test", orders=orders,
        pending_expires_at=activation + timedelta(minutes=DEFAULT_CONFIG.pending_expiry_minutes),
        final_target_label="TP3", final_target_price=target,
        total_initial_risk_dollars=30.0,
    )


def test_within_freeze_band_leg_is_deferred_not_order_sent(monkeypatch):
    _reset_executor_guards()
    signal = parse_one_signal(
        "9. SELL XAUUSD 4271 - 4273 SL 4280 TP1 4261 TP2 4251 TP3 4236 02:02 PM",
        source_date="2026-06-18",
        source_offset=3,
    )
    _freeze_wall_clock(
        monkeypatch,
        signal.signal_time_chart + timedelta(minutes=DEFAULT_CONFIG.activation_delay_minutes + 1),
    )
    # bid 4273.75, min stop distance 0.5 -> SELL LIMIT placeable only at >= 4274.25.
    #   4275 : placeable LIMIT (above the band)
    #   4274 : within the freeze band  -> DEFER (this is the leg that used to 10015)
    #   4271 : price-passed (<= bid)    -> DEFER (stale)
    plan = _multi_leg_plan(signal, entries=[4275.0, 4274.0, 4271.0],
                           initial_sl=4280.0, target=4236.0)
    mt5 = _FakeMt5(bid=4273.75, ask=4274.00)
    executor = Mt5Executor(_FakeConn(mt5), "XAUUSD")
    executor._allow_partial_placement = True  # reopen/mirror mode

    log = executor.place_signal(signal, plan)

    # Only the placeable leg is sent to the broker; the within-band and the
    # price-passed legs are deferred, so nothing 10015s and the signal is not
    # hard-failed (placed == 1, no failure log).
    assert log.placed == 1
    assert len(mt5.requests) == 1
    assert mt5.requests[0]["type"] == mt5.ORDER_TYPE_SELL_LIMIT
    assert mt5.requests[0]["price"] == 4275.0
    assert any("within broker stop/freeze level" in a for a in log.actions)
    assert any("stale SELL LIMIT 4271" in a for a in log.actions)
    assert not any("placement failed" in a for a in log.actions)
    assert not any("FAILED" in a for a in log.actions)
    assert signal.signal_key not in executor._session_failed_signal_keys
