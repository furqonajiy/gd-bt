"""--trailing-live-entry must place the FULL trailing-open ladder even when the
M1 replay marked some legs "already played out". The replay filters those legs
out of the placeable `orders`, so without this rebuild a fast trailing signal
permanently drops them (the live #07 #1/#2 bug). _restore_trailing_ladder_orders
adds them back using the PLANNED entry price (not the played-out leg's modelled
fill), with SL/lot from the replay entry.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from trading.engine import PlannedOrder
from trading.engine.cli import _restore_trailing_ladder_orders


@dataclass
class _FakeEntry:
    entry_index: int
    entry_price: float   # for a played-out leg this is the MODELLED FILL, not planned
    initial_sl: float
    lot: float


def _order(idx, ep, sl=0.0, lot=0.01):
    return PlannedOrder(entry_index=idx, side="SELL", entry_price=ep,
                        initial_sl=sl, lot=lot, risk_dollars=0.0)


def test_restores_played_out_legs_with_planned_price():
    # Replay played out legs 0 and 1 (fills 4040.9/4040.2); legs 2..6 still placeable.
    planned = [4034.5, 4035.05, 4035.6, 4036.15, 4036.7, 4037.25, 4037.8]
    replay_entries = [
        _FakeEntry(0, 4040.90, 4049.0, 0.01),   # played out -> entry_price = fill
        _FakeEntry(1, 4040.20, 4049.0, 0.01),   # played out
        _FakeEntry(2, 4035.6, 4049.0, 0.01),
        _FakeEntry(3, 4036.15, 4049.0, 0.01),
        _FakeEntry(4, 4036.7, 4049.0, 0.01),
        _FakeEntry(5, 4037.25, 4049.0, 0.01),
        _FakeEntry(6, 4037.8, 4049.0, 0.01),
    ]
    placed = [_order(i, planned[i], sl=4049.0) for i in (2, 3, 4, 5, 6)]  # replay-filtered subset

    full = _restore_trailing_ladder_orders(placed, replay_entries, planned, "SELL")

    assert [o.entry_index for o in full] == [0, 1, 2, 3, 4, 5, 6]   # full ladder
    # the restored legs use the PLANNED price, not the played-out fill (4040.x)
    assert full[0].entry_price == 4034.5
    assert full[1].entry_price == 4035.05
    # SL / lot carried from the replay entry
    assert full[0].initial_sl == 4049.0 and full[0].lot == 0.01


def test_noop_when_all_legs_already_placeable():
    planned = [4034.5, 4035.05]
    entries = [_FakeEntry(0, 4034.5, 4049.0, 0.01), _FakeEntry(1, 4035.05, 4049.0, 0.01)]
    placed = [_order(0, 4034.5), _order(1, 4035.05)]
    full = _restore_trailing_ladder_orders(placed, entries, planned, "SELL")
    assert [o.entry_index for o in full] == [0, 1]
    assert full is not placed  # returns a new list, original untouched
