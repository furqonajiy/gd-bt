"""CI-safe tests for tools/sweep_self_limit.py pure helpers (no engine/data)."""
from __future__ import annotations

import sys
from pathlib import Path

from pytest import approx

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from tools.sweep_self_limit import make_limit_candidates, _monthly_stats  # noqa: E402


def test_all_candidates_are_limit_only():
    cands = make_limit_candidates(seed=7, max_candidates=40)
    assert cands, "expected candidates"
    assert all(c["trailing_open_distance"] == 0.0 for c in cands)
    assert len(cands) <= 40


def test_candidates_are_deduped():
    cands = make_limit_candidates(seed=7, max_candidates=40)
    serialized = {tuple(sorted(c.items(), key=lambda kv: kv[0])) for c in cands}
    assert len(serialized) == len(cands)


def test_monthly_stats_counts_positive_months_and_worst():
    monthly = [
        {"month": "2025-01", "trading_pnl": 10.0},
        {"month": "2025-02", "trading_pnl": -5.0},
        {"month": "2025-03", "trading_pnl": 3.0},
        {"month": "2025-04", "trading_pnl": 0.0},   # zero is not "profitable"
    ]
    s = _monthly_stats(monthly)
    assert s["total_months"] == 4
    assert s["stable_months"] == 2          # 10 and 3
    assert s["stable_fraction"] == approx(0.5)
    assert s["worst_month"] == approx(-5.0)


def test_monthly_stats_empty():
    s = _monthly_stats([])
    assert s["total_months"] == 0
    assert s["stable_months"] == 0
    assert s["stable_fraction"] == 0.0
    assert s["worst_month"] == 0.0