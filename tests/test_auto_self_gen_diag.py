"""Tests for auto_self's per-cycle feed diagnostic (_format_gen_diag)."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for p in (str(ROOT), str(ROOT / "tools")):
    if p not in sys.path:
        sys.path.insert(0, p)

import auto_self as A  # noqa: E402


_STATS = {"bars": 5000, "generated": 48, "in_window": 12, "allowed": 2, "placed": 0, "cap": 2}


def test_line_reports_all_fields_when_unblocked():
    line, _ = A._format_gen_diag(_STATS, halted=False, spread_block=False, cur_spread=31, block_new=False)
    assert "bars=5000" in line
    assert "generated=48" in line
    assert "in-window=12" in line
    assert "allowed=2" in line
    assert "placed=0/2" in line
    assert "spread=31" in line
    assert "block_new=no" in line


def test_block_reason_labels():
    _, _ = A._format_gen_diag(_STATS, halted=False, spread_block=False, cur_spread=10, block_new=False)
    spread_line, _ = A._format_gen_diag(_STATS, halted=False, spread_block=True, cur_spread=42, block_new=True)
    halt_line, _ = A._format_gen_diag(_STATS, halted=True, spread_block=False, cur_spread=10, block_new=True)
    both_line, _ = A._format_gen_diag(_STATS, halted=True, spread_block=True, cur_spread=42, block_new=True)
    assert "block_new=spread" in spread_line
    assert "block_new=halt" in halt_line and "halt+spread" not in halt_line
    assert "block_new=halt+spread" in both_line


def test_unknown_spread_shows_na():
    line, _ = A._format_gen_diag(_STATS, halted=False, spread_block=False, cur_spread=-1, block_new=False)
    assert "spread=n/a" in line


def test_signature_ignores_raw_spread_flap():
    # Same stats + flags, different live spread -> same signature (no reprint spam).
    _, sig_a = A._format_gen_diag(_STATS, halted=False, spread_block=False, cur_spread=24, block_new=False)
    _, sig_b = A._format_gen_diag(_STATS, halted=False, spread_block=False, cur_spread=33, block_new=False)
    assert sig_a == sig_b


def test_signature_changes_on_block_transition():
    _, clear = A._format_gen_diag(_STATS, halted=False, spread_block=False, cur_spread=30, block_new=False)
    _, blocked = A._format_gen_diag(_STATS, halted=False, spread_block=True, cur_spread=40, block_new=True)
    assert clear != blocked


def test_signature_changes_when_counts_change():
    _, base = A._format_gen_diag(_STATS, halted=False, spread_block=False, cur_spread=30, block_new=False)
    moved = dict(_STATS, allowed=1, placed=1)
    _, after = A._format_gen_diag(moved, halted=False, spread_block=False, cur_spread=30, block_new=False)
    assert base != after