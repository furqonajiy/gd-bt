"""Tests for tools/btc_edge_gate.py pure measurement functions (no MT5, no data).

Covers spread-aware MFE/MAE for both sides, first-touch resolution (which level is
reached first, plus timeout), and the fair-coin breakeven win rate.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("btc_edge_gate", ROOT / "tools" / "btc_edge_gate.py")
gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gate)


def _arr(values):
    return np.asarray(values, dtype=np.float32)


def test_excursion_buy_is_marked_against_bid_from_ask_entry():
    bid = _arr([100.2, 101.0, 99.5])
    ask = _arr([100.7, 101.5, 100.0])
    mfe, mae = gate.excursion("BUY", entry_ask=100.5, entry_bid=100.0, bid_win=bid, ask_win=ask)
    assert mfe == 101.0 - 100.5      # best bid minus entry ask
    assert mae == 99.5 - 100.5       # worst bid minus entry ask (adverse, negative)


def test_excursion_sell_is_marked_against_ask_from_bid_entry():
    bid = _arr([99.7, 98.5, 100.5])
    ask = _arr([100.3, 99.0, 101.0])
    mfe, mae = gate.excursion("SELL", entry_ask=100.5, entry_bid=100.0, bid_win=bid, ask_win=ask)
    assert mfe == 100.0 - 99.0       # entry bid minus best (lowest) ask
    assert mae == 100.0 - 101.0      # entry bid minus worst (highest) ask


def test_first_touch_buy_win_before_loss():
    bid = _arr([101.0, 102.0, 97.0])   # +2 reached at idx1 before -2 at idx2
    ask = _arr([101.5, 102.5, 97.5])
    assert gate.first_touch("BUY", 100.0, 100.0, bid, ask, target=2.0, stop=2.0) == "win"


def test_first_touch_buy_loss_before_win():
    bid = _arr([99.0, 97.0, 103.0])    # -2 reached at idx1 before +2 at idx2
    ask = _arr([99.5, 97.5, 103.5])
    assert gate.first_touch("BUY", 100.0, 100.0, bid, ask, target=2.0, stop=2.0) == "loss"


def test_first_touch_timeout_when_neither_level_reached():
    bid = _arr([100.0, 101.0, 99.5])
    ask = _arr([100.5, 101.5, 100.0])
    assert gate.first_touch("BUY", 100.0, 100.0, bid, ask, target=2.0, stop=2.0) == "timeout"


def test_first_touch_sell_win_before_loss():
    ask = _arr([99.0, 97.0, 103.0])    # target hit when ask <= 98 (idx1) before stop ask >= 102 (idx2)
    bid = _arr([98.5, 96.5, 102.5])
    assert gate.first_touch("SELL", 100.0, 100.0, bid, ask, target=2.0, stop=2.0) == "win"


def test_breakeven_winrate():
    assert gate.breakeven_winrate(target=2.0, stop=2.0) == 0.5
    assert abs(gate.breakeven_winrate(target=4.0, stop=2.0) - (1.0 / 3.0)) < 1e-9
    assert abs(gate.breakeven_winrate(target=2.0, stop=4.0) - (2.0 / 3.0)) < 1e-9