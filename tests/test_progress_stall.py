"""Tests for the trend-progress stall cap (the refined, safer anti-cluster filter).

Covers the no-lookahead column construction (_add_indicators) and the stateful
veto state machine (_ProgressStall), plus parity / combine-only-removes / diag.
"""
from __future__ import annotations

import types

import pandas as pd

from tools.generate_scalper_signals import (
    _ProgressStall, _add_indicators, build_parser, generate_signals,
)


def _args(**ov):
    a = build_parser().parse_args(["--charts", "x", "--output", "y"])
    for k, v in ov.items():
        setattr(a, k, v)
    return a


# --- state machine (_ProgressStall.decide) ----------------------------------

def _row(**kw):
    base = dict(prog_regime="bear", prog_leg_id=1, prog_epoch=0,
                prog_valid_progress=False, prog_bars_since_progress=99.0,
                prog_local_ref=4000.0, high=4001.0, low=3999.0)
    base.update(kw)
    return types.SimpleNamespace(**base)


def _ps(**ov):
    return _ProgressStall(_args(progress_stall_filter=True, progress_stall_n=3,
                               progress_min_no_progress_bars=20,
                               progress_probe_interval_bars=30, **ov))


def test_out_of_scope_passes_through():
    ps = _ps()
    ok_nan, r_nan, _ = ps.decide(_row(prog_regime="nan"), "SELL", 1)
    assert ok_nan and r_nan == "htf_nan"
    ok_flat, r_flat, _ = ps.decide(_row(prog_regime="flat"), "SELL", 2)
    assert ok_flat and r_flat == "htf_flat"
    ok_opp, r_opp, _ = ps.decide(_row(prog_regime="bear"), "BUY", 3)
    assert ok_opp and r_opp == "htf_opposite"


def test_first_same_side_signal_allowed():
    ps = _ps()
    ok, reason, _ = ps.decide(_row(prog_epoch=0), "SELL", 1)
    assert ok and reason == "accept"


def test_valid_progress_resets_count():
    ps = _ps()
    for p in range(1, 4):
        ps.decide(_row(prog_epoch=0, prog_bars_since_progress=25.0), "SELL", p)
    ok, reason, d = ps.decide(_row(prog_epoch=0, prog_valid_progress=True), "SELL", 10)
    assert ok and reason == "accept" and d["non_progressing_count"] == 0


def test_progress_on_any_bar_between_signals_rearms_via_epoch():
    # signals are pullbacks (valid=False at the signal bar), but a breakout bar
    # BETWEEN signals advanced the epoch -> the next same-side signal re-arms.
    ps = _ps()
    for p in range(1, 4):
        ps.decide(_row(prog_epoch=0, prog_bars_since_progress=25.0), "SELL", p)
    ok, reason, _ = ps.decide(_row(prog_epoch=1, prog_valid_progress=False,
                                   prog_bars_since_progress=2.0), "SELL", 12)
    assert ok and reason == "accept"


def test_stall_requires_both_count_and_bars():
    ps = _ps()
    res = [ps.decide(_row(prog_bars_since_progress=5.0), "SELL", p) for p in range(1, 6)]
    assert all(ok for ok, _, _ in res), "no veto while bars_since < min_no_progress_bars"
    ps2 = _ps()
    out = [ps2.decide(_row(prog_bars_since_progress=25.0), "SELL", p) for p in range(1, 5)]
    assert out[0][0] and out[1][0]            # count 1,2 -> accept
    assert not out[2][0] and out[2][1] == "progress_stall"   # count 3 >= n -> veto
    assert not out[3][0]                       # stays vetoed (within probe interval)


def test_counter_frozen_after_stall():
    ps = _ps()
    decs = [ps.decide(_row(prog_bars_since_progress=25.0), "SELL", p) for p in range(1, 9)]
    counts = [d[2]["non_progressing_count"] for d in decs]
    assert max(counts) == 3, f"counter must freeze at stall_n=3, saw {counts}"


def test_probe_allowed_only_after_interval_and_does_not_unblock():
    ps = _ps()   # probe interval 30 bars
    # reach stall at pos 3 (blocked, last_probe_pos=3)
    for p in range(1, 4):
        ps.decide(_row(prog_bars_since_progress=25.0), "SELL", p)
    # pos 20 (<30 since 3) -> still vetoed
    ok_early, r_early, _ = ps.decide(_row(prog_bars_since_progress=40.0), "SELL", 20)
    assert not ok_early and r_early == "progress_stall"
    # pos 33 (>=30 since 3) -> exactly one probe allowed
    ok_probe, r_probe, d_probe = ps.decide(_row(prog_bars_since_progress=50.0), "SELL", 33)
    assert ok_probe and d_probe["probe_allowed"] == 1
    # probe did NOT unblock: next signal soon after is vetoed again
    ok_after, r_after, _ = ps.decide(_row(prog_bars_since_progress=55.0), "SELL", 40)
    assert not ok_after and r_after == "progress_stall"


def test_new_leg_resets_state():
    ps = _ps()
    for p in range(1, 5):
        ps.decide(_row(prog_leg_id=1, prog_bars_since_progress=25.0), "SELL", p)
    ok, reason, _ = ps.decide(_row(prog_leg_id=2, prog_epoch=0), "SELL", 5)
    assert ok and reason == "accept"


# --- no-lookahead column construction (_add_indicators) ----------------------

def _chart(rows):
    return pd.DataFrame(rows, columns=["time", "open", "high", "low", "close", "spread"])


def _const_hours(spec: dict[int, float], minute_close=None) -> pd.DataFrame:
    # spec: hour-of-series -> H1 close target (set at HH:59); other bars flat at it
    rows = []
    t0 = pd.Timestamp("2026-06-01 00:00:00")
    for i in range(len(spec) * 60):
        t = t0 + pd.Timedelta(minutes=i)
        c = spec[i // 60]
        o = c
        hi, lo = c + 0.2, c - 0.2
        rows.append((t, o, hi, lo, c, 2))
    return _chart(rows)


def test_regime_uses_completed_candles_no_lookahead():
    # 18 declining H1 candles (enough for ATR-14 warmup), then hour 18 closes far
    # UP. That up-bucket completes only at hour 19, so early bars of hour 18 must
    # still read the prior (down) completed regime; the flip appears at hour 19.
    spec = {h: 2100.0 - 5.0 * h for h in range(18)}   # 0..17 declining
    spec[18] = 2200.0                                  # late up-spike bucket
    spec[19] = 2205.0
    a = _args(progress_stall_filter=True, progress_htf_minutes=60,
              progress_ema_fast=2, progress_ema_slow=4)
    idx = _add_indicators(_const_hours(spec), a).set_index("time")
    early = idx.loc[pd.Timestamp("2026-06-01 18:05:00"), "prog_htf_diff_atr"]
    later = idx.loc[pd.Timestamp("2026-06-01 19:05:00"), "prog_htf_diff_atr"]
    assert early < 0 and later > 0   # hour-18 up-flip only visible from hour 19


def test_prior_extreme_excludes_current_bar_and_close_confirm():
    # up-leg; a bar makes a new HIGH but closes back below -> NOT valid progress
    # (close confirmation); a bar that also closes above the prior extreme IS.
    rows = []
    t0 = pd.Timestamp("2026-06-01 00:00:00")
    # 16h (960 bars) of rising closes -> bull regime with ATR-14 H1 warmup
    n = 960
    for i in range(n):
        c = 2000 + i * 0.5
        rows.append((t0 + pd.Timedelta(minutes=i), c, c + 0.3, c - 0.3, c, 2))
    base_c = 2000 + (n - 1) * 0.5
    # bar A (idx n): high spikes above prior extreme but CLOSE falls back -> wick only
    rows.append((t0 + pd.Timedelta(minutes=n), base_c, base_c + 5.0, base_c - 0.3, base_c - 1.0, 2))
    # bar B (idx n+1): high AND close break the prior extreme -> confirmed progress
    rows.append((t0 + pd.Timedelta(minutes=n + 1), base_c, base_c + 6.0, base_c - 0.3, base_c + 6.0, 2))
    a = _args(progress_stall_filter=True, progress_htf_minutes=60, progress_ema_fast=2,
              progress_ema_slow=4, progress_min_atr=0.1, progress_min_points=0.5,
              progress_close_confirm_atr=0.1)
    out = _add_indicators(_chart(rows), a)
    assert out.iloc[n]["prog_regime"] == "bull"   # sanity: regime established
    wick = bool(out.iloc[n]["prog_valid_progress"])
    confirm = bool(out.iloc[n + 1]["prog_valid_progress"])
    assert wick is False, "wick-only new high must NOT count as progress"
    assert confirm is True, "close-confirmed new high must count as progress"


# --- generate-level: parity, combine-only-removes, diagnostics ---------------

def _sine_chart(n=600):
    import math
    rows = []
    t0 = pd.Timestamp("2026-06-01 00:00:00")
    prev = 2000.0
    for i in range(n):
        mid = 2000.0 + 0.35 * i + 5.0 * math.sin(i / 9.0)
        rows.append((t0 + pd.Timedelta(minutes=i), prev, max(prev, mid) + 0.4, min(prev, mid) - 0.4, mid, 2))
        prev = mid
    return _chart(rows)


def test_default_off_preserves_generation_parity():
    df = _sine_chart()
    base = generate_signals(df.copy(), _args())                       # all off
    prog_off = generate_signals(df.copy(), _args(progress_stall_filter=False))
    assert [(s.time, s.side) for s in base] == [(s.time, s.side) for s in prog_off]


def test_progress_stall_only_removes_signals_and_logs_reason():
    df = _sine_chart()
    base = generate_signals(df.copy(), _args())
    recs: list[dict] = []
    guarded = generate_signals(df.copy(), _args(
        progress_stall_filter=True, progress_htf_minutes=15, progress_ema_fast=5,
        progress_ema_slow=12, progress_stall_n=2, progress_min_no_progress_bars=3),
        prog_records=recs)
    assert len(guarded) <= len(base)
    base_keys = {(s.time, s.side) for s in base}
    assert all((s.time, s.side) in base_keys for s in guarded)   # never adds
    assert recs, "diagnostics should record would-be-taken decisions"
    reasons = {r["reject_reason"] for r in recs}
    assert reasons <= {"accept", "progress_stall", "htf_nan", "htf_flat", "htf_opposite"}
    for r in recs:
        assert set(r) >= {"time", "side", "htf_regime", "htf_leg_id", "local_ref",
                         "valid_progress", "bars_since_valid_progress",
                         "non_progressing_count", "stall_blocked", "bars_since_last_probe",
                         "probe_allowed", "reject_reason"}
