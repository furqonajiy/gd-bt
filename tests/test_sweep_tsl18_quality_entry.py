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


def _read_results(out_dir):
    """(fieldnames, rows) from a sweep output dir's results.csv."""
    with (out_dir / "results.csv").open(encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        return list(reader.fieldnames or []), rows


def test_alias_cli_full_skeleton_writes_full_june_outputs(tmp_path):
    # Automation aliases: --mode full -> full_june, --output-dir -> --out-root,
    # --require-full-lifecycle-ticks / --fail-on-open-or-pending / --rank-objective.
    rc = main(["--mode", "full", "--skeleton", "--output-dir", str(tmp_path),
               "--require-full-lifecycle-ticks", "--fail-on-open-or-pending",
               "--rank-objective", "edge_plus_rebate_guarded"])
    assert rc == 0
    # 'full' normalizes to full_june, so the subfolder is TSL18_QUALITY_full_june.
    out_dir = tmp_path / "TSL18_QUALITY_full_june"
    assert (out_dir / "results.csv").exists()
    assert (out_dir / "top_candidates.json").exists()
    assert (out_dir / "summary.md").exists()
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


def test_input_candidates_alias_drives_validate_top(tmp_path):
    # --input-candidates is the automation alias for --top-json (validate_top).
    top = tmp_path / "prior_top.json"
    top.write_text(json.dumps([
        {"label": "trend_only_s0.4", "quality_profile": "trend_only",
         "min_quality_score": 0.4, "extreme_entry_mode": "off"},
    ]), encoding="utf-8")
    rc = main(["--mode", "validate_top", "--skeleton", "--input-candidates", str(top),
               "--out-root", str(tmp_path)])
    assert rc == 0
    _, rows = _read_results(tmp_path / "TSL18_QUALITY_validate_top")
    assert [r["label"] for r in rows] == ["trend_only_s0.4"]


def test_start_end_date_override_window(tmp_path):
    # Explicit window overrides flow into the output window_start / window_end
    # (and --end-date stays EXCLUSIVE, just like the mode-default windows).
    rc = main(["--mode", "smoke", "--skeleton", "--out-root", str(tmp_path),
               "--start-date", "2026-06-10", "--end-date", "2026-06-20"])
    assert rc == 0
    _, rows = _read_results(tmp_path / "TSL18_QUALITY_smoke")
    assert rows
    assert all(r["window_start"] == "2026-06-10" for r in rows)
    assert all(r["window_end"] == "2026-06-20" for r in rows)


def test_default_window_unchanged_without_override(tmp_path):
    # Sanity: omitting the overrides keeps the mode's default window (smoke).
    rc = main(["--mode", "smoke", "--skeleton", "--out-root", str(tmp_path)])
    assert rc == 0
    _, rows = _read_results(tmp_path / "TSL18_QUALITY_smoke")
    assert rows and rows[0]["window_start"] == "2026-06-27"
    assert rows[0]["window_end"] == "2026-07-01"


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
