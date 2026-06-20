"""Regression: shared-SL + trailing-open must NOT freeze the stop at the shared
level when a trailing-open fill lands beyond it.

Live reproduction (2026-06-13 #01): a 4-entry BUY ladder with --shared-sl and
--trailing-open-distance. Price dove well below the planned entries, so the
trailing-open legs filled BELOW the shared SL level (4211.75). The old engine
kept initial_sl = shared level, which sat ABOVE the BUY fill -> the stop-trigger
fired on the same bar and booked a phantom profit at a price that never traded,
while live MT5 (which rejects a stop above the fill and anchors a per-leg stop
from the fill) closed those legs at a small loss. Backtest +$18.64 vs live
-$9.00 on one signal.

The fix anchors each trailing-open leg's stop at (fill - its planned distance to
the shared level), matching the live executor. The stop is then correctly BELOW
the fill and the leg stays open until price actually reaches it.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta

from trading.engine import Bar, DEFAULT_CONFIG, advance_bars, open_position, parse_one_signal


def _bar(t: datetime, o: float, h: float, l: float, c: float) -> Bar:
    return Bar(t, o, h, l, c, 0, 0.0)


def _cfg():
    return replace(
        DEFAULT_CONFIG,
        sizing_mode="risk",
        risk_per_signal=0.03,
        entry_count=4,
        entry_ladder="range_uniform",
        sl_multiplier=1.15,
        trailing_open_distance=5.0,
        shared_sl=True,
        activation_delay_minutes=0,
        minimum_lot=0.01,
        lot_step=0.01,
    )


def _sig():
    # BUY 4217.5 - 4215.5 SL 4212.5 -> shared level = 4217.5 - 1.15*(4217.5-4212.5) = 4211.75
    return parse_one_signal(
        "1. BUY XAUUSD 4217.5 - 4215.5 SL 4212.5 TP1 4224.5 TP2 4229 TP3 4236 12:03 AM",
        source_date="2026-06-13",
        source_offset=7,
    )


def test_shared_level_is_4211_75():
    pos = open_position(_sig(), 5000.0, _cfg())
    assert round(pos.shared_sl_level, 2) == 4211.75


def test_trailing_open_fill_below_shared_level_anchors_stop_below_fill():
    cfg = _cfg()
    pos = open_position(_sig(), 5000.0, cfg)
    t = pos.signal.signal_time_chart
    planned = [e.entry_price for e in pos.entries]

    # Deep dive ~$15 below the entries arms every leg (Ask <= entry - 5), then a
    # small rebound triggers the BUY STOPs near 4207 -- below the 4211.75 shared
    # level. Keep the rebound under the shared level so the OLD bug (instant SL at
    # 4211.75) would have fired; the fix must keep the legs OPEN.
    advance_bars(pos, [
        _bar(t, 4210, 4210, 4202, 4203),                       # arms all legs (low 4202)
        _bar(t + timedelta(minutes=1), 4205, 4208, 4205, 4207),    # rebound to 4207 -> fills, low 4205 keeps legs open
    ], cfg)

    for e, plan_price in zip(pos.entries, planned):
        assert e.status == "OPEN", f"leg @{plan_price} should stay open, got {e.status}"
        # Stop is strictly BELOW the actual BUY fill -- a real stop, not the
        # shared level sitting above it.
        assert e.initial_sl < e.entry_price
        # Each leg keeps its planned distance to the shared level, anchored on fill.
        planned_distance = abs(plan_price - 4211.75)
        assert abs(e.initial_sl - (e.entry_price - planned_distance)) < 1e-6
        # And it is NOT the frozen shared level (the old, broken behaviour).
        assert abs(e.initial_sl - 4211.75) > 1e-6


def test_no_phantom_profit_on_same_bar_as_fill():
    cfg = _cfg()
    pos = open_position(_sig(), 5000.0, cfg)
    t = pos.signal.signal_time_chart

    advance_bars(pos, [
        _bar(t, 4210, 4210, 4202, 4203),
        _bar(t + timedelta(minutes=1), 4205, 4208, 4205, 4207),
    ], cfg)

    # No leg may have exited on the fill bar with a positive PnL booked at a
    # level above the fill (the phantom-profit artefact).
    for e in pos.entries:
        if e.status != "OPEN":
            assert not (e.pnl and e.pnl > 0 and (e.exit_price or 0) > e.entry_price)


def test_per_entry_stops_unchanged_without_shared_sl():
    # Same dive/rebound, shared_sl OFF: each leg already anchored from its fill at
    # base_stop_distance -- the fix must leave that path identical.
    cfg = replace(_cfg(), shared_sl=False)
    pos = open_position(_sig(), 5000.0, cfg)
    t = pos.signal.signal_time_chart

    advance_bars(pos, [
        _bar(t, 4210, 4210, 4202, 4203),
        _bar(t + timedelta(minutes=1), 4205, 4208, 4205, 4207),
    ], cfg)

    for e in pos.entries:
        if e.status == "OPEN":
            assert abs(e.initial_sl - (e.entry_price - pos.base_stop_distance)) < 1e-6
