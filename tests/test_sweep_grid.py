"""Tests for tools/sweep_grid.py grid construction (pure; no data/ needed).

Run from repo root:  python -m pytest tests/test_sweep_grid.py
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

sg = importlib.import_module("tools.sweep_grid")
BASE = sg.sweep.base_config_dict()


def test_cast_respects_field_types():
    assert sg._cast("3", 3.0) == 3.0 and isinstance(sg._cast("3", 3.0), float)
    assert sg._cast("2", 3) == 2 and isinstance(sg._cast("2", 3), int)
    assert sg._cast("true", False) is True
    assert sg._cast("false", False) is False
    assert sg._cast("TP1", "TP2") == "TP1"


def test_cast_bad_bool_raises():
    with pytest.raises(SystemExit):
        sg._cast("maybe", False)


def test_parse_grid_types_and_unknown_field():
    grid = sg.parse_grid(["bep_trigger_distance=2,3,5"], BASE)
    assert grid == {"bep_trigger_distance": [2.0, 3.0, 5.0]}

    grid = sg.parse_grid(["lock_after_tp1=true,false"], BASE)
    assert grid == {"lock_after_tp1": [True, False]}

    with pytest.raises(SystemExit):
        sg.parse_grid(["not_a_field=1,2"], BASE)

    with pytest.raises(SystemExit):
        sg.parse_grid(["bep_trigger_distance"], BASE)  # missing '='


def test_build_grid_is_full_cartesian_and_freezes_rest():
    grid = {
        "profit_lock_mode": ["tp_levels", "bep_plus_half_tp1"],
        "bep_trigger_distance": [2.0, 3.0, 5.0],
    }
    cands = sg.build_grid_candidates(grid, BASE)
    assert len(cands) == 2 * 3  # full product

    gridded = set(grid.keys())
    for c in cands:
        # every non-gridded field stays exactly at the base (DD40 contract)
        for k, v in BASE.items():
            if k not in gridded:
                assert c[k] == v
        # gridded values come from the supplied lists
        assert c["profit_lock_mode"] in grid["profit_lock_mode"]
        assert c["bep_trigger_distance"] in grid["bep_trigger_distance"]

    # each (mode, trigger) pair appears exactly once
    pairs = {(c["profit_lock_mode"], c["bep_trigger_distance"]) for c in cands}
    assert len(pairs) == 6