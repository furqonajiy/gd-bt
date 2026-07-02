"""Structural tests for the Victor/V017 trailing-geometry staged sweep.

Pin the curated grid (all required labels, base_v017 baseline, single-knob
perturbations, no Cartesian explosion) and the results schema — all via
``--skeleton`` so no MT5/charts/backtests are needed.
"""
from __future__ import annotations

import csv

import pytest

from tools.sweep_victor_trailing_geometry import (
    MIN_TRAILING_DISTANCE, RESULTS_COLUMNS, V017_BASELINE, _candidate,
    build_candidate_grid, main,
)

REQUIRED_LABELS = {
    "base_v017",
    "open_0_50", "open_0_75", "open_1_00",
    "close_0_50_stage1", "close_0_75_stage1", "close_0_50_stage2",
    "slm_1_60", "slm_1_70", "slm_1_80",
    "hold_120", "hold_150", "hold_180",
    "expiry_120", "expiry_180",
    "entry_gap_0_50", "entry_gap_0_70",
    "tp2_final", "tp3_final",
}

# Live-executability floor: ELEV8 rejects a resting stop under the ~0.4
# min-stop (retcode 10015), so no sweep candidate may carry a trailing
# distance in (0, MIN_TRAILING_DISTANCE). Operator rule 2026-07-02.
FORBIDDEN_SUB_MIN_LABELS = {"open_0_25", "close_0_25_stage1"}

REQUIRED_COLUMNS = {
    "label", "window", "window_start", "window_end",
    "entries", "entry_sl_gap", "sl_multiplier", "pending_expiry", "max_hold",
    "final_target", "tp1_lock_delay_minutes", "tp2_lock_delay_minutes",
    "tp1_lock_fraction", "trailing_open_distance", "trailing_close_distance",
    "trailing_close_after_stage",
    "pure_trading_pnl", "rebate_pnl", "net_pnl", "closed_lots",
    "pure_pnl_per_lot", "net_pnl_per_lot",
    "max_drawdown_pct", "win_rate_pct", "signals_included",
    "tick_signals", "m1_signals", "open_or_pending_left", "included_in_ranking",
    "score", "guard_reason",
}


def test_grid_contains_all_required_labels_base_first():
    grid = build_candidate_grid("full_recent")
    labels = [c["label"] for c in grid]
    assert labels[0] == "base_v017"                 # baseline always first
    assert REQUIRED_LABELS <= set(labels)
    assert len(labels) == len(set(labels))          # no duplicates
    assert not (FORBIDDEN_SUB_MIN_LABELS & set(labels))


def test_no_candidate_carries_sub_min_trailing_distance():
    """Every grid candidate is live-executable: no trailing distance in
    (0, MIN_TRAILING_DISTANCE) — the broker rejects that resting stop."""
    for mode in ("smoke", "full_recent"):
        for c in build_candidate_grid(mode):
            for key in ("trailing_open_distance", "trailing_close_distance"):
                dist = float(c[key])
                assert dist == 0.0 or dist >= MIN_TRAILING_DISTANCE, (
                    f"{c['label']}: {key}={dist} below the live min-stop floor")


def test_candidate_guard_rejects_sub_min_trailing_distances():
    with pytest.raises(ValueError, match="MIN_TRAILING_DISTANCE"):
        _candidate("bad_open", trailing_open_distance=0.25)
    with pytest.raises(ValueError, match="MIN_TRAILING_DISTANCE"):
        _candidate("bad_close", trailing_close_distance=0.25)
    with pytest.raises(ValueError, match="MIN_TRAILING_DISTANCE"):
        _candidate("bad_edge", trailing_open_distance=0.4)
    # 0 = disabled and >= MIN_TRAILING_DISTANCE both stay allowed
    assert _candidate("ok_off", trailing_open_distance=0.0)["trailing_open_distance"] == 0.0
    assert _candidate("ok_min", trailing_close_distance=0.5)["trailing_close_distance"] == 0.5


def test_base_v017_has_expected_baseline_parameters():
    grid = build_candidate_grid("full_recent")
    base = next(c for c in grid if c["label"] == "base_v017")
    for k, v in V017_BASELINE.items():
        assert base[k] == v
    # spot-check the headline geometry the task specified
    assert base["entries"] == 8 and base["entry_ladder"] == "range_to_sl"
    assert base["sl_multiplier"] == 1.7 and base["final_target"] == "TP2"
    assert base["trailing_open_distance"] == 0.5
    assert base["trailing_close_distance"] == 0.5
    assert base["trailing_close_after_stage"] == 1


def test_each_candidate_changes_only_intended_parameters():
    """Every candidate is a single controlled step (<=2 knobs) from baseline —
    proves it is NOT a Cartesian blow-up."""
    grid = build_candidate_grid("full_recent")
    for c in grid:
        diff = {k for k in V017_BASELINE if c[k] != V017_BASELINE[k]}
        assert len(diff) <= 2, f"{c['label']} changed too many knobs: {diff}"
    # the specific perturbation each label claims to make
    by = {c["label"]: c for c in grid}
    assert by["open_0_75"]["trailing_open_distance"] == 0.75
    assert by["close_0_50_stage2"]["trailing_close_after_stage"] == 2
    assert by["slm_1_80"]["sl_multiplier"] == 1.80
    assert by["hold_180"]["max_hold"] == 180
    assert by["expiry_120"]["pending_expiry"] == 120
    assert by["entry_gap_0_70"]["entry_sl_gap"] == 0.70
    assert by["tp3_final"]["final_target"] == "TP3"


def test_no_cartesian_explosion():
    # a curated staged grid is ~21 candidates, not hundreds/thousands
    assert len(build_candidate_grid("full_recent")) <= 30
    # smoke is a small representative subset
    smoke = [c["label"] for c in build_candidate_grid("smoke")]
    assert smoke[0] == "base_v017" and 2 <= len(smoke) <= 6


def test_skeleton_writes_required_schema(tmp_path):
    rc = main(["--mode", "full_recent", "--skeleton", "--out-root", str(tmp_path)])
    assert rc == 0
    out_dir = tmp_path / "VICTOR_TRAILING_full_recent"
    assert (out_dir / "results.csv").exists()
    assert (out_dir / "top_candidates.json").exists()
    assert (out_dir / "summary.md").exists()
    with (out_dir / "results.csv").open(encoding="utf-8") as f:
        reader = csv.DictReader(f)
        cols = list(reader.fieldnames or [])
        rows = list(reader)
    assert cols == RESULTS_COLUMNS
    assert REQUIRED_COLUMNS <= set(cols)
    assert {r["label"] for r in rows} >= REQUIRED_LABELS
    # geometry columns are populated even in skeleton mode
    base = next(r for r in rows if r["label"] == "base_v017")
    assert base["trailing_open_distance"] == "0.5"
    assert base["final_target"] == "TP2"


def test_alias_output_dir_and_smoke_paths(tmp_path):
    rc = main(["--mode", "smoke", "--skeleton", "--output-dir", str(tmp_path)])
    assert rc == 0
    assert (tmp_path / "VICTOR_TRAILING_smoke" / "results.csv").exists()
