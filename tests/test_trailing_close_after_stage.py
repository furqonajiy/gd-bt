"""trailing_close_after_stage in tp_levels mode: 0 = trail from open (parity);
1 = engage the trailing-close stop only at/after TP1 (the leg rides the base
stop until TP1 is touched). This is the lever the tick trailing sweep explores
(trail-from-open vs trail-after-TP1)."""
from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

from trading.engine import Bar, DEFAULT_CONFIG, advance_bars, open_position, parse_one_signal


def _bar(t, o, h, l, c):
    return Bar(t, o, h, l, c, 0, 0.0)


def _pos(after_stage):
    sig = parse_one_signal(
        "1. BUY XAUUSD 4750 - 4748 SL 4744 TP1 4760 TP2 4770 TP3 4780 10:00 AM",
        source_date="2026-06-01", source_offset=3,
    )
    cfg = replace(DEFAULT_CONFIG, entry_count=1, trailing_close_distance=3.0,
                  trailing_close_after_stage=after_stage, activation_delay_minutes=0,
                  lock_after_tp1=True, profit_lock_mode="tp_levels")
    pos = open_position(sig, 1000.0, cfg)
    t = sig.signal_time_chart
    # In profit (+7) but TP1 (4760) NOT touched -> stage 0.
    advance_bars(pos, [
        _bar(t, 4752, 4752, 4749, 4751),
        _bar(t + timedelta(minutes=1), 4751, 4757, 4755, 4756),
    ], cfg)
    return sig, cfg, pos


def test_after_stage_0_trails_from_open():
    sig, cfg, pos = _pos(0)
    e = pos.entries[0]
    assert e.trailing_stop == 4754.0                 # high 4757 - 3
    assert pos.effective_stop_for(e, cfg) == 4754.0  # folded in immediately


def test_after_stage_1_waits_for_tp1():
    sig, cfg, pos = _pos(1)
    e = pos.entries[0]
    assert e.trailing_stop == 4754.0                 # engine still tracks it
    # stage 0 (TP1 not touched): trailing NOT folded in -> rides the base stop.
    assert pos.effective_stop_for(e, cfg) < 4754.0
    # Touch TP1 -> stage 1 -> trailing-close now active.
    advance_bars(pos, [_bar(sig.signal_time_chart + timedelta(minutes=2),
                            4756, 4761, 4759, 4760)], cfg)
    assert pos.effective_stop_for(e, cfg) >= 4754.0
