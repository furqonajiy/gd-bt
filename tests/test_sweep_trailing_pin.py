"""The self-limit sweep can pin a fixed trailing (open, close) combo per cell, so
the per-regime trailing matrix enumerates all combinations while the SC24-seeded
strategy grid still varies underneath. Default (no pins) is the legacy
no-trailing sweep: trailing_open pinned 0, trailing_close left to the draw.

Deterministic (candidate construction only; no backtest), so it runs everywhere.
"""
from __future__ import annotations

import importlib

ssl = importlib.import_module("tools.sweep_self_limit")


def test_legacy_pins_trailing_open_zero_and_varies_close():
    cands = ssl.make_limit_candidates(42, 40)
    assert {c["trailing_open_distance"] for c in cands} == {0.0}
    # trailing_close is left to the candidate draw -> more than one value present.
    assert len({c["trailing_close_distance"] for c in cands}) > 1


def test_pin_forces_one_trailing_combo_on_every_candidate():
    cands = ssl.make_limit_candidates(
        42, 40, pin_trailing_open=0.2, pin_trailing_close=5.0)
    assert cands  # SC24 seeds + draws still produced
    assert all(c["trailing_open_distance"] == 0.2 for c in cands)
    assert all(c["trailing_close_distance"] == 5.0 for c in cands)


def test_pin_open_only_leaves_close_to_draw():
    cands = ssl.make_limit_candidates(42, 40, pin_trailing_open=3.0)
    assert all(c["trailing_open_distance"] == 3.0 for c in cands)
    assert len({c["trailing_close_distance"] for c in cands}) > 1
