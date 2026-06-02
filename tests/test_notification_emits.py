"""Part B: the executor/CLI actually emit notifications at each action site.

Spies on the Notifier surface and asserts the wiring fires once, with the right
payload, on placement, fill reconciliation, and skips. These guard against the
emits silently going dead again (every method existed before but was uncalled).
"""
from __future__ import annotations
import json
from datetime import datetime, timedelta

from xauusd_trading import (
    DEFAULT_CONFIG, Mt5Executor, NewSignalPlan, Notifier, PlannedOrder,
    open_position, parse_one_signal, signal_to_magic,
)
from xauusd_trading.execution import mt5_executor_tp2


class _SpyNotifier:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    def __getattr__(self, name):
        def _record(**kwargs):
            self.calls.append((name, kwargs))
        return _record

    def of(self, name: str) -> list[dict]:
        return [kw for n, kw in self.calls if n == name]


class _Resp:
    def __init__(self, order=123):
        self.retcode = 10009
        self.comment = "done"
        self.order = order


class _Sym:
    digits = 2


class _Tick:
    def __init__(self, bid, ask):
        self.bid = bid
        self.ask = ask


class _PlaceMt5:
    TRADE_ACTION_PENDING = 5
    TRADE_RETCODE_DONE = 10009
    ORDER_TYPE_BUY_LIMIT = 2
    ORDER_TYPE_SELL_LIMIT = 3
    ORDER_TIME_GTC = 0
    ORDER_FILLING_RETURN = 2

    def __init__(self, *, bid, ask):
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


class _ReconPos:
    def __init__(self, *, ticket, magic, type_, price_open, sl, tp, volume, time, comment=""):
        self.ticket = ticket
        self.magic = magic
        self.type = type_
        self.price_open = price_open
        self.sl = sl
        self.tp = tp
        self.volume = volume
        self.time = time
        self.comment = comment


class _ReconMt5:
    POSITION_TYPE_BUY = 0
    POSITION_TYPE_SELL = 1

    def __init__(self, positions):
        self._positions = list(positions)

    def positions_get(self, symbol=None):
        return list(self._positions)

    def orders_get(self, symbol=None):
        return []


class _Conn:
    def __init__(self, mt5):
        self.mt5 = mt5


class _EmptyChart:
    def bars_between(self, start, end):
        return []

    def last_time(self):
        return None


def _reset_guards():
    Mt5Executor._session_skipped_inactive_signal_keys.clear()
    Mt5Executor._session_skipped_stale_entries.clear()
    Mt5Executor._session_skipped_expired_signal_keys.clear()
    Mt5Executor._session_failed_signal_keys.clear()


def _signal():
    return parse_one_signal(
        "1. SELL XAUUSD 4520 - 4522 SL 4530 TP1 4512 TP2 4502 TP3 4488 11:25 AM",
        source_date="2026-05-05", source_offset=7,
    )


def test_order_placed_fires_once_on_successful_limit_placement(monkeypatch):
    _reset_guards()
    signal = _signal()
    activation = signal.signal_time_chart + timedelta(minutes=DEFAULT_CONFIG.activation_delay_minutes)
    monkeypatch.setattr(mt5_executor_tp2, "_wall_clock_chart_now",
                        lambda: activation + timedelta(minutes=1))
    plan = NewSignalPlan(
        signal=signal, action="FOLLOW", rationale="test",
        orders=[PlannedOrder(entry_index=0, side="SELL", entry_price=4521.0,
                             initial_sl=4530.0, lot=0.13, risk_dollars=10.0)],
        pending_expires_at=activation + timedelta(minutes=DEFAULT_CONFIG.pending_expiry_minutes),
        final_target_label="TP3", final_target_price=4488.0,
        total_initial_risk_dollars=10.0,
    )
    spy = _SpyNotifier()
    # SELL LIMIT 4521 is above live bid 4504.58 -> placeable.
    mt5 = _PlaceMt5(bid=4504.58, ask=4504.85)
    executor = Mt5Executor(_Conn(mt5), "XAUUSD", notifier=spy)

    executor.place_signal(signal, plan)

    placed = spy.of("order_placed")
    assert len(placed) == 1
    assert placed[0]["side"] == "SELL"
    assert [p["entry_index"] for p in placed[0]["placed"]] == [0]
    assert placed[0]["placed"][0]["ticket"] == 1001


def test_entry_filled_fires_once_on_pending_to_open(monkeypatch):
    _reset_guards()
    signal = _signal()
    pos = open_position(signal, equity=1000.0, config=DEFAULT_CONFIG)
    assert pos.entries[0].status == "PENDING"
    magic = signal_to_magic(signal.signal_key)
    mt5_pos = _ReconPos(
        ticket=501, magic=magic, type_=_ReconMt5.POSITION_TYPE_SELL,
        price_open=4521.0, sl=4530.0, tp=4488.0, volume=0.13,
        time=0, comment="x.1",
    )
    spy = _SpyNotifier()
    executor = Mt5Executor(_Conn(_ReconMt5([mt5_pos])), "XAUUSD",
                           server_offset_hours=3, notifier=spy)
    executor._broker_epoch_to_chart_time = lambda epoch: datetime(2026, 5, 5, 7, 27)
    now = datetime(2026, 5, 5, 7, 30)

    executor.reconcile_with_mt5(pos, DEFAULT_CONFIG, _EmptyChart(), now)
    filled = spy.of("entry_filled")
    assert len(filled) == 1
    assert filled[0]["fill_price"] == 4521.0
    assert filled[0]["ticket"] == 501

    # Second reconcile is a noop -> no duplicate fill notification.
    executor.reconcile_with_mt5(pos, DEFAULT_CONFIG, _EmptyChart(), now)
    assert len(spy.of("entry_filled")) == 1


def test_signal_skipped_writes_expected_event(tmp_path):
    path = tmp_path / "n.jsonl"
    Notifier(path).signal_skipped(signal_key="2026-05-05#01", side="SELL",
                                  reason="pending window already closed")
    line = path.read_text(encoding="utf-8").strip()
    event = json.loads(line)
    assert event["kind"] == "signal_skipped"
    assert event["signal_key"] == "2026-05-05#01"
    assert "skipped" in event["text"].lower()
    assert "pending window already closed" in event["text"]
