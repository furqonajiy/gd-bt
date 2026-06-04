"""Tests for tools/sweep_limit_entry.py candidate generation (pure; no data/ needed).

Run from repo root:  python -m pytest tests/test_sweep_limit_entry.py -q
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

sl = importlib.import_module("tools.sweep_limit_entry")


def test_all_candidates_are_limit_entries():
    cands = sl.make_limit_candidates(seed=1, max_candidates=60)
    assert cands, "expected at least one candidate"
    # every candidate must be a LIMIT entry
    assert all(c["trailing_open_distance"] == 0.0 for c in cands)


def test_trailing_close_is_still_varied():
    cands = sl.make_limit_candidates(seed=1, max_candidates=80)
    tc_values = {c["trailing_close_distance"] for c in cands}
    # trailing-close is NOT pinned -> some candidates trail on exit
    assert tc_values - {0.0}, "expected some candidates with trailing_close > 0"


def test_first_candidate_is_dd40_base_as_limit():
    cands = sl.make_limit_candidates(seed=3, max_candidates=10)
    base = sl.sweep.base_config_dict()
    base["trailing_open_distance"] = 0.0
    assert cands[0] == base


def test_respects_cap_and_dedups():
    cands = sl.make_limit_candidates(seed=7, max_candidates=30)
    assert len(cands) <= 30
    ids = [sl.sweep._json_hash(c) for c in cands]
    assert len(ids) == len(set(ids)), "candidates must be unique"