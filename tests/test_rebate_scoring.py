"""Tests for the rebate-aware scoring used by the TSL18 quality-entry sweep.

"Rebate" is the engine's $3/closed-lot bonus. These pin the metric math and,
crucially, that a candidate green ONLY on the rebate (bad/negative pure trading
P&L) is flagged and never wins the rebate-guarded objective.
"""
from __future__ import annotations

import pytest

from tools.rebate_scoring import (
    SCORE_OBJECTIVES, compute_rebate_metrics, passes_rebate_guards, score_candidate,
)


def test_rebate_metrics_math():
    m = compute_rebate_metrics(1000.0, 100.0, rebate_per_lot=3.0)
    assert m.pure_trading_pnl == 1000.0
    assert m.rebate_pnl == 300.0            # 100 lots * $3
    assert m.net_pnl == 1300.0
    assert m.closed_lots == 100.0
    assert m.pure_pnl_per_lot == 10.0
    assert m.net_pnl_per_lot == 13.0
    assert m.rebate_share_of_profit == pytest.approx(300.0 / 1300.0, abs=1e-4)


def test_rebate_pnl_override_is_used():
    # passing rebate_pnl directly (e.g. read from a workbook) overrides lots*rate
    m = compute_rebate_metrics(500.0, 50.0, rebate_per_lot=3.0, rebate_pnl=120.0)
    assert m.rebate_pnl == 120.0 and m.net_pnl == 620.0


def test_zero_closed_lots_no_divide():
    m = compute_rebate_metrics(0.0, 0.0)
    assert m.pure_pnl_per_lot == 0.0 and m.net_pnl_per_lot == 0.0
    assert m.rebate_share_of_profit == 0.0


def test_negative_pure_positive_net_is_flagged():
    # rebate-farm: pure trading P&L is negative, rebate drags net positive
    m = compute_rebate_metrics(-100.0, 100.0, rebate_per_lot=3.0)
    assert m.net_pnl == 200.0 and m.net_pnl > 0
    assert m.rebate_share_of_profit == 1.0          # all "profit" is rebate
    ok, reason = passes_rebate_guards(m, min_pure_trading_pnl=0.0)
    assert not ok and reason == "pure_pnl_below_min"
    # the guarded objective scores it on its (negative) pure edge, so it loses
    assert score_candidate(m, "edge_plus_rebate_guarded") == -100.0


def test_high_rebate_share_is_flagged():
    # pure positive but tiny; rebate dominates -> share over the cap -> rejected
    m = compute_rebate_metrics(10.0, 100.0, rebate_per_lot=3.0)   # rebate 300, net 310
    ok, reason = passes_rebate_guards(m, min_pure_trading_pnl=0.0,
                                      max_rebate_share_of_profit=0.50)
    assert not ok and reason == "rebate_share_too_high"


def test_healthy_candidate_passes_guards():
    m = compute_rebate_metrics(2000.0, 100.0, rebate_per_lot=3.0)  # rebate 300, net 2300
    ok, reason = passes_rebate_guards(m, min_pure_trading_pnl=0.0,
                                      max_rebate_share_of_profit=0.50)
    assert ok and reason == "ok"


def test_score_objectives():
    m = compute_rebate_metrics(1000.0, 100.0, rebate_per_lot=3.0)  # net 1300
    assert score_candidate(m, "net_pnl") == 1300.0
    assert score_candidate(m, "pure_pnl") == 1000.0
    assert score_candidate(m, "edge_plus_rebate_guarded") == 1300.0   # guards pass
    # dd_adjusted_net penalises by drawdown
    assert score_candidate(m, "dd_adjusted_net", max_drawdown_pct=30.0) == pytest.approx(1300.0 / 1.3)


def test_unknown_objective_raises():
    m = compute_rebate_metrics(1.0, 1.0)
    with pytest.raises(ValueError):
        score_candidate(m, "not_an_objective")
    assert "edge_plus_rebate_guarded" in SCORE_OBJECTIVES
