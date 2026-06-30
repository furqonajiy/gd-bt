"""Contract tests for the TWL25 loss-filter sweep (tools/sweep_tick_loss_filters.py).

Pins the sweep-integrity rules that the 2026-06 review surfaced:
  * the June phase window is bounded (cannot leak July/future signals);
  * the full grid is exactly 144 candidates;
  * unresolved exposure (open_or_pending_left) fails BOTH DD gates;
  * the leaderboard CSV surfaces open_or_pending_left + partial_tick_signals;
  * aggregate refuses to publish an incomplete grid or duplicate rows.
"""
from __future__ import annotations

import csv
import sys
from argparse import Namespace
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
for _p in (str(_REPO_ROOT), str(_REPO_ROOT / "tools")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import sweep_tick_loss_filters as s  # noqa: E402


# -- phase windows ----------------------------------------------------------

def test_june_phase_ends_exclusive_at_july_1():
    _charts, _ticks, start, end = s.phase_defaults("june")
    assert start == "2026-06-01"
    assert end == "2026-07-01"   # exclusive bound -> June run cannot include July


def test_jan_jun_phase_ends_exclusive_at_july_1():
    _charts, _ticks, start, end = s.phase_defaults("jan_jun")
    assert start == "2026-01-01"
    assert end == "2026-07-01"


# -- grid size --------------------------------------------------------------

def test_full_grid_is_144_unique_candidates():
    assert s.EXPECTED_FULL_GRID == 144
    cands = s.candidates()
    assert len(cands) == 144
    assert len({c["candidate_id"] for c in cands}) == 144


# -- strict gating on unresolved exposure -----------------------------------

def test_unresolved_exposure_fails_both_gates():
    # Great P&L / DD / win-rate, but ONE open/pending leg left -> not deployable.
    assert s.passes_gates(tick_pnl=5000.0, dd=8.0, win_rate=70.0, open_left=1) == (False, False)


def test_clean_candidate_can_pass_gates():
    assert s.passes_gates(tick_pnl=5000.0, dd=8.0, win_rate=70.0, open_left=0) == (True, True)
    # DD between 25 and 40 -> only the DD40 gate
    assert s.passes_gates(tick_pnl=5000.0, dd=33.0, win_rate=42.0, open_left=0) == (False, True)
    # negative P&L never passes
    assert s.passes_gates(tick_pnl=-1.0, dd=8.0, win_rate=70.0, open_left=0) == (False, False)


# -- leaderboard CSV surfaces the integrity columns -------------------------

def test_leaderboard_csv_has_exposure_and_partial_columns(tmp_path):
    rows = [{
        "candidate_id": "abc", "phase": "june", "signal_name": "f_all",
        "strategy_name": "tsl18_base", "score": 1.0, "tick_pnl": 10.0,
        "open_or_pending_left": 2, "partial_tick_signals": 3,
        "passes_dd25_gate": False, "passes_dd40_gate": False,
    }]
    s.write_board(rows, tmp_path, "june", top_n=5)
    with (tmp_path / "leaderboard_june.csv").open() as fh:
        reader = csv.DictReader(fh)
        header = reader.fieldnames
        first = next(reader)
    assert "open_or_pending_left" in header
    assert "partial_tick_signals" in header
    assert first["open_or_pending_left"] == "2"
    assert first["partial_tick_signals"] == "3"


# -- aggregate completeness + dedup guards ----------------------------------

def _write_results(path: Path, candidate_ids, phase="june"):
    path.parent.mkdir(parents=True, exist_ok=True)
    import json
    with path.open("w", encoding="utf-8") as fh:
        for cid in candidate_ids:
            fh.write(json.dumps({"candidate_id": cid, "phase": phase,
                                 "signal_name": "f", "strategy_name": "s",
                                 "score": 1.0, "tick_pnl": 1.0}) + "\n")


def _agg_args(inputs, out_dir, expected_rows):
    return Namespace(inputs=inputs, output_dir=str(out_dir), phase="june",
                     top_n=5, expected_rows=expected_rows)


def test_aggregate_refuses_incomplete_full_grid(tmp_path):
    res = tmp_path / "in" / "results_june_shard0.jsonl"
    _write_results(res, [f"c{i}" for i in range(3)])
    with pytest.raises(SystemExit, match="Incomplete aggregation"):
        s.aggregate(_agg_args([str(res)], tmp_path / "out", expected_rows=144))


def test_aggregate_accepts_matching_expected_rows(tmp_path):
    res = tmp_path / "in" / "results_june_shard0.jsonl"
    _write_results(res, [f"c{i}" for i in range(3)])
    rc = s.aggregate(_agg_args([str(res)], tmp_path / "out", expected_rows=3))
    assert rc == 0
    assert (tmp_path / "out" / "leaderboard_june.csv").exists()


def test_aggregate_zero_expected_skips_count_check(tmp_path):
    # Smoke runs pass expected_rows=0 -> no completeness assert.
    res = tmp_path / "in" / "results_june_shard0.jsonl"
    _write_results(res, ["only-one"])
    assert s.aggregate(_agg_args([str(res)], tmp_path / "out", expected_rows=0)) == 0


def test_aggregate_still_detects_duplicates(tmp_path):
    res = tmp_path / "in" / "results_june_shard0.jsonl"
    _write_results(res, ["dup", "dup", "other"])  # same (phase, candidate_id) twice
    with pytest.raises(SystemExit, match="Duplicate candidate rows"):
        s.aggregate(_agg_args([str(res)], tmp_path / "out", expected_rows=0))
