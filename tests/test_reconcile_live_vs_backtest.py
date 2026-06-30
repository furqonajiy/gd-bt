"""Unit tests for the per-signal LIVE vs BACKTEST reconcile tool.

Covers the two parts that carry the logic: (1) backtest dollars recomputed at
the live lot from prices (side-aware), and (2) the discrepancy classifier that
produces the Description column. The HTML/xlsx parsers are exercised indirectly
by the e2e session run; here we pin the pure logic so a refactor can't silently
re-bucket a discrepancy.
"""
from __future__ import annotations

import datetime as dt

from tools.reconcile_live_vs_backtest import (
    DISCREPANCY, SignalRow, _signed_pnl, classify,
)


def _dtm(hhmm: str, day: int = 30) -> dt.datetime:
    h, m = hhmm.split(":")
    return dt.datetime(2026, 6, day, int(h), int(m))


# --- dollars at the live lot, side-aware ------------------------------------

def test_signed_pnl_buy_and_sell():
    # BUY up 10 pt at $1/pt -> +$10; SELL down 10 pt -> +$10 (profit).
    assert _signed_pnl("BUY", 4000.0, 4010.0, 1.0) == 10.0
    assert _signed_pnl("SELL", 4010.0, 4000.0, 1.0) == 10.0
    # SELL that went up loses; usd_per_point scales (0.02 lot -> 2.0).
    assert _signed_pnl("SELL", 4000.0, 4010.0, 1.0) == -10.0
    assert _signed_pnl("BUY", 4000.0, 4005.0, 2.0) == 10.0
    assert _signed_pnl("BUY", None, 4010.0, 1.0) is None


# --- discrepancy classification ---------------------------------------------

def _row(**kw) -> SignalRow:
    base = dict(sig=1, side="SELL", bt_legs=8, lv_legs=8, lv_fills=8,
                bt_pnl=0.0, lv_pnl=0.0, bt_exit="SL",
                bt_open=_dtm("06:00"), bt_close=_dtm("06:30"),
                lv_open=_dtm("06:00"), lv_close=_dtm("06:30"),
                d_entry=0.0, d_open_min=0, d_close_min=0, d_pnl=0.0)
    base.update(kw)
    r = SignalRow(**{k: v for k, v in base.items() if k in SignalRow.__dataclass_fields__})
    r.d_pnl = r.lv_pnl - r.bt_pnl
    return r


def test_classify_keys_are_known():
    for r in (_row(), _row(lv_legs=1), _row(lv_fills=12)):
        k, _ = classify(r)
        assert k in DISCREPANCY


def test_no_live_and_no_bt_and_open():
    assert classify(_row(lv_legs=0, lv_fills=0, lv_pnl=0.0))[0] == "NO_LIVE"
    assert classify(_row(bt_legs=0, bt_pnl=0.0))[0] == "NO_BT"
    r = _row(); r.lv_has_open = True
    assert classify(r)[0] == "STILL_OPEN"


def test_under_fill_beats_other_classes():
    # fewer live legs than backtest -> ladder under-fill, regardless of timing.
    r = _row(lv_legs=1, lv_fills=1, bt_pnl=-144.0, lv_pnl=-18.0, d_close_min=30)
    assert classify(r)[0] == "UNDER_FILL"


def test_late_arm_only_when_materially_later():
    assert classify(_row(d_open_min=100, bt_pnl=-57.0, lv_pnl=-8.0))[0] == "LATE_ARM"
    # a 15-min EARLIER arm is not a deployment late-arm -> not LATE_ARM
    assert classify(_row(d_open_min=-15, d_pnl=0.0))[0] != "LATE_ARM"


def test_reopen_churn_when_extra_fills():
    r = _row(lv_fills=12, bt_pnl=102.0, lv_pnl=28.0)
    assert classify(r)[0] == "OVER_FILL"
    assert "re-fill" in classify(r)[1] or "re-arm" in classify(r)[1]


def test_exit_drift_and_match():
    # same legs/fills, exit 6 min late -> exit-time drift
    assert classify(_row(d_close_min=6, bt_pnl=-94.0, lv_pnl=-87.0))[0] == "LATE_CLOSE"
    # everything tight -> match
    assert classify(_row(d_close_min=1, d_entry=-0.4, bt_pnl=268.0, lv_pnl=271.0))[0] == "MATCH"


def test_description_carries_counterfactual_dollars():
    # the Description must quote both live and model dollars (the operator ask)
    _, desc = classify(_row(lv_fills=12, bt_pnl=102.0, lv_pnl=28.0))
    assert "$" in desc and "model" in desc.lower()
