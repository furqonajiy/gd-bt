"""Unit tests for tools/parity_reconcile.py pure logic (no data/MT5 needed)."""
from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from pytest import approx  # noqa: E402

from tools.parity_reconcile import (  # noqa: E402
    BtLeg, LiveEntry, LiveExit,
    classify_pnl, decompose, entry_cost_pts, exit_cost_pts,
    _norm_close_idx, signed_move,
)


def test_signed_move_directions():
    assert signed_move("BUY", 100.0, 101.0) == 1.0
    assert signed_move("BUY", 100.0, 99.0) == -1.0
    assert signed_move("SELL", 100.0, 99.0) == 1.0
    assert signed_move("SELL", 100.0, 101.0) == -1.0


def test_entry_cost_positive_when_worse():
    # BUY filled above the modeled entry = paid more = positive cost.
    assert entry_cost_pts("BUY", 100.0, 100.3) == approx(0.3)
    # SELL filled below the modeled entry = sold cheaper = positive cost.
    assert entry_cost_pts("SELL", 100.0, 99.7) == approx(0.3)


def test_exit_cost_positive_when_worse():
    # BUY exits below the modeled exit = sold lower = positive cost.
    assert exit_cost_pts("BUY", 105.0, 104.6) == approx(0.4)
    # SELL exits above the modeled exit = bought back higher = positive cost.
    assert exit_cost_pts("SELL", 95.0, 95.4) == approx(0.4)


def test_classify_pnl():
    assert classify_pnl(None) == "OPEN"
    assert classify_pnl(12.0) == "WIN"
    assert classify_pnl(-3.0) == "LOSS"
    assert classify_pnl(0.0) == "FLAT"


def test_norm_close_idx_one_based_and_unknown():
    assert _norm_close_idx(1) == 0
    assert _norm_close_idx(3) == 2
    assert _norm_close_idx("2") == 1
    assert _norm_close_idx("?") is None
    assert _norm_close_idx(0) is None
    assert _norm_close_idx(True) is None


def _row(flag, *, live_pnl=None, bt_pnl=None, lot=1.0):
    from tools.parity_reconcile import CompRow
    return CompRow(
        signal_key="K", idx=0, side="BUY", flag=flag, lot=lot,
        bt_entry=100.0, live_entry=100.1, entry_slip=0.1,
        bt_exit=105.0, live_exit=104.9, exit_slip=0.1,
        bt_status="TP1", live_reason="TP",
        bt_pnl_at_live_lot=bt_pnl, live_pnl=live_pnl,
        pnl_delta=(None if (live_pnl is None or bt_pnl is None) else live_pnl - bt_pnl),
    )


def test_decompose_buckets_sum_to_gap():
    rows = [
        _row("SLIP", live_pnl=48.0, bt_pnl=50.0),     # -2 slippage
        _row("FLIP", live_pnl=-30.0, bt_pnl=50.0),    # -80 label flip
        _row("FILL_ONLY_LIVE", live_pnl=10.0),        # +10 fills only live
        _row("FILL_ONLY_BT", bt_pnl=20.0),            # -20 fills only bt
    ]
    comp = decompose(rows)
    assert round(comp["slippage"], 6) == -2.0
    assert round(comp["label_flip"], 6) == -80.0
    assert round(comp["fill_only_live"], 6) == 10.0
    assert round(comp["fill_only_bt"], 6) == -20.0
    # live_total = 48 - 30 + 10 = 28 ; bt_total = 50 + 50 + 20 = 120
    assert round(comp["live_total"], 6) == 28.0
    assert round(comp["bt_total_at_live_lot"], 6) == 120.0
    assert round(comp["gap"], 6) == round(comp["live_total"] - comp["bt_total_at_live_lot"], 6)


def test_dataclasses_construct():
    assert LiveEntry(1.0, "t", 0.1, 1.0, 1).lot == 0.1
    assert LiveExit(2.0, 5.0, "TP", 1).reason == "TP"
    assert BtLeg(1.0, 2.0, "TP1", None, None, True, True).closed is True