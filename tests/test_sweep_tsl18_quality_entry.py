"""Tests for the TSL18 quality-entry + collision-policy sweep.

The sweep must be runnable without MT5/charts (via --skeleton) and must emit a
results.csv with the full required schema (now including the REAL collision-policy
columns + metrics), a top_candidates.json, and a summary.md. These tests pin that
contract, the grid (quality + collision families), the collision-flag rendering,
and that validate_top preserves a candidate's collision config from a prior run.
"""
from __future__ import annotations

import csv
import json

from tools.sweep_tsl18_quality_entry import (
    COLLISION_DEFAULTS, RESULTS_COLUMNS, _candidate, _collision_flags,
    build_candidate_grid, main,
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
    # REAL collision-policy columns + metrics
    "opposite_signal_policy", "same_side_overlap_policy", "collision_policy_pnl",
    "opposite_collisions_total", "opposite_collisions_allowed",
    "opposite_collisions_rejected", "opposite_collisions_flipped",
    "opposite_collisions_profit_bank_rearmed",
    "same_side_clusters_total", "same_side_clusters_accepted",
    "same_side_clusters_rejected", "same_side_clusters_downsized",
    "max_same_side_cluster_risk", "max_opposite_exposure",
}


def _read_results(out_dir):
    """(fieldnames, rows) from a sweep output dir's results.csv."""
    with (out_dir / "results.csv").open(encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        return list(reader.fieldnames or []), rows


def test_results_columns_include_real_collision_metrics():
    # The placeholder columns are gone; the real collision metric columns are in.
    assert REQUIRED_COLUMNS <= set(RESULTS_COLUMNS)
    assert "collision_policy" not in RESULTS_COLUMNS          # old placeholder removed
    assert "collisions_detected" not in RESULTS_COLUMNS       # old placeholder removed
    assert "collision_pnl_delta" not in RESULTS_COLUMNS       # old placeholder removed


def test_smoke_skeleton_writes_required_columns_and_labels(tmp_path):
    rc = main(["--mode", "smoke", "--skeleton", "--out-root", str(tmp_path)])
    assert rc == 0
    out_dir = tmp_path / "TSL18_QUALITY_smoke"
    results = out_dir / "results.csv"
    top = out_dir / "top_candidates.json"
    summary = out_dir / "summary.md"
    assert results.exists() and top.exists() and summary.exists()

    fieldnames, rows = _read_results(out_dir)
    assert REQUIRED_COLUMNS <= set(fieldnames)
    assert fieldnames == RESULTS_COLUMNS                      # canonical order
    labels = {r["label"] for r in rows}
    assert {"base", "hybrid_quality", "profit_bank_rearm",
            "hybrid_quality_profit_bank_scale_better"} <= labels
    json.loads(top.read_text(encoding="utf-8"))              # valid JSON (a list)


def test_smoke_grid_has_a_non_baseline_collision_candidate():
    smoke = build_candidate_grid("smoke")
    assert smoke[0]["label"] == "base"
    assert smoke[0]["opposite_signal_policy"] == "allow_hedge"
    assert smoke[0]["same_side_overlap_policy"] == "allow_all"
    non_baseline = [c for c in smoke
                    if c["opposite_signal_policy"] != "allow_hedge"
                    or c["same_side_overlap_policy"] != "allow_all"]
    assert non_baseline, "smoke grid must include a non-baseline collision candidate"


def test_full_grid_covers_quality_and_collision_families():
    full = build_candidate_grid("full_june")
    assert full[0]["label"] == "base"
    labels = {c["label"] for c in full}
    # quality family
    assert {"trend_only", "reversal_extreme", "hybrid_quality",
            "high_frequency_quality", "extreme_support_demand",
            "extreme_supply_resistance", "extreme_both"} <= labels
    # collision-only family
    assert {"reject_opposite", "profit_bank_rearm", "same_reject_overlap",
            "same_scale_better_only", "same_scale_fixed_risk"} <= labels
    # combined family
    assert {"hybrid_quality_profit_bank", "hybrid_quality_scale_better",
            "hybrid_quality_profit_bank_scale_better",
            "hybrid_quality_extreme_both_profit_bank_scale_better"} <= labels
    # at least one candidate flips each collision axis off-baseline
    assert any(c["opposite_signal_policy"] != "allow_hedge" for c in full)
    assert any(c["same_side_overlap_policy"] != "allow_all" for c in full)


def test_collision_flags_render_only_for_non_baseline():
    # baseline -> no flags (so the backtest command stays byte-identical)
    assert _collision_flags(_candidate("base")) == []
    # opposite profit_bank_rearm renders its policy + threshold
    pb = _collision_flags(_candidate("pb", opposite_signal_policy="profit_bank_rearm",
                                     opposite_profit_threshold_r=0.5))
    assert "--opposite-signal-policy" in pb and "profit_bank_rearm" in pb
    assert "--opposite-profit-threshold-r" in pb
    # same-side scale_in_better_entry_only renders its gap + cap
    sb = _collision_flags(_candidate("sb",
                                     same_side_overlap_policy="scale_in_better_entry_only",
                                     same_side_cluster_entry_gap=5.0,
                                     max_cluster_risk_multiple=1.0))
    assert "--same-side-overlap-policy" in sb and "scale_in_better_entry_only" in sb
    assert "--same-side-cluster-entry-gap" in sb and "--max-cluster-risk-multiple" in sb


def test_alias_cli_full_skeleton_writes_full_june_outputs(tmp_path):
    # Automation aliases: --mode full -> full_june, --output-dir -> --out-root,
    # --require-full-lifecycle-ticks / --fail-on-open-or-pending / --rank-objective.
    rc = main(["--mode", "full", "--skeleton", "--output-dir", str(tmp_path),
               "--require-full-lifecycle-ticks", "--fail-on-open-or-pending",
               "--rank-objective", "edge_plus_rebate_guarded"])
    assert rc == 0
    out_dir = tmp_path / "TSL18_QUALITY_full_june"
    fieldnames, rows = _read_results(out_dir)
    assert fieldnames == RESULTS_COLUMNS
    assert rows and rows[0]["label"] == "base"
    assert all(r["score_objective"] == "edge_plus_rebate_guarded" for r in rows)


def test_alias_and_current_names_produce_equivalent_schema(tmp_path):
    cur = tmp_path / "cur"
    ali = tmp_path / "ali"
    assert main(["--mode", "full_june", "--skeleton", "--out-root", str(cur),
                 "--require-full-tick-lifecycle", "--exclude-open-or-pending",
                 "--score-objective", "edge_plus_rebate_guarded"]) == 0
    assert main(["--mode", "full", "--skeleton", "--output-dir", str(ali),
                 "--require-full-lifecycle-ticks", "--fail-on-open-or-pending",
                 "--rank-objective", "edge_plus_rebate_guarded"]) == 0
    f_cur, r_cur = _read_results(cur / "TSL18_QUALITY_full_june")
    f_ali, r_ali = _read_results(ali / "TSL18_QUALITY_full_june")
    assert f_cur == f_ali                                   # identical schema
    assert {r["label"] for r in r_cur} == {r["label"] for r in r_ali}  # identical grid


def test_validate_top_preserves_collision_fields(tmp_path):
    # A prior top record carrying a non-baseline collision config must be replayed
    # with that EXACT config (incl. the cluster sub-knobs that are JSON-only).
    top = tmp_path / "prior_top.json"
    top.write_text(json.dumps([
        {"label": "profit_bank_rearm", "quality_profile": "off",
         "min_quality_score": 0.0, "extreme_entry_mode": "off",
         "opposite_signal_policy": "profit_bank_rearm",
         "same_side_overlap_policy": "allow_all",
         "opposite_profit_threshold_r": 0.5},
    ]), encoding="utf-8")
    rc = main(["--mode", "validate_top", "--skeleton", "--input-candidates", str(top),
               "--out-root", str(tmp_path)])
    assert rc == 0
    _, rows = _read_results(tmp_path / "TSL18_QUALITY_validate_top")
    assert [r["label"] for r in rows] == ["profit_bank_rearm"]
    assert rows[0]["opposite_signal_policy"] == "profit_bank_rearm"
    assert rows[0]["same_side_overlap_policy"] == "allow_all"


def test_candidate_helper_rejects_unknown_collision_keys():
    import pytest
    with pytest.raises(ValueError):
        _candidate("bad", not_a_real_collision_key=1)
    # and a baseline candidate carries all the default collision keys
    base = _candidate("base")
    assert set(COLLISION_DEFAULTS) <= set(base)


def test_hybrid_backtest_exports_collision_policy_symbol():
    """Workflow preflight already runs this file; keep the hybrid collision import
    regression here too so a missing root import fails before any long sweep starts."""
    import tools.backtest_hybrid as hybrid

    assert hybrid.CollisionPolicy is not None
