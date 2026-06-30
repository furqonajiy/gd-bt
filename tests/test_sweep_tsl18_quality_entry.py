"""Smoke tests for the TSL18 quality-entry sweep SKELETON.

The skeleton must be runnable without MT5/charts (via --skeleton) and must emit a
results.csv with the full required schema (including the placeholder collision
columns, which carry no logic in this branch), a top_candidates.json, and a
summary.md. These tests pin that contract and the gate/grid helpers.
"""
from __future__ import annotations

import csv
import json

from tools.sweep_tsl18_quality_entry import (
    RESULTS_COLUMNS, build_candidate_grid, main,
)

REQUIRED_COLUMNS = {
    # candidate identity + dimensions
    "label", "quality_profile", "min_quality_score", "extreme_entry_mode",
    "window", "window_start", "window_end",
    # rebate-aware metrics
    "pure_trading_pnl", "rebate_pnl", "net_pnl", "closed_lots",
    "pure_pnl_per_lot", "net_pnl_per_lot", "rebate_share_of_profit",
    # selection
    "score_objective", "score", "passes_rebate_guards", "rebate_guard_reason",
    # backtest context
    "max_drawdown_pct", "win_rate_pct", "signals_included",
    "tick_signals", "m1_signals", "data_source_mixed",
    # exclusion gates
    "partial_tick_lifecycle_excluded", "open_or_pending_left", "included_in_ranking",
    # placeholder collision metrics (NOT implemented in this branch)
    "collision_policy", "collisions_detected", "collision_pnl_delta",
}


def test_smoke_skeleton_writes_required_columns(tmp_path):
    rc = main(["--mode", "smoke", "--skeleton", "--out-root", str(tmp_path)])
    assert rc == 0
    out_dir = tmp_path / "TSL18_QUALITY_smoke"
    results = out_dir / "results.csv"
    top = out_dir / "top_candidates.json"
    summary = out_dir / "summary.md"
    assert results.exists() and top.exists() and summary.exists()

    with results.open(encoding="utf-8") as f:
        reader = csv.DictReader(f)
        cols = set(reader.fieldnames or [])
        rows = list(reader)
    # the schema must contain every required column, in the canonical order
    assert REQUIRED_COLUMNS <= cols
    assert list(reader.fieldnames) == RESULTS_COLUMNS
    # the smoke grid has base + two variants
    labels = {r["label"] for r in rows}
    assert {"base", "trend_only", "extreme_both"} <= labels
    # placeholder collision columns are present but empty (no logic in this branch)
    for r in rows:
        assert r["collision_policy"] == ""
        assert r["collisions_detected"] == ""
        assert r["collision_pnl_delta"] == ""

    json.loads(top.read_text(encoding="utf-8"))   # valid JSON (a list)


def test_candidate_grid_base_first_and_covers_profiles():
    smoke = build_candidate_grid("smoke")
    assert smoke[0]["label"] == "base"
    assert smoke[0]["quality_profile"] == "off"
    full = build_candidate_grid("full_june")
    assert full[0]["label"] == "base"
    profiles = {c["quality_profile"] for c in full}
    assert {"trend_only", "reversal_extreme", "hybrid_quality",
            "high_frequency_quality"} <= profiles
    extremes = {c["extreme_entry_mode"] for c in full}
    assert {"support_demand", "supply_resistance", "both"} <= extremes
