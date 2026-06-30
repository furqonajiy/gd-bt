"""Tests for the TSL18 quality-entry research layer in the scalper generator.

The layer LABELS every would-be ema-pullback entry with a no-lookahead quality
class + 0..1 score, an optional quality PROFILE that filters the feed by
class/score, and an extreme-entry mode (buy-bottom / sell-top). These tests pin:

  * default flags preserve existing generation byte-for-byte (parity);
  * the classifier assigns trend_pullback / countertrend_reversal /
    range_extreme_reversal / low_quality_chase / unknown correctly;
  * quality-profile off does not filter; trend_only drops non-trend classes;
    hybrid_quality rejects low_quality_chase;
  * extreme-entry mode keeps BUYs near support/demand and SELLs near
    resistance/supply, and rejects the off-side;
  * the classifier HTF trend has no lookahead (completion-stamped, like the
    structure guard);
  * the guard only ever removes signals, never invents one.

Pure semantics are tested through `_classify_quality` / `_quality_profile_ok` /
`_extreme_eval` (deterministic, no chart); parity + no-lookahead are tested
end-to-end through `_add_indicators` / `generate_signals` on tiny synthetic charts.
"""
from __future__ import annotations

import math
import types

import pandas as pd

from tools.generate_scalper_signals import (
    _add_indicators, _classify_quality, _extreme_eval, _quality_profile_ok,
    build_parser, generate_signals,
)


def _args(**overrides):
    args = build_parser().parse_args(["--charts", "x", "--output", "y"])
    for k, v in overrides.items():
        setattr(args, k, v)
    return args


def _qrow(**kw):
    """A benign BUY trend-pullback row; tests override only what they exercise."""
    base = dict(
        close=4000.0, high=4000.5, low=3999.5, open=3999.8,
        atr=2.0, ema_mid=3999.5, vwap=4000.0, rsi=50.0,
        bb_pctb=0.5, bb_bandwidth=0.001, adx=25.0,
        qual_htf_diff=2.0,                                  # bullish HTF
        pday_low=3950.0, pday_high=4050.0,                  # far away (not "near")
        sd_demand_low=float("nan"), sd_demand_high=float("nan"),
        sd_supply_low=float("nan"), sd_supply_high=float("nan"),
        qual_bear_recent=False, qual_bull_recent=False,
        swing_low=3990.0, swing_high=4010.0,
    )
    base.update(kw)
    return types.SimpleNamespace(**base)


# --- classifier semantics ---------------------------------------------------

def test_classify_trend_pullback():
    args = _args(entry_quality_classifier=True)
    qclass, score, diag = _classify_quality(_qrow(), "BUY", args)
    assert qclass == "trend_pullback"
    assert 0.0 <= score <= 1.0
    assert diag["htf_state"] == "bull" and diag["quality_class"] == "trend_pullback"


def test_classify_deep_trend_pullback():
    # aligned + a deep pullback (close >= 1 ATR from ema_mid)
    args = _args(entry_quality_classifier=True)
    qclass, _, _ = _classify_quality(_qrow(ema_mid=4002.5), "BUY", args)
    assert qclass == "deep_trend_pullback"


def test_classify_countertrend_reversal():
    # opposed HTF (BUY while bearish) but at a support extreme -> reversal
    args = _args(entry_quality_classifier=True)
    qclass, _, diag = _classify_quality(
        _qrow(qual_htf_diff=-2.0, pday_low=4000.0), "BUY", args)
    assert qclass == "countertrend_reversal"
    assert diag["htf_state"] == "bear" and diag["near_support"] == 1


def test_classify_range_extreme_reversal():
    # flat HTF (ranging) + at a support extreme -> range reversal
    args = _args(entry_quality_classifier=True)
    qclass, _, diag = _classify_quality(
        _qrow(qual_htf_diff=0.0, pday_low=4000.0), "BUY", args)
    assert qclass == "range_extreme_reversal"
    assert diag["htf_state"] == "flat"


def test_classify_low_quality_chase_aligned_overbought():
    # aligned but RSI overbought and not at a level -> chasing
    args = _args(entry_quality_classifier=True)
    qclass, score, _ = _classify_quality(_qrow(rsi=82.0), "BUY", args)
    assert qclass == "low_quality_chase"
    assert score <= 0.25      # chases are capped low


def test_classify_low_quality_chase_opposed_midrange():
    # opposed HTF and NOT at any extreme -> a counter-trend chase
    args = _args(entry_quality_classifier=True)
    qclass, _, _ = _classify_quality(_qrow(qual_htf_diff=-2.0), "BUY", args)
    assert qclass == "low_quality_chase"


def test_classify_unknown_on_nan_htf():
    args = _args(entry_quality_classifier=True)
    qclass, score, _ = _classify_quality(_qrow(qual_htf_diff=float("nan")), "BUY", args)
    assert qclass == "unknown" and score == 0.0


def test_classify_sell_trend_pullback():
    # mirror: SELL aligned with a bearish HTF, not extended -> trend pullback
    args = _args(entry_quality_classifier=True)
    qclass, _, diag = _classify_quality(
        _qrow(qual_htf_diff=-2.0, rsi=50.0, pday_high=4050.0, pday_low=3950.0,
              ema_mid=4000.0), "SELL", args)
    assert qclass == "trend_pullback" and diag["htf_state"] == "bear"


# --- quality profile gate ---------------------------------------------------

def test_profile_off_never_filters():
    args = _args(quality_profile="off")
    for qclass in ("trend_pullback", "low_quality_chase", "unknown"):
        ok, reason = _quality_profile_ok(qclass, 0.0, args)
        assert ok and reason == "accept"


def test_trend_only_filters_non_trend():
    args = _args(quality_profile="trend_only")
    assert _quality_profile_ok("trend_pullback", 0.5, args)[0]
    assert _quality_profile_ok("deep_trend_pullback", 0.5, args)[0]
    assert not _quality_profile_ok("countertrend_reversal", 0.9, args)[0]
    assert not _quality_profile_ok("range_extreme_reversal", 0.9, args)[0]
    assert not _quality_profile_ok("low_quality_chase", 0.9, args)[0]


def test_hybrid_quality_rejects_low_quality_chase():
    args = _args(quality_profile="hybrid_quality")
    ok, reason = _quality_profile_ok("low_quality_chase", 0.9, args)
    assert not ok and reason == "low_quality_chase"
    # trend pullbacks always kept; reversal kept when score clears the floor
    assert _quality_profile_ok("trend_pullback", 0.0, args)[0]
    assert _quality_profile_ok("countertrend_reversal", 0.7, args)[0]


def test_min_quality_score_floor_applies():
    args = _args(quality_profile="high_frequency_quality", min_quality_score=0.5)
    assert not _quality_profile_ok("trend_pullback", 0.4, args)[0]
    assert _quality_profile_ok("trend_pullback", 0.6, args)[0]


# --- extreme-entry mode -----------------------------------------------------

def test_extreme_buy_near_support_demand():
    args = _args(extreme_entry_mode="support_demand")
    # near a prior-day low -> accepted, level tagged
    ok, reason, ltype, price, dist = _extreme_eval(
        _qrow(pday_low=4000.0), "BUY", args)
    assert ok and reason == "accept" and ltype == "prior_low" and price == 4000.0
    # near a demand zone -> accepted
    ok2, _, ltype2, _, _ = _extreme_eval(
        _qrow(pday_low=3950.0, sd_demand_low=3999.0, sd_demand_high=4001.0), "BUY", args)
    assert ok2 and ltype2 == "demand_zone"
    # not near any support -> rejected
    ok3, reason3, _, _, _ = _extreme_eval(_qrow(pday_low=3950.0), "BUY", args)
    assert not ok3 and reason3 == "no_support_extreme"


def test_extreme_sell_near_resistance_supply():
    args = _args(extreme_entry_mode="supply_resistance")
    ok, reason, ltype, price, _ = _extreme_eval(
        _qrow(pday_high=4000.0), "SELL", args)
    assert ok and reason == "accept" and ltype == "prior_high" and price == 4000.0
    ok2, _, ltype2, _, _ = _extreme_eval(
        _qrow(pday_high=4050.0, sd_supply_low=3999.0, sd_supply_high=4001.0), "SELL", args)
    assert ok2 and ltype2 == "supply_zone"
    ok3, reason3, _, _, _ = _extreme_eval(_qrow(pday_high=4050.0), "SELL", args)
    assert not ok3 and reason3 == "no_resistance_extreme"


def test_extreme_mode_rejects_off_side():
    # support_demand is buy-bottom only -> a SELL is out of scope
    args = _args(extreme_entry_mode="support_demand")
    ok, reason, *_ = _extreme_eval(_qrow(pday_high=4000.0), "SELL", args)
    assert not ok and reason == "wrong_side_for_mode"
    # supply_resistance is sell-top only -> a BUY is out of scope
    args2 = _args(extreme_entry_mode="supply_resistance")
    ok2, reason2, *_ = _extreme_eval(_qrow(pday_low=4000.0), "BUY", args2)
    assert not ok2 and reason2 == "wrong_side_for_mode"


def test_extreme_both_keeps_each_side_at_its_extreme():
    args = _args(extreme_entry_mode="both")
    assert _extreme_eval(_qrow(pday_low=4000.0), "BUY", args)[0]
    assert _extreme_eval(_qrow(pday_high=4000.0), "SELL", args)[0]


# --- synthetic chart: parity, end-to-end filtering, no-add ------------------

def _synthetic_chart(n: int = 600) -> pd.DataFrame:
    rows = []
    t0 = pd.Timestamp("2026-06-01 00:00:00")
    prev_c = 2000.0
    for i in range(n):
        mid = 2000.0 + 0.35 * i + 5.0 * math.sin(i / 9.0)
        o = prev_c
        c = mid
        hi = max(o, c) + 0.4
        lo = min(o, c) - 0.4
        rows.append((t0 + pd.Timedelta(minutes=i), o, hi, lo, c, 2))
        prev_c = c
    return pd.DataFrame(rows, columns=["time", "open", "high", "low", "close", "spread"])


def test_default_flags_preserve_generation_parity():
    df = _synthetic_chart()
    base = generate_signals(df.copy(), _args())                 # everything off
    # turning the classifier ON with profile OFF must not change the feed
    annotated = generate_signals(df.copy(), _args(entry_quality_classifier=True))
    assert [(s.time, s.side, s.r1, s.sl, s.tp3) for s in base] == \
           [(s.time, s.side, s.r1, s.sl, s.tp3) for s in annotated]


def test_quality_profile_off_matches_baseline():
    df = _synthetic_chart()
    base = generate_signals(df.copy(), _args())
    off = generate_signals(df.copy(), _args(quality_profile="off"))
    assert len(off) == len(base)


def test_trend_only_filters_and_never_adds():
    df = _synthetic_chart()
    base = generate_signals(df.copy(), _args())
    # The monotonic rise keeps price far above the cumulative session VWAP (and
    # mildly elevated RSI), so without relaxing the chase knobs every pullback
    # classifies as low_quality_chase; disable the VWAP-chase / overbought-chase so
    # the aligned pullbacks read as trend pullbacks and the trend/reversal split is
    # exercised deterministically.
    relax = dict(quality_chase_vwap_atr=1e9, quality_rsi_overbought=90.0)
    trend = generate_signals(df.copy(), _args(quality_profile="trend_only", **relax))
    rev = generate_signals(df.copy(), _args(quality_profile="reversal_extreme", **relax))
    base_keys = {(s.time, s.side) for s in base}
    # both profiles can only remove signals from the base feed
    assert all((s.time, s.side) in base_keys for s in trend)
    assert all((s.time, s.side) in base_keys for s in rev)
    # the rising chart is dominated by aligned trend pullbacks, so trend_only keeps
    # them while reversal_extreme drops them.
    assert len(trend) > 0
    assert len(rev) < len(trend) <= len(base)


def test_quality_diagnostics_carry_class_and_reason():
    df = _synthetic_chart()
    sink: list[dict] = []
    a = _args(quality_profile="hybrid_quality", entry_quality_classifier=True,
              extreme_entry_mode="support_demand")
    generate_signals(df.copy(), a, quality_records=sink)
    assert sink, "expected at least one base-setup quality diagnostics row"
    classes = {"trend_pullback", "deep_trend_pullback", "countertrend_reversal",
               "range_extreme_reversal", "low_quality_chase", "unknown"}
    for rec in sink:
        assert rec["quality_class"] in classes
        assert set(rec) >= {"time", "side", "close", "quality_class", "quality_score",
                            "htf_state", "vwap_side", "rsi", "near_support",
                            "near_resistance", "near_demand", "near_supply",
                            "recent_opposite_impulse", "quality_reject_reason",
                            "extreme_mode_reason", "extreme_level_type"}


def test_extreme_mode_never_adds_signals():
    df = _synthetic_chart()
    base = generate_signals(df.copy(), _args())
    guarded = generate_signals(df.copy(), _args(extreme_entry_mode="both"))
    assert len(guarded) <= len(base)
    base_keys = {(s.time, s.side) for s in base}
    assert all((s.time, s.side) in base_keys for s in guarded)


# --- no-lookahead on the classifier HTF trend -------------------------------

def _htf_lookahead_chart() -> pd.DataFrame:
    hour_close = {4: 2060.0, 5: 2055.0, 6: 2050.0, 7: 2045.0, 8: 2040.0,
                  9: 2035.0, 10: 2100.0, 11: 2105.0, 12: 2110.0}
    rows = []
    t0 = pd.Timestamp("2026-06-01 04:00:00")
    for i in range(9 * 60):
        t = t0 + pd.Timedelta(minutes=i)
        if t.hour == 10 and t.minute < 55:
            c = 2034.0
        else:
            c = hour_close[t.hour]
        rows.append((t, c, c + 0.3, c - 0.3, c, 2))
    return pd.DataFrame(rows, columns=["time", "open", "high", "low", "close", "spread"])


def test_quality_htf_has_no_lookahead():
    # The late (10:55) bullish flip lives in the 10:00-11:00 bucket, which only
    # COMPLETES at 11:00. An M1 bar at 10:05 must still read the prior (bearish)
    # completed bucket -> qual_htf_diff < 0; at 11:05 the flip is visible -> > 0.
    a = _args(entry_quality_classifier=True, htf_minutes=60,
              htf_ema_fast=2, htf_ema_slow=4)
    out = _add_indicators(_htf_lookahead_chart(), a).set_index("time")
    early = float(out.loc[pd.Timestamp("2026-06-01 10:05:00"), "qual_htf_diff"])
    later = float(out.loc[pd.Timestamp("2026-06-01 11:05:00"), "qual_htf_diff"])
    assert early < 0, f"10:05 should be bearish (pre-flip), got {early}"
    assert later > 0, f"11:05 should be bullish (flip completed), got {later}"
    bucket = [out.loc[pd.Timestamp(f"2026-06-01 10:{m:02d}:00"), "qual_htf_diff"]
              for m in (1, 5, 30, 54, 59)]
    assert max(bucket) - min(bucket) < 1e-9    # flat across the in-progress bucket
