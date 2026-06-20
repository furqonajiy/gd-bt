"""A sizeable signal always trades at least the broker minimum lot.

Regression for the reported bug: with a deep entry ladder on a small risk
budget the per-entry lot floored below `minimum_lot` and the engine zeroed it
(every entry reported 0.00 lot / NO_FILL). It must clamp UP to `minimum_lot`
instead, in both the backtest sizing path (`compute_lot`) and the live
executor rounding (`round_lot`).
"""
from __future__ import annotations

import math
from dataclasses import replace

from trading.xauusd import DEFAULT_CONFIG, compute_lot, parse_one_signal, round_lot


# The exact signal + config from the bug report: 8 entries, $5000 @ 2% risk,
# sl_multiplier 2.1 — the raw per-entry lot is ~0.0099, which floors to 0.
_BUG_SIGNAL = "1. BUY XAUUSD 4364 - 4366 SL 4360 TP1 4369 TP2 4374 TP3 4381 1:39 PM"


def _bug_config():
    return replace(
        DEFAULT_CONFIG,
        sizing_mode="risk",
        initial_capital=5000.0,
        risk_per_signal=0.02,
        entry_count=8,
        entry_ladder="range_to_sl",
        entry_sl_gap=0.5,
        sl_multiplier=2.1,
        minimum_lot=0.01,
        lot_step=0.01,
    )


def test_compute_lot_floors_to_minimum_not_zero():
    sig = parse_one_signal(_BUG_SIGNAL, "2026-01-02", 7)
    lot, _ = compute_lot(5000.0, sig, _bug_config())
    # Previously 0.0; now clamped up to the broker minimum.
    assert math.isclose(lot, 0.01, abs_tol=1e-9)


def test_compute_lot_keeps_larger_size_unchanged():
    # A generous risk budget still sizes above the minimum (no forced clamp).
    sig = parse_one_signal(_BUG_SIGNAL, "2026-01-02", 7)
    cfg = replace(_bug_config(), entry_count=1, risk_per_signal=0.10)
    lot, _ = compute_lot(5000.0, sig, cfg)
    assert lot > 0.01


def test_compute_lot_fixed_mode_floors_to_minimum():
    sig = parse_one_signal(_BUG_SIGNAL, "2026-01-02", 7)
    cfg = replace(_bug_config(), sizing_mode="fixed", lot_per_entry=0.004)
    lot, _ = compute_lot(5000.0, sig, cfg)
    assert math.isclose(lot, 0.01, abs_tol=1e-9)


def test_round_lot_bumps_subminimum_up_to_min():
    # A positive lot below the minimum is bumped up, not skipped.
    assert round_lot(0.004, 0.01, 0.01) == 0.01
    assert round_lot(0.009, 0.01, 0.01) == 0.01
    # A normal lot still floors to the step.
    assert round_lot(0.037, 0.01, 0.01) == 0.03
    assert round_lot(0.01, 0.01, 0.01) == 0.01


def test_round_lot_zero_stays_zero():
    # Genuinely nothing to place (engine could not size) still returns 0.0.
    assert round_lot(0.0, 0.01, 0.01) == 0.0
    assert round_lot(-1.0, 0.01, 0.01) == 0.0
