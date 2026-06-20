"""BEP early-floor buffer in the bep_plus_half_tp1 lock model.

Once a leg moves `bep_trigger_distance` in favour (BEFORE TP1), its protective
stop ratchets to entry +/- `bep_buffer`. With the default buffer 0.0 that is exact
break-even (the historical behaviour, byte-identical -- the parity guard below),
and a positive buffer locks a few points of profit on a leg that spikes then
reverses before reaching TP1 (the wild-bar give-back the 2026-06-18 reconciliation
measured on live SC24). The stage-1 (fractional) TP1 lock is still the ceiling.

These all run on the legacy lifecycle (per_entry_targets off, trailing off), so
the DD40 / TRAILING-0.5 contract is untouched (covered by test_smoke.py).
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta

from trading.xauusd import Bar, DEFAULT_CONFIG, advance_bars, open_position, parse_one_signal


def _bar(t: datetime, o: float, h: float, l: float, c: float) -> Bar:
    return Bar(t, o, h, l, c, 0, 0.0)


def _buy_signal():
    return parse_one_signal(
        "1. BUY XAUUSD 4750 - 4748 SL 4744 TP1 4760 TP2 4770 TP3 4780 10:00 AM",
        source_date="2026-06-01",
        source_offset=3,
    )


def _bep_cfg(bep_buffer: float):
    return replace(
        DEFAULT_CONFIG,
        entry_count=1,
        entry_ladder="range_to_sl",
        sl_multiplier=1.0,
        activation_delay_minutes=0,
        sizing_mode="fixed",
        lot_per_entry=0.10,
        profit_lock_mode="bep_plus_half_tp1",
        lock_after_tp1=True,
        tp1_lock_delay_minutes=0,
        tp1_lock_fraction=1.0,
        bep_trigger_distance=3.0,
        bep_buffer=bep_buffer,
        trailing_open_distance=0.0,
        trailing_close_distance=0.0,
    )


def _run_arm_then_reverse(bep_buffer: float):
    """Fill one leg, arm BEP at +3 (without dipping through the floor), then
    retrace through entry. TP1 (4760) is never touched, so only the early floor
    can fire. Returns the single entry after the replay."""
    sig = _buy_signal()
    cfg = _bep_cfg(bep_buffer)
    pos = open_position(sig, 5000.0, cfg)
    assert len(pos.entries) == 1
    t = sig.signal_time_chart
    advance_bars(pos, [
        _bar(t, 4751, 4751, 4749, 4750),                          # fill the leg @ 4750
        _bar(t + timedelta(minutes=1), 4752, 4754, 4752, 4753),   # +4 high arms BEP (entry+3); low 4752 stays above both floors
        _bar(t + timedelta(minutes=2), 4753, 4753, 4750, 4751),   # retrace: low 4750 reaches entry & entry+1 floors
    ], cfg)
    return pos.entries[0]


def test_positive_buffer_locks_small_profit_before_tp1():
    e = _run_arm_then_reverse(bep_buffer=1.0)
    assert e.status != "OPEN"
    assert e.exit_price == 4751.0          # entry (4750) + buffer (1.0)
    assert e.pnl is not None and e.pnl > 0  # the "+ small points" is a locked gain


def test_zero_buffer_is_exact_break_even_parity():
    e = _run_arm_then_reverse(bep_buffer=0.0)
    assert e.status != "OPEN"
    assert e.exit_price == 4750.0          # exactly entry: historical break-even
    assert e.pnl is not None and abs(e.pnl) < 1e-9


def test_default_config_buffer_is_zero():
    # The generalization must not perturb the blessed default.
    assert DEFAULT_CONFIG.bep_buffer == 0.0
    assert DEFAULT_CONFIG.profit_lock_mode == "tp_levels"
