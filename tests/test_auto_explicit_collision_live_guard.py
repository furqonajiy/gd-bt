"""Live-safety guard: ``tools/auto_explicit.py`` must REFUSE non-baseline TSL18
collision flags BEFORE connecting to MT5.

The collision layer (PR #329) is backtest/sweep-only: live execution does not
enforce reject/downsize/flip/bank outcomes yet. Accepting a non-baseline flag in
live ``auto`` would give an operator false confidence that the account is
protected, so ``_validate_live_collision_policy`` hard-stops (SystemExit) before
any MT5 connection. These tests pin that contract.
"""
from __future__ import annotations

import argparse

import pytest

from tools.auto_explicit import _validate_live_collision_policy, build_parser


def _ns(**kw) -> argparse.Namespace:
    base = dict(opposite_signal_policy="allow_hedge", same_side_overlap_policy="allow_all")
    base.update(kw)
    return argparse.Namespace(**base)


def test_baseline_collision_policies_are_accepted():
    # The baseline (parity) policies must pass the guard untouched.
    _validate_live_collision_policy(_ns())  # no raise


def test_non_baseline_opposite_policy_is_refused():
    with pytest.raises(SystemExit) as ei:
        _validate_live_collision_policy(_ns(opposite_signal_policy="reject_opposite"))
    msg = str(ei.value)
    assert "backtest/sweep only" in msg
    assert "false protection" in msg


def test_non_baseline_same_side_policy_is_refused():
    with pytest.raises(SystemExit) as ei:
        _validate_live_collision_policy(_ns(same_side_overlap_policy="scale_in_fixed_risk"))
    msg = str(ei.value)
    assert "backtest/sweep only" in msg
    assert "false protection" in msg


def test_every_non_baseline_value_is_refused():
    for opp in ("reject_opposite", "profit_bank_rearm", "close_then_flip", "reduce_then_hedge"):
        with pytest.raises(SystemExit):
            _validate_live_collision_policy(_ns(opposite_signal_policy=opp))
    for same in ("reject_overlap", "scale_in_better_entry_only", "scale_in_fixed_risk"):
        with pytest.raises(SystemExit):
            _validate_live_collision_policy(_ns(same_side_overlap_policy=same))


def test_parser_collision_defaults_are_baseline():
    # The default CLI (no collision flags) resolves to the baseline policies, so a
    # normal live `auto` invocation passes the guard.
    actions = {a.dest: a for a in build_parser()._actions}
    assert actions["opposite_signal_policy"].default == "allow_hedge"
    assert actions["same_side_overlap_policy"].default == "allow_all"
    _validate_live_collision_policy(
        _ns(opposite_signal_policy=actions["opposite_signal_policy"].default,
            same_side_overlap_policy=actions["same_side_overlap_policy"].default))
