"""Unit tests for the LIVE-ONLY stale/terminal entry guard (LiveEntryGuard) and
the TSL18 live snapshot's safe-guard flags.

The guard is the placement-side half of the 2026-07-01 TSL18 stale-revival fix:
a signal whose original SL was already touched, whose current price is through
the original SL, whose live RR is too thin, or whose trailing-close would
instantly stop it out, must NOT be opened. Default-off: ``maybe`` returns None
unless a threshold is set, so the live path is unchanged for a fresh signal.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from trading.engine import LiveEntryGuard

_REPO = Path(__file__).resolve().parents[1]


def _guard(**kw):
    base = dict(max_age_minutes=0, min_rr=1.0, min_reward_distance=0.0,
                max_spread_fraction_of_risk=0.0, trailing_close_distance=0.0,
                freeze_distance=0.0)
    base.update(kw)
    return LiveEntryGuard.maybe(**base)


def _check(g, **kw):
    base = dict(side="BUY", planned_entry=4030.0, effective_sl=4025.0,
                original_sl=4026.0, tp1=4040.0, final_target=4052.0,
                age_minutes=1.0, bid=4030.0, ask=4030.2,
                sl_hit_after=False, target_hit_after=False)
    base.update(kw)
    return g.check(**base)


# -- maybe() gating ---------------------------------------------------------

def test_maybe_off_by_default_is_none():
    assert LiveEntryGuard.maybe() is None
    assert LiveEntryGuard.maybe(min_rr=1.0) is not None
    assert LiveEntryGuard.maybe(max_age_minutes=20) is not None
    assert LiveEntryGuard.maybe(max_spread_fraction_of_risk=0.25) is not None


# -- terminal (already touched) wins first ----------------------------------

def test_sl_already_touched_is_terminal():
    g = _guard()
    assert "original SL already touched" in _check(g, sl_hit_after=True)


def test_target_already_reached_is_terminal():
    g = _guard()
    assert "TP/final target already reached" in _check(g, target_hit_after=True)


# -- price context vs ORIGINAL SL (the exact reported bug) ------------------

def test_buy_current_price_through_original_sl_is_skipped():
    # BUY 4032-4030, SL 4028, live price collapsed to ~4014 -> through the SL.
    g = _guard()
    reason = _check(g, side="BUY", original_sl=4028.0, effective_sl=4026.0,
                    planned_entry=4014.2, bid=4014.0, ask=4014.2, final_target=4052.0)
    assert "original SL" in reason and "Ask" in reason


def test_sell_current_price_through_original_sl_is_skipped():
    # SELL entry ~4030, SL 4032 (above), live price rose to 4040 -> through SL.
    g = _guard()
    reason = _check(g, side="SELL", original_sl=4032.0, effective_sl=4034.0,
                    planned_entry=4040.0, bid=4040.0, ask=4040.2, tp1=4020.0,
                    final_target=4008.0)
    assert "original SL" in reason and "Bid" in reason


# -- risk/reward + friction + immediate-close -------------------------------

def test_low_rr_is_skipped():
    g = _guard(min_rr=1.0)
    # risk 1.0, reward_final 0.5 -> rr 0.5 < 1.0
    reason = _check(g, original_sl=4025.0, effective_sl=4029.0,
                    planned_entry=4030.0, bid=4030.0, ask=4030.0, final_target=4030.5)
    assert "low live RR" in reason


def test_thin_reward_distance_is_skipped():
    g = _guard(min_rr=0.0, min_reward_distance=2.0)
    # tp1 reward = 4030.5 - 4030 = 0.5 < 2.0
    reason = _check(g, original_sl=4025.0, effective_sl=4028.0, planned_entry=4030.0,
                    bid=4030.0, ask=4030.0, tp1=4030.5, final_target=4060.0)
    assert "thin live reward" in reason


def test_spread_friction_is_skipped():
    g = _guard(min_rr=0.0, max_spread_fraction_of_risk=0.25)
    # risk = 4030-4029 = 1.0; spread 0.5 > 0.25*1.0
    reason = _check(g, original_sl=4025.0, effective_sl=4029.0, planned_entry=4030.0,
                    bid=4029.7, ask=4030.2, final_target=4060.0)
    assert "friction" in reason


def test_immediate_close_risk_is_skipped():
    # trailing-close 0.5 <= spread 0.3 + freeze 0.4 -> would instantly stop out.
    # max_age_minutes enables the guard (age 1 < 20 won't trigger); in the live
    # snapshot the spread knob enables it the same way.
    g = _guard(min_rr=0.0, max_age_minutes=20, max_spread_fraction_of_risk=0.0,
               trailing_close_distance=0.5, freeze_distance=0.4)
    reason = _check(g, original_sl=4025.0, effective_sl=4025.0, planned_entry=4030.0,
                    bid=4030.0, ask=4030.3, final_target=4060.0)
    assert "immediate-close" in reason


def test_valid_fresh_entry_passes():
    g = _guard(min_rr=1.0, min_reward_distance=2.0, max_spread_fraction_of_risk=0.25,
               trailing_close_distance=0.5, freeze_distance=0.0)
    # entry 4030, SL 4025 (risk 5), final 4052 (reward 22 -> rr 4.4), tp1 4040
    # (reward 10), spread 0.2 (< 0.25*5=1.25), trailing 0.5 > spread 0.2.
    assert _check(g, original_sl=4026.0, effective_sl=4025.0, planned_entry=4030.0,
                  bid=4030.0, ask=4030.2, tp1=4040.0, final_target=4052.0) is None


# -- CLI snapshot carries the safe flags, not the dangerous one -------------

def test_tsl18_snapshot_has_safe_flags_and_no_dangerous_flag():
    out = subprocess.run([sys.executable, "cli/run.py", "tsl18", "3", "--print"],
                         cwd=_REPO, capture_output=True, text=True).stdout
    assert "--max-live-signal-age-minutes 20" in out
    assert "--min-live-entry-rr 1.0" in out
    assert "--min-live-entry-reward-distance 2.0" in out
    assert "--max-live-spread-fraction-of-risk 0.25" in out
    # the dangerous replay-revival flag must NEVER appear in the live snapshot
    assert "--allow-live-replay-played-out-legs" not in out
