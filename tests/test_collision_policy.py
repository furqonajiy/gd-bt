"""Tests for the TSL18 collision policies (CollisionPolicy).

The layer is default-OFF and parity-preserving: with the baseline policies
(``opposite_signal_policy="allow_hedge"`` + ``same_side_overlap_policy
="allow_all"``) ``CollisionPolicy.maybe`` returns ``None`` and ``run_backtest``
does zero extra work, so its output is byte-identical to a run without the layer.

Covered here: default-off parity, the opposite-side decisions (reject / flip /
profit-bank / reduce) with BUY-active+SELL and the SELL-active+BUY mirror, the
same-side cluster decisions (reject / better-entry-only / fixed-risk downsize),
the no-chase re-arm constraint, and that an SL/TP/engine close (or a touched
original SL) stays terminal.
"""
from __future__ import annotations

import types
from datetime import datetime
from pathlib import Path

from trading.engine import (
    CollisionDecision, CollisionPolicy, StrategyConfig, can_rearm, status_is_terminal,
)
from trading.engine.strategy.collision_policy import (
    OPPOSITE_POLICIES, SAME_SIDE_POLICIES,
)


# --- helpers ----------------------------------------------------------------

def _sig(key, side, t, *, rh, rl, sl):
    dt = datetime.fromisoformat(t)
    return types.SimpleNamespace(
        signal_key=key, side=side, signal_time_chart=dt, signal_time_source=dt,
        range_high=rh, range_low=rl, sl=sl)


def _rows(*, entries, sl, lot=1.0, fill="2026-06-01 10:00", exit=None,
          status="OPEN", pnl=0.0):
    """Per-entry rows shaped like replay_signal_rows' output. ``entries`` is the
    list of planned entry prices; one leg each."""
    ft = datetime.fromisoformat(fill) if fill else None
    xt = datetime.fromisoformat(exit) if exit else None
    return [{"entry_price": ep, "effective_SL": sl, "lot": lot,
             "fill_time": ft, "exit_time": xt, "trading_pnl": pnl,
             "entry_status": status, "exit_price": None}
            for ep in entries]


def _register(policy, key, side, t, *, entries, sl, lot=1.0,
              fill="2026-06-01 10:00", exit=None, status="OPEN", pnl=0.0):
    """Build + register an accepted active signal (no collision on it)."""
    rh, rl = max(entries), min(entries)
    sig = _sig(key, side, t, rh=rh, rl=rl, sl=sl)
    rows = _rows(entries=entries, sl=sl, lot=lot, fill=fill, exit=exit,
                 status=status, pnl=pnl)
    dec = policy.decide(sig, rows)
    policy.register(sig, {"entry_rows": rows}, dec)
    return sig


def _cfg(**over):
    return StrategyConfig(minimum_lot=0.01, max_hold_minutes=90,
                          pending_expiry_minutes=180, **over)


# --- default-off parity -----------------------------------------------------

def test_default_config_yields_no_policy():
    assert CollisionPolicy.maybe(StrategyConfig()) is None


def test_explicit_baseline_yields_no_policy():
    # allow_hedge + allow_all == current behavior -> no policy object built.
    assert CollisionPolicy.maybe(StrategyConfig(
        opposite_signal_policy="allow_hedge",
        same_side_overlap_policy="allow_all")) is None


def test_any_non_baseline_policy_enables_layer():
    for over in (dict(opposite_signal_policy="reject_opposite"),
                 dict(opposite_signal_policy="close_then_flip"),
                 dict(same_side_overlap_policy="reject_overlap"),
                 dict(same_side_overlap_policy="scale_in_fixed_risk")):
        assert CollisionPolicy.maybe(StrategyConfig(**over)) is not None


def test_policy_name_tuples_cover_the_cli_choices():
    assert OPPOSITE_POLICIES == (
        "allow_hedge", "reject_opposite", "profit_bank_rearm",
        "close_then_flip", "reduce_then_hedge")
    assert SAME_SIDE_POLICIES == (
        "allow_all", "reject_overlap", "scale_in_better_entry_only",
        "scale_in_fixed_risk")


# --- opposite-side: BUY active + SELL ---------------------------------------

def test_reject_opposite_skips_the_new_sell_while_buy_open():
    p = CollisionPolicy.maybe(_cfg(opposite_signal_policy="reject_opposite"))
    _register(p, "BUY1", "BUY", "2026-06-01 10:00", entries=[4700.0], sl=4690.0)
    sell = _sig("SELL1", "SELL", "2026-06-01 10:10", rh=4700.0, rl=4700.0, sl=4710.0)
    dec = p.decide(sell, _rows(entries=[4700.0], sl=4710.0, fill=None, status="PENDING"))
    assert dec.accept is False
    assert dec.collision_type == "opposite"
    assert dec.action == "reject_opposite"
    assert p.summary()["opposite_collisions_rejected"] == 1


def test_close_then_flip_closes_the_buy_and_opens_the_sell():
    p = CollisionPolicy.maybe(_cfg(opposite_signal_policy="close_then_flip"))
    _register(p, "BUY1", "BUY", "2026-06-01 10:00", entries=[4700.0], sl=4690.0)
    sell = _sig("SELL1", "SELL", "2026-06-01 10:10", rh=4700.0, rl=4700.0, sl=4710.0)
    dec = p.decide(sell, _rows(entries=[4700.0], sl=4710.0),
                   price_at=lambda t: 4705.0)
    assert dec.accept is True
    assert dec.action == "close_then_flip"
    assert dec.opposite_exposure_before == 1.0
    assert dec.opposite_exposure_after == 0.0
    # the old BUY is retired now and is terminal (an engine close).
    assert p._active and p._active[0].end == sell.signal_time_chart
    assert p._active[0].terminal is True
    assert p.summary()["opposite_collisions_flipped"] == 1


def test_reduce_then_hedge_keeps_both_but_cuts_old_side_and_downsizes_new():
    p = CollisionPolicy.maybe(_cfg(opposite_signal_policy="reduce_then_hedge",
                                   hedge_lot_fraction=0.5))
    _register(p, "BUY1", "BUY", "2026-06-01 10:00", entries=[4700.0], sl=4690.0, lot=1.0)
    sell = _sig("SELL1", "SELL", "2026-06-01 10:10", rh=4700.0, rl=4700.0, sl=4710.0)
    dec = p.decide(sell, _rows(entries=[4700.0], sl=4710.0))
    assert dec.accept is True
    assert dec.action == "reduce_then_hedge"
    assert dec.lot_scale == 0.5                      # the new hedge is downsized
    assert dec.opposite_exposure_before == 1.0
    assert dec.opposite_exposure_after == 0.5        # old side cut in half
    # the old BUY survives at half lot (window still open).
    assert p._active[0].end > sell.signal_time_chart


def test_profit_bank_rearm_banks_a_profitable_buy_and_keeps_it_rearmable():
    p = CollisionPolicy.maybe(_cfg(opposite_signal_policy="profit_bank_rearm",
                                   opposite_profit_threshold_r=0.5))
    # BUY entry 4700, SL 4690 -> 1R = 10 price. Mark at 4715 -> +1.5R (>= 0.5).
    _register(p, "BUY1", "BUY", "2026-06-01 10:00", entries=[4700.0], sl=4690.0, lot=1.0)
    sell = _sig("SELL1", "SELL", "2026-06-01 10:10", rh=4700.0, rl=4700.0, sl=4710.0)
    dec = p.decide(sell, _rows(entries=[4700.0], sl=4710.0),
                   price_at=lambda t: 4715.0)
    assert dec.accept is True
    assert dec.action == "profit_bank_rearm"
    assert dec.old_side_pnl_delta != 0.0             # the bank booked a delta
    assert p._active[0].rearmable is True
    assert p._active[0].terminal is False            # banked, not engine-finished
    assert p.summary()["opposite_collisions_profit_bank_rearmed"] == 1


def test_profit_bank_rearm_hedges_when_old_side_not_profitable_enough():
    p = CollisionPolicy.maybe(_cfg(opposite_signal_policy="profit_bank_rearm",
                                   opposite_profit_threshold_r=0.5))
    _register(p, "BUY1", "BUY", "2026-06-01 10:00", entries=[4700.0], sl=4690.0, lot=1.0)
    sell = _sig("SELL1", "SELL", "2026-06-01 10:10", rh=4700.0, rl=4700.0, sl=4710.0)
    # mark at 4702 -> +0.2R < 0.5 -> do NOT bank, hedge instead.
    dec = p.decide(sell, _rows(entries=[4700.0], sl=4710.0),
                   price_at=lambda t: 4702.0)
    assert dec.accept is True
    assert dec.action == "allow"
    assert dec.old_side_pnl_delta == 0.0
    assert p.summary()["opposite_collisions_allowed"] == 1


# --- opposite-side: SELL active + BUY (mirror) ------------------------------

def test_reject_opposite_mirror_sell_active_buy_rejected():
    p = CollisionPolicy.maybe(_cfg(opposite_signal_policy="reject_opposite"))
    _register(p, "SELL1", "SELL", "2026-06-01 10:00", entries=[4700.0], sl=4710.0)
    buy = _sig("BUY1", "BUY", "2026-06-01 10:10", rh=4700.0, rl=4700.0, sl=4690.0)
    dec = p.decide(buy, _rows(entries=[4700.0], sl=4690.0, fill=None, status="PENDING"))
    assert dec.accept is False
    assert dec.action == "reject_opposite"


def test_profit_bank_rearm_mirror_banks_a_profitable_sell():
    p = CollisionPolicy.maybe(_cfg(opposite_signal_policy="profit_bank_rearm",
                                   opposite_profit_threshold_r=0.5))
    # SELL entry 4700, SL 4710 -> 1R = 10. Mark at 4685 -> +1.5R for a SELL.
    _register(p, "SELL1", "SELL", "2026-06-01 10:00", entries=[4700.0], sl=4710.0, lot=1.0)
    buy = _sig("BUY1", "BUY", "2026-06-01 10:10", rh=4700.0, rl=4700.0, sl=4690.0)
    dec = p.decide(buy, _rows(entries=[4700.0], sl=4690.0),
                   price_at=lambda t: 4685.0)
    assert dec.accept is True
    assert dec.action == "profit_bank_rearm"
    assert p._active[0].rearmable is True


# --- same-side overlap ------------------------------------------------------

def test_reject_overlap_skips_the_second_same_side_buy():
    p = CollisionPolicy.maybe(_cfg(same_side_overlap_policy="reject_overlap"))
    _register(p, "BUY1", "BUY", "2026-06-01 10:00", entries=[4700.0], sl=4694.0)
    buy2 = _sig("BUY2", "BUY", "2026-06-01 10:05", rh=4699.0, rl=4699.0, sl=4693.0)
    dec = p.decide(buy2, _rows(entries=[4699.0], sl=4693.0))
    assert dec.accept is False
    assert dec.collision_type == "same_side"
    assert dec.action == "reject_overlap"
    assert dec.cluster_id == "C1"
    assert p.summary()["same_side_clusters_rejected"] == 1


def test_better_entry_only_rejects_4699_after_4700_with_gap_5():
    p = CollisionPolicy.maybe(_cfg(
        same_side_overlap_policy="scale_in_better_entry_only",
        same_side_cluster_entry_gap=5.0, max_cluster_risk_multiple=10.0))
    _register(p, "BUY1", "BUY", "2026-06-01 10:00", entries=[4700.0], sl=4694.0)
    buy2 = _sig("BUY2", "BUY", "2026-06-01 10:05", rh=4699.0, rl=4699.0, sl=4693.0)
    dec = p.decide(buy2, _rows(entries=[4699.0], sl=4693.0))
    assert dec.accept is False                       # 4699 is not 5 lower than 4700
    assert dec.action == "scale_in_rejected"


def test_better_entry_only_allows_4690_after_4700_when_risk_cap_allows():
    p = CollisionPolicy.maybe(_cfg(
        same_side_overlap_policy="scale_in_better_entry_only",
        same_side_cluster_entry_gap=5.0, max_cluster_risk_multiple=2.0))
    # anchor risk = |4700-4694|*1*100 = $600; cap = 2x = $1200.
    _register(p, "BUY1", "BUY", "2026-06-01 10:00", entries=[4700.0], sl=4694.0, lot=1.0)
    buy2 = _sig("BUY2", "BUY", "2026-06-01 10:05", rh=4690.0, rl=4690.0, sl=4684.0)
    dec = p.decide(buy2, _rows(entries=[4690.0], sl=4684.0, lot=1.0))   # risk $600
    assert dec.accept is True                        # 4690 <= 4700-5 and 600+600 <= 1200
    assert dec.action == "scale_in_allowed"
    assert dec.cluster_risk_before == 600.0
    assert dec.cluster_risk_after == 1200.0


def test_better_entry_only_rejects_4690_when_risk_cap_too_tight():
    p = CollisionPolicy.maybe(_cfg(
        same_side_overlap_policy="scale_in_better_entry_only",
        same_side_cluster_entry_gap=5.0, max_cluster_risk_multiple=1.0))
    _register(p, "BUY1", "BUY", "2026-06-01 10:00", entries=[4700.0], sl=4694.0, lot=1.0)
    buy2 = _sig("BUY2", "BUY", "2026-06-01 10:05", rh=4690.0, rl=4690.0, sl=4684.0)
    dec = p.decide(buy2, _rows(entries=[4690.0], sl=4684.0, lot=1.0))
    assert dec.accept is False                       # entry better, but 1200 > 600 cap
    assert dec.action == "scale_in_rejected"


def test_better_entry_only_mirror_sell_requires_higher_entry():
    p = CollisionPolicy.maybe(_cfg(
        same_side_overlap_policy="scale_in_better_entry_only",
        same_side_cluster_entry_gap=5.0, max_cluster_risk_multiple=10.0))
    _register(p, "SELL1", "SELL", "2026-06-01 10:00", entries=[4700.0], sl=4706.0)
    # a SELL 4701 is only +1 -> rejected; a SELL 4710 is +10 -> allowed.
    near = _sig("SELL2", "SELL", "2026-06-01 10:05", rh=4701.0, rl=4701.0, sl=4707.0)
    assert p.decide(near, _rows(entries=[4701.0], sl=4707.0)).accept is False
    far = _sig("SELL3", "SELL", "2026-06-01 10:06", rh=4710.0, rl=4710.0, sl=4716.0)
    assert p.decide(far, _rows(entries=[4710.0], sl=4716.0)).accept is True


def test_fixed_risk_downsizes_the_scale_in_to_the_cluster_cap():
    p = CollisionPolicy.maybe(_cfg(
        same_side_overlap_policy="scale_in_fixed_risk",
        max_cluster_risk_multiple=1.5))
    # anchor risk $600, cap = 1.5x = $900 -> $300 budget left for the scale-in.
    _register(p, "BUY1", "BUY", "2026-06-01 10:00", entries=[4700.0], sl=4694.0, lot=1.0)
    buy2 = _sig("BUY2", "BUY", "2026-06-01 10:05", rh=4699.0, rl=4699.0, sl=4693.0)
    dec = p.decide(buy2, _rows(entries=[4699.0], sl=4693.0, lot=1.0))   # full risk $600
    assert dec.accept is True
    assert dec.action == "scale_in_downsized"
    assert dec.lot_scale == 0.5                       # 300/600 -> half size
    assert p.summary()["same_side_clusters_downsized"] == 1


def test_fixed_risk_rejects_when_downsize_falls_below_min_lot():
    p = CollisionPolicy.maybe(_cfg(
        same_side_overlap_policy="scale_in_fixed_risk",
        max_cluster_risk_multiple=1.0))
    # cap == anchor risk -> 0 budget -> scale 0 -> below min lot -> reject.
    _register(p, "BUY1", "BUY", "2026-06-01 10:00", entries=[4700.0], sl=4694.0, lot=1.0)
    buy2 = _sig("BUY2", "BUY", "2026-06-01 10:05", rh=4699.0, rl=4699.0, sl=4693.0)
    dec = p.decide(buy2, _rows(entries=[4699.0], sl=4693.0, lot=1.0))
    assert dec.accept is False
    assert dec.action == "scale_in_rejected"


def test_fixed_risk_allows_full_size_when_within_budget():
    p = CollisionPolicy.maybe(_cfg(
        same_side_overlap_policy="scale_in_fixed_risk",
        max_cluster_risk_multiple=3.0))
    _register(p, "BUY1", "BUY", "2026-06-01 10:00", entries=[4700.0], sl=4694.0, lot=1.0)
    buy2 = _sig("BUY2", "BUY", "2026-06-01 10:05", rh=4699.0, rl=4699.0, sl=4693.0)
    dec = p.decide(buy2, _rows(entries=[4699.0], sl=4693.0, lot=1.0))
    assert dec.accept is True
    assert dec.action == "scale_in_allowed"
    assert dec.lot_scale == 1.0


def test_cluster_window_excludes_a_far_apart_same_side_signal():
    p = CollisionPolicy.maybe(_cfg(same_side_overlap_policy="reject_overlap",
                                   same_side_cluster_window_minutes=30))
    _register(p, "BUY1", "BUY", "2026-06-01 10:00", entries=[4700.0], sl=4694.0)
    # 40 minutes later (> 30 window) -> not a cluster member -> accepted.
    buy2 = _sig("BUY2", "BUY", "2026-06-01 10:40", rh=4699.0, rl=4699.0, sl=4693.0)
    dec = p.decide(buy2, _rows(entries=[4699.0], sl=4693.0, fill="2026-06-01 10:40"))
    assert dec.accept is True
    assert dec.collision_type == ""


# --- re-arm no-chase + terminal constraints ---------------------------------

def test_profit_bank_rearm_never_reopens_buy_above_original_entry():
    assert can_rearm("BUY", 4700.0, 4700.0, terminal=False) is True    # at original
    assert can_rearm("BUY", 4700.0, 4699.5, terminal=False) is True    # better
    assert can_rearm("BUY", 4700.0, 4700.5, terminal=False) is False   # would chase up


def test_profit_bank_rearm_never_reopens_sell_below_original_entry():
    assert can_rearm("SELL", 4700.0, 4700.0, terminal=False) is True   # at original
    assert can_rearm("SELL", 4700.0, 4700.5, terminal=False) is True   # better
    assert can_rearm("SELL", 4700.0, 4699.5, terminal=False) is False  # would chase down


def test_sl_tp_engine_close_remains_terminal():
    for st in ("SL", "TP1", "TP2", "TP3", "LOCK_TP1", "LOCK_TP2",
               "TIME_EXIT", "TRAILING_STOP", "BEP"):
        assert status_is_terminal(st) is True
    for st in ("OPEN", "PENDING", "NO_FILL", ""):
        assert status_is_terminal(st) is False
    # a terminal signal can never be re-armed, even at a better price.
    assert can_rearm("BUY", 4700.0, 4690.0, terminal=True) is False


def test_original_sl_touched_remains_terminal():
    # a leg whose exit went through the ORIGINAL SL is terminal even if its
    # status string is not itself a system terminal.
    buy_through_sl = [{"entry_status": "OPEN", "exit_price": 4689.0}]
    assert CollisionPolicy._signal_terminal(buy_through_sl, "BUY", 4690.0) is True
    sell_through_sl = [{"entry_status": "OPEN", "exit_price": 4711.0}]
    assert CollisionPolicy._signal_terminal(sell_through_sl, "SELL", 4710.0) is True
    # an exit that stayed on the safe side of the SL is not terminal from that.
    buy_safe = [{"entry_status": "OPEN", "exit_price": 4705.0}]
    assert CollisionPolicy._signal_terminal(buy_safe, "BUY", 4690.0) is False


def test_register_marks_a_stopped_out_signal_terminal():
    p = CollisionPolicy.maybe(_cfg(opposite_signal_policy="reject_opposite"))
    _register(p, "BUY1", "BUY", "2026-06-01 10:00", entries=[4700.0], sl=4690.0,
              exit="2026-06-01 10:30", status="SL", pnl=-1000.0)
    assert p._active[0].terminal is True


# --- old-side P&L accounting (regression for the review findings) -----------

def _register_legs(policy, key, side, t, legs):
    """Register an active signal from explicit leg dicts (each: entry_price,
    effective_SL, lot, fill_time, exit_time, status, trading_pnl)."""
    dt = datetime.fromisoformat(t)
    entries = [leg["entry_price"] for leg in legs]
    sig = types.SimpleNamespace(
        signal_key=key, side=side, signal_time_chart=dt, signal_time_source=dt,
        range_high=max(entries), range_low=min(entries), sl=legs[0]["effective_SL"])
    rows = [{**leg, "exit_price": None} for leg in legs]
    policy.register(sig, {"entry_rows": rows}, policy.decide(sig, rows))
    return sig


def test_repeated_reduce_does_not_double_subtract_natural_pnl():
    # A still-open old leg reduced twice must recompute `natural` off its
    # REMAINING P&L, not the full original (the leg["trading_pnl"] re-scale fix).
    p = CollisionPolicy.maybe(_cfg(opposite_signal_policy="reduce_then_hedge",
                                   hedge_lot_fraction=0.5))
    _register_legs(p, "SELL1", "SELL", "2026-06-01 10:00", [
        {"entry_price": 4800.0, "effective_SL": 4810.0, "lot": 0.10,
         "fill_time": datetime.fromisoformat("2026-06-01 10:00"),
         "exit_time": None, "entry_status": "OPEN", "trading_pnl": 300.0}])
    b1 = _sig("BUY1", "BUY", "2026-06-01 10:05", rh=4790.0, rl=4790.0, sl=4780.0)
    p.decide(b1, _rows(entries=[4790.0], sl=4780.0), price_at=lambda t: 4790.0)
    b2 = _sig("BUY2", "BUY", "2026-06-01 10:10", rh=4780.0, rl=4780.0, sl=4770.0)
    p.decide(b2, _rows(entries=[4780.0], sl=4770.0), price_at=lambda t: 4780.0)
    # delta1 = 50 - 150 = -100 ; delta2 (off the remaining 150) = 50 - 75 = -25
    assert abs(p.summary()["collision_policy_pnl"] - (-125.0)) < 1e-6


def test_full_close_reverses_phantom_pnl_of_a_leg_that_fills_after_t():
    # close_then_flip cancels the old side's still-resting orders; a leg that
    # only fills AFTER the flip must have its natural P&L reversed, not kept.
    p = CollisionPolicy.maybe(_cfg(opposite_signal_policy="close_then_flip"))
    _register_legs(p, "SELL1", "SELL", "2026-06-01 10:00", [
        {"entry_price": 4800.0, "effective_SL": 4810.0, "lot": 0.05,
         "fill_time": datetime.fromisoformat("2026-06-01 10:00"),
         "exit_time": None, "entry_status": "OPEN", "trading_pnl": 300.0},
        {"entry_price": 4805.0, "effective_SL": 4815.0, "lot": 0.05,
         "fill_time": datetime.fromisoformat("2026-06-01 10:30"),    # fills AFTER t
         "exit_time": datetime.fromisoformat("2026-06-01 11:00"),
         "entry_status": "TP1", "trading_pnl": 200.0}])
    buy = _sig("BUY1", "BUY", "2026-06-01 10:10", rh=4790.0, rl=4790.0, sl=4780.0)
    dec = p.decide(buy, _rows(entries=[4790.0], sl=4780.0), price_at=lambda t: 4790.0)
    # leg1 open: close_now 50 - natural 300 = -250 ; leg2 (fills after t): -200
    assert abs(dec.old_side_pnl_delta - (-450.0)) < 1e-6


def test_same_side_reject_does_not_apply_a_staged_opposite_flip():
    # A book holding BOTH directions: a new BUY is opposite to an active SELL
    # (close_then_flip) AND same-side to an active BUY (reject_overlap). The
    # same-side reject must win WITHOUT the opposite flip leaking through.
    p = CollisionPolicy.maybe(_cfg(opposite_signal_policy="close_then_flip",
                                   same_side_overlap_policy="reject_overlap"))
    # Seed BOTH directions directly into the active set (register, no decide), so
    # the policy hasn't already flipped one side when the new BUY arrives.
    def _seed(key, side, t, entries, sl):
        dt = datetime.fromisoformat(t)
        sig = _sig(key, side, t, rh=max(entries), rl=min(entries), sl=sl)
        p.register(sig, {"entry_rows": _rows(entries=entries, sl=sl, fill=t)},
                   CollisionDecision())
    _seed("SELL1", "SELL", "2026-06-01 10:00", [4800.0], 4810.0)
    _seed("BUY1", "BUY", "2026-06-01 10:02", [4700.0], 4694.0)
    new = _sig("BUY2", "BUY", "2026-06-01 10:05", rh=4699.0, rl=4699.0, sl=4693.0)
    dec = p.decide(new, _rows(entries=[4699.0], sl=4693.0), price_at=lambda t: 4799.0)
    assert dec.accept is False
    assert dec.action == "reject_overlap"
    s = p.summary()
    assert s["opposite_collisions_flipped"] == 0          # the flip did NOT happen
    assert s["opposite_collisions_total"] == 1            # but the collision was seen
    assert s["same_side_clusters_rejected"] == 1
    # the old SELL is untouched: still active, not retired, not terminal.
    old_sell = next(a for a in p._active if a.signal_key == "SELL1")
    assert old_sell.end > new.signal_time_chart
    assert old_sell.terminal is False


def test_run_backtest_collision_pnl_reconciles_trading_plus_bonus(tmp_path):
    # On a run that books an old-side delta, realized_pnl must still equal
    # trading_pnl + bonus (the delta is folded into trading_pnl, not lost).
    from trading.engine import run_backtest, parse_one_signal, CsvChartSource
    rows = [_bar("2026.06.02", f"{10 + (i // 60):02d}:{i % 60:02d}:00",
                 100 + i * 0.1, 100 + i * 0.1 + 0.2, 100 + i * 0.1 - 0.2, 100 + i * 0.1)
            for i in range(60)]
    p = tmp_path / "rise.csv"
    p.write_text("\n".join([_HEADER, *rows]) + "\n", encoding="utf-8")
    chart = CsvChartSource([p])
    buy = parse_one_signal("1. BUY XAUUSD 100 - 100 SL 90 TP1 130 TP2 140 TP3 150 10:00 AM",
                           source_date="2026-06-02", source_offset=3)
    sell = parse_one_signal("2. SELL XAUUSD 105 - 105 SL 115 TP1 95 TP2 85 TP3 75 10:20 AM",
                            source_date="2026-06-02", source_offset=3)
    res = run_backtest([buy, sell], chart,
                       StrategyConfig(opposite_signal_policy="close_then_flip", **_GEO))
    assert res["collision_policy"]["collision_policy_pnl"] != 0.0     # a delta was booked
    assert abs(res["realized_pnl"] - (res["trading_pnl"] + res["bonus"])) < 1e-6


# --- run_backtest integration (parity + only-removes) -----------------------

_HEADER = "<DATE>\t<TIME>\t<OPEN>\t<HIGH>\t<LOW>\t<CLOSE>\t<TICKVOL>\t<VOL>\t<SPREAD>"


def _bar(d, t, o, h, l, c):
    return f"{d}\t{t}\t{o}\t{h}\t{l}\t{c}\t100.0\t0.0\t2"


def _flat_chart(tmp_path):
    from trading.engine import CsvChartSource
    rows = [_bar("2026.06.02", f"{10 + (i // 60):02d}:{i % 60:02d}:00",
                 100, 100.2, 99.9, 100) for i in range(40)]
    p = tmp_path / "C.csv"
    p.write_text("\n".join([_HEADER, *rows]) + "\n", encoding="utf-8")
    return CsvChartSource([p])


def _two_opposing_signals():
    from trading.engine import parse_one_signal
    buy = parse_one_signal(
        "1. BUY XAUUSD 100 - 100 SL 90 TP1 110 TP2 120 TP3 130 10:00 AM",
        source_date="2026-06-02", source_offset=3)
    sell = parse_one_signal(
        "2. SELL XAUUSD 100 - 100 SL 110 TP1 90 TP2 80 TP3 70 10:10 AM",
        source_date="2026-06-02", source_offset=3)
    return [buy, sell]


_GEO = dict(initial_capital=5000.0, sizing_mode="risk", risk_per_signal=0.01,
            minimum_lot=0.01, entry_count=1, entry_ladder="range_uniform",
            sl_multiplier=1.0, activation_delay_minutes=0,
            pending_expiry_minutes=60, max_hold_minutes=60)


def test_run_backtest_collision_off_is_byte_identical(tmp_path):
    from trading.engine import run_backtest
    sigs = _two_opposing_signals()
    chart = _flat_chart(tmp_path)
    plain = run_backtest(sigs, chart, StrategyConfig(**_GEO))
    baseline = run_backtest(list(_two_opposing_signals()), _flat_chart(tmp_path),
                            StrategyConfig(opposite_signal_policy="allow_hedge",
                                           same_side_overlap_policy="allow_all", **_GEO))
    assert plain["net_profit"] == baseline["net_profit"]
    assert plain["signals_included"] == baseline["signals_included"]
    assert len(plain["entry_rows"]) == len(baseline["entry_rows"])
    assert "collision_policy" not in plain
    assert "collision_policy" not in baseline


def test_run_backtest_reject_opposite_only_removes_the_opposite(tmp_path):
    from trading.engine import run_backtest
    base = run_backtest(_two_opposing_signals(), _flat_chart(tmp_path),
                        StrategyConfig(**_GEO))
    gated = run_backtest(_two_opposing_signals(), _flat_chart(tmp_path),
                         StrategyConfig(opposite_signal_policy="reject_opposite", **_GEO))
    base_keys = {r["signal_key"] for r in base["rows"]}
    gated_keys = {r["signal_key"] for r in gated["rows"]}
    assert gated_keys <= base_keys                      # never invents a signal
    assert gated["signals_included"] == base["signals_included"] - 1
    assert gated["collision_policy"]["opposite_collisions_rejected"] == 1
    # every surviving row carries the collision reporting fields.
    assert all("collision_type" in r for r in gated["rows"])
