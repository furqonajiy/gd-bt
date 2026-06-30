"""Manual-close trailing-open re-arm must never CHASE: a re-entry may only fill at
the ORIGINAL planned entry or better. Pins:
  * Entry.original_entry_price is captured at construction and survives the fill
    patch to entry_price (so re-arm compares against the planned level).
  * _candidate_trailing_open_price waits for the pullback (no-chase trigger).
  * _market_fill_passed_trailing_open's STOP-reject fallback refuses to fill worse
    than the original entry.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

_REPO = Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from trading.engine import ExecutionLog
from trading.engine.core.positions import Entry
from trading.engine.execution.mt5_executor_trailing import Mt5Executor


# -- original_entry_price preservation --------------------------------------

def test_entry_original_price_survives_fill_mutation():
    e = Entry(entry_index=0, entry_price=4700.0, initial_sl=4694.0, lot=0.01)
    assert e.original_entry_price == 4700.0
    e.entry_price = 4702.5            # the lifecycle patches this to the fill
    assert e.original_entry_price == 4700.0   # the planned level is preserved


# -- no-chase trigger (waits for pullback) ----------------------------------

class _StubMt5:
    ORDER_TYPE_BUY = 0
    ORDER_TYPE_SELL = 1
    ORDER_TYPE_BUY_STOP = 4
    ORDER_TYPE_SELL_STOP = 5
    TRADE_ACTION_DEAL = 1
    TRADE_RETCODE_DONE = 10009

    def __init__(self, bid, ask):
        self._bid, self._ask = bid, ask

    def symbol_info_tick(self, s):
        return SimpleNamespace(bid=self._bid, ask=self._ask, time=0, time_msc=0, last=0.0)

    def symbol_info(self, s):
        return SimpleNamespace(point=0.01, digits=2, trade_stops_level=0, volume_min=0.01,
                               volume_step=0.01, filling_mode=1)

    def last_error(self):
        return (1, "ok")


def _exec(bid, ask):
    return Mt5Executor(SimpleNamespace(mt5=_StubMt5(bid, ask)), "XAUUSD",
                       min_lot=0.01, lot_step=0.01, server_offset_hours=3,
                       notifier=None, forensic=None)


def test_buy_rearm_waits_when_price_above_original_entry():
    # BUY original entry 4700, current Ask 4750 -> must NOT arm (would chase).
    ex = _exec(bid=4749.8, ask=4750.0)
    assert ex._candidate_trailing_open_price("BUY", 4700.0, 4749.8, 4750.0, 0.5) is None


def test_buy_rearm_allowed_when_price_back_at_original_zone():
    # BUY original entry 4700, Ask 4699.5, distance 0.5 -> trigger 4700.0 <= entry.
    ex = _exec(bid=4699.3, ask=4699.5)
    trig = ex._candidate_trailing_open_price("BUY", 4700.0, 4699.3, 4699.5, 0.5)
    assert trig is not None and trig <= 4700.0


def test_sell_rearm_waits_when_price_below_original_entry():
    # SELL original entry 4750, current Bid 4700 -> must NOT arm (would chase).
    ex = _exec(bid=4700.0, ask=4700.2)
    assert ex._candidate_trailing_open_price("SELL", 4750.0, 4700.0, 4700.2, 0.5) is None


def test_sell_rearm_allowed_when_price_back_at_original_zone():
    ex = _exec(bid=4750.5, ask=4750.7)
    trig = ex._candidate_trailing_open_price("SELL", 4750.0, 4750.5, 4750.7, 0.5)
    assert trig is not None and trig >= 4750.0


# -- STOP-reject market fallback refuses worse-than-original ----------------

def _order(idx=0):
    return SimpleNamespace(entry_index=idx, entry_price=4700.0)


def test_market_fallback_skipped_when_buy_fill_worse_than_original_entry():
    # Trigger crossed (Ask 4702.4 >= trigger 4700.3) but Ask is ABOVE the original
    # entry 4700 -> the fallback must skip, not chase.
    ex = _exec(bid=4702.2, ask=4702.4)
    log = ExecutionLog()
    placed = ex._market_fill_passed_trailing_open(
        SimpleNamespace(side="BUY", signal_key="TSL18-2026-07-01#04"),
        _order(2), trigger=4700.3, lot=0.01, stop_distance=6.0, tp=4800.0,
        magic=1, comment="x", digits=2, log=log, reject_reason="10015",
        original_entry=4700.0)
    assert placed is False
    assert any("worse than original entry" in a for a in log.actions)


def test_market_fallback_skipped_when_sell_fill_worse_than_original_entry():
    ex = _exec(bid=4748.2, ask=4748.4)
    log = ExecutionLog()
    placed = ex._market_fill_passed_trailing_open(
        SimpleNamespace(side="SELL", signal_key="TSL18-2026-07-01#07"),
        _order(3), trigger=4749.7, lot=0.01, stop_distance=6.0, tp=4700.0,
        magic=1, comment="x", digits=2, log=log, reject_reason="10015",
        original_entry=4750.0)
    assert placed is False
    assert any("worse than original entry" in a for a in log.actions)
