"""Tests for the anti-wrong-side-structure guard in the scalper generator.

The guard targets the specific failure mode of scalping AGAINST the larger
structure (BUY in a bearish HTF / SELL in a bullish HTF / entry right after an
opposite-side impulse). These tests pin:

  * default flags preserve existing signal generation byte-for-byte (parity);
  * BUY is rejected when the HTF is bearish (and SELL when bullish) once the
    filter is enabled;
  * the impulse cooldown rejects wrong-side entries;
  * the diagnostics stream carries a reject reason for every base-setup bar.

The veto semantics are tested through the pure ``_structure_eval`` (deterministic,
no chart needed); parity + diagnostics are tested end-to-end through
``generate_signals`` on a tiny synthetic chart.
"""
from __future__ import annotations

import types

import pandas as pd

from tools.generate_scalper_signals import (
    _add_indicators, _structure_eval, build_parser, generate_signals,
)


def _args(**overrides):
    # start from the real parser defaults so a test can't drift from production
    args = build_parser().parse_args([
        "--charts", "x", "--output", "y",
    ])
    for k, v in overrides.items():
        setattr(args, k, v)
    return args


def _row(**kw):
    base = dict(close=4000.0, vwap=3990.0, struct_htf_diff=1.0,
                struct_bear_recent=False, struct_bull_recent=False,
                swing_low=3980.0, swing_high=4020.0)
    base.update(kw)
    return types.SimpleNamespace(**base)


# --- pure veto semantics ----------------------------------------------------

def test_buy_rejected_when_htf_bearish():
    args = _args(structure_filter=True)
    ok, reason, diag = _structure_eval(_row(struct_htf_diff=-2.0), "BUY", args)
    assert not ok and reason == "htf_bearish_buy"
    assert diag["htf_state"] == "bear"


def test_sell_rejected_when_htf_bullish():
    args = _args(structure_filter=True)
    ok, reason, _ = _structure_eval(_row(struct_htf_diff=+2.0), "SELL", args)
    assert not ok and reason == "htf_bullish_sell"


def test_aligned_side_accepted():
    args = _args(structure_filter=True)
    ok_buy, r_buy, _ = _structure_eval(_row(struct_htf_diff=+2.0), "BUY", args)
    ok_sell, r_sell, _ = _structure_eval(_row(struct_htf_diff=-2.0, close=3970.0), "SELL", args)
    assert ok_buy and r_buy == "accept"
    assert ok_sell and r_sell == "accept"


def test_htf_nan_vetoes():
    args = _args(structure_filter=True)
    ok, reason, _ = _structure_eval(_row(struct_htf_diff=float("nan")), "BUY", args)
    assert not ok and reason == "htf_nan"


def test_impulse_cooldown_rejects_wrong_side():
    # BUY after a recent bearish impulse is vetoed; flip the impulse and a BUY
    # in a bullish HTF is fine.
    args = _args(structure_filter=True, structure_impulse_atr=1.5,
                 structure_impulse_cooldown_bars=5)
    ok, reason, _ = _structure_eval(
        _row(struct_htf_diff=+2.0, struct_bear_recent=True), "BUY", args)
    assert not ok and reason == "impulse_cooldown"
    ok2, reason2, _ = _structure_eval(
        _row(struct_htf_diff=+2.0, struct_bear_recent=False), "BUY", args)
    assert ok2 and reason2 == "accept"


def test_impulse_veto_off_when_params_zero():
    # the same bearish impulse does NOT veto when the impulse knobs are off
    args = _args(structure_filter=True, structure_impulse_atr=0.0,
                 structure_impulse_cooldown_bars=0)
    ok, reason, _ = _structure_eval(
        _row(struct_htf_diff=+2.0, struct_bear_recent=True), "BUY", args)
    assert ok and reason == "accept"


def test_vwap_side_veto():
    args = _args(structure_filter=True, structure_require_vwap_side=True)
    ok, reason, _ = _structure_eval(
        _row(struct_htf_diff=+2.0, close=3980.0, vwap=3990.0), "BUY", args)
    assert not ok and reason == "vwap_wrong_side"


def test_min_score_veto_and_score_value():
    # bullish HTF, above VWAP, no impulse, swing intact -> score 4
    args = _args(structure_filter=True, structure_min_score=4)
    ok, reason, diag = _structure_eval(_row(struct_htf_diff=+2.0), "BUY", args)
    assert ok and diag["score"] == 4
    # require 4 but break ONLY the swing (still above VWAP) -> score 3 -> rejected
    ok2, reason2, diag2 = _structure_eval(
        _row(struct_htf_diff=+2.0, close=3992.0, vwap=3990.0, swing_low=3995.0), "BUY", args)
    assert not ok2 and reason2 == "score_below_min" and diag2["score"] == 3


# --- synthetic chart: parity + diagnostics ----------------------------------

def _synthetic_chart(n: int = 600) -> pd.DataFrame:
    # a rising trend with a regular oscillation, so price keeps pulling back to
    # the moving averages -> the ema-pullback entry actually fires (both sides).
    import math

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
    a = _args()  # structure OFF by default
    s1 = generate_signals(df.copy(), a)
    s2 = generate_signals(df.copy(), a)
    # deterministic + identical, and the guard is a pure no-op when off
    assert [(s.time, s.side, s.r1, s.sl, s.tp3) for s in s1] == \
           [(s.time, s.side, s.r1, s.sl, s.tp3) for s in s2]
    a_off2 = _args(structure_filter=False)
    s3 = generate_signals(df.copy(), a_off2)
    assert len(s3) == len(s1)


def test_structure_diagnostics_carry_reject_reasons():
    df = _synthetic_chart()
    a = _args(structure_filter=True, structure_htf_minutes=15,
              structure_ema_fast=5, structure_ema_slow=12, structure_min_score=3)
    sink: list[dict] = []
    generate_signals(df.copy(), a, struct_records=sink)
    assert sink, "expected at least one base-setup diagnostics row"
    allowed = {"accept", "htf_nan", "htf_bearish_buy", "htf_bullish_sell",
               "vwap_wrong_side", "impulse_cooldown", "score_below_min"}
    for rec in sink:
        assert rec["reject_reason"] in allowed
        assert set(rec) >= {"time", "side", "close", "htf_state", "vwap_side",
                            "impulse_state", "score", "reject_reason"}


def _htf_lookahead_chart() -> pd.DataFrame:
    # H1 closes decline 04:00..09:00 (bearish), then the 10:00 bucket spikes UP
    # late (at 10:55) and stays up. With a responsive fast/slow EMA the H1 trend
    # FLIPS bullish on the 10:00 bucket -- but that bucket only COMPLETES at 11:00.
    # We set each hour's HH:59 close to the target (resample().last() reads it).
    hour_close = {4: 2060.0, 5: 2055.0, 6: 2050.0, 7: 2045.0, 8: 2040.0,
                  9: 2035.0, 10: 2100.0, 11: 2105.0, 12: 2110.0}
    rows = []
    t0 = pd.Timestamp("2026-06-01 04:00:00")
    for i in range(9 * 60):  # 04:00 .. 12:59
        t = t0 + pd.Timedelta(minutes=i)
        # within the 10:00 hour, stay low until the 10:55 spike -> the flip is LATE
        if t.hour == 10 and t.minute < 55:
            c = 2034.0
        else:
            c = hour_close[t.hour]
        rows.append((t, c, c + 0.3, c - 0.3, c, 2))
    return pd.DataFrame(rows, columns=["time", "open", "high", "low", "close", "spread"])


def _htf_indicators():
    a = _args(structure_filter=True, structure_htf_minutes=60,
              structure_ema_fast=2, structure_ema_slow=4)
    out = _add_indicators(_htf_lookahead_chart(), a)
    return a, out.set_index("time")


def test_htf_structure_has_no_lookahead():
    # The late (10:55) bullish flip lives in the 10:00-11:00 bucket, which only
    # completes at 11:00. An M1 bar at 10:05 must therefore still read the prior
    # (bearish) completed bucket, NOT the future flip.
    _, idx = _htf_indicators()
    early = float(idx.loc[pd.Timestamp("2026-06-01 10:05:00"), "struct_htf_diff"])
    later = float(idx.loc[pd.Timestamp("2026-06-01 11:05:00"), "struct_htf_diff"])
    assert early < 0, f"10:05 should be bearish (pre-flip), got {early}"
    assert later > 0, f"11:05 should be bullish (flip now completed), got {later}"
    # and the value is flat across the whole in-progress bucket (no intra-bucket leak)
    bucket = [idx.loc[pd.Timestamp(f"2026-06-01 10:{m:02d}:00"), "struct_htf_diff"]
              for m in (1, 5, 30, 54, 59)]
    assert max(bucket) - min(bucket) < 1e-9


def test_buy_rejected_only_when_completed_htf_bearish():
    a, idx = _htf_indicators()
    early = idx.loc[pd.Timestamp("2026-06-01 10:05:00")]   # completed HTF bearish
    later = idx.loc[pd.Timestamp("2026-06-01 11:05:00")]   # completed HTF bullish
    assert not _structure_eval(early, "BUY", a)[0]          # BUY rejected (bearish)
    assert _structure_eval(later, "BUY", a)[0]              # BUY allowed (bullish)


def test_sell_rejected_only_when_completed_htf_bullish():
    a, idx = _htf_indicators()
    early = idx.loc[pd.Timestamp("2026-06-01 10:05:00")]   # completed HTF bearish
    later = idx.loc[pd.Timestamp("2026-06-01 11:05:00")]   # completed HTF bullish
    assert _structure_eval(early, "SELL", a)[0]             # SELL allowed (bearish)
    assert not _structure_eval(later, "SELL", a)[0]         # SELL rejected (bullish)


def test_structure_on_never_adds_signals():
    # the guard can only remove signals, never invent them
    df = _synthetic_chart()
    base = generate_signals(df.copy(), _args())
    guarded = generate_signals(df.copy(), _args(
        structure_filter=True, structure_htf_minutes=15,
        structure_ema_fast=5, structure_ema_slow=12))
    assert len(guarded) <= len(base)
    base_times = {(s.time, s.side) for s in base}
    assert all((s.time, s.side) in base_times for s in guarded)
