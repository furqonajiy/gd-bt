"""Tests for the structure-guard sweep's metric/aggregation logic.

Pins the parts that are easy to get subtly wrong: SIGNAL-level losing streaks
(TSL18 opens up to 8 entries per signal, so the entry-level streak over-counts
the felt pain) and the cross-feed filtered-winner/loser matching (which must use
the chart timestamp, not the renumbering per-day signal index, and must survive
the feed-zone-date vs chart-time midnight boundary).
"""
from __future__ import annotations

from tools.sweep_structure_guard import Trade, _filtered_breakdown, _metrics


def _leg(sigkey: str, t: str, side: str, pnl: float) -> Trade:
    # date is the (source/feed-zone) date column; time_chart is chart time
    return Trade(date=t[:10], time_chart=t, side=side, pnl=pnl, status="x", signal_key=sigkey)


def test_signal_level_streaks_vs_entry_level():
    # A: BUY win (+5,-3 -> +2); B: SELL lose in bull HTF; C: BUY lose in bear HTF;
    # D: BUY win. Signal order by time: A win, B lose(wrong), C lose(wrong), D win.
    trades = [
        _leg("S#A", "2026-06-30 10:00", "BUY", +5.0),
        _leg("S#A", "2026-06-30 10:00", "BUY", -3.0),
        _leg("S#B", "2026-06-30 10:10", "SELL", -4.0),
        _leg("S#B", "2026-06-30 10:10", "SELL", -4.0),
        _leg("S#C", "2026-06-30 10:20", "BUY", -2.0),
        _leg("S#C", "2026-06-30 10:20", "BUY", -2.0),
        _leg("S#D", "2026-06-30 10:30", "BUY", +10.0),
    ]
    htf = {("2026-06-30 10:10", "SELL"): "bull",   # B wrong-side
           ("2026-06-30 10:20", "BUY"): "bear"}    # C wrong-side
    m = _metrics("v", trades, htf)
    assert m.signals == 4 and m.losing_signals == 2
    # entry-level streak counts -3,-4,-4,-2,-2 = 5 (over-counts)
    assert m.max_consecutive_losing_entries == 5
    # signal-level streak counts B,C = 2 (the felt pain)
    assert m.max_consecutive_losing_signals == 2
    # both losing signals are wrong-side -> consecutive wrong-side = 2
    assert m.max_consecutive_wrong_side_losing_signals == 2


def test_wrong_side_streak_breaks_on_aligned_loss():
    # two losing signals, but the middle one is NOT wrong-side -> wrong-side run = 1
    trades = [
        _leg("S#1", "2026-06-30 09:00", "BUY", -3.0),   # bear HTF -> wrong-side
        _leg("S#2", "2026-06-30 09:10", "BUY", -3.0),   # bull HTF -> aligned loss
        _leg("S#3", "2026-06-30 09:20", "SELL", -3.0),  # bull HTF -> wrong-side
    ]
    htf = {("2026-06-30 09:00", "BUY"): "bear",
           ("2026-06-30 09:10", "BUY"): "bull",
           ("2026-06-30 09:20", "SELL"): "bull"}
    m = _metrics("v", trades, htf)
    assert m.max_consecutive_losing_signals == 3       # all three lose
    assert m.max_consecutive_wrong_side_losing_signals == 1  # broken by the aligned loss


def test_filtered_breakdown_matches_on_chart_time_not_renumbered_index():
    # base has 3 signals; the variant drops the middle one. The variant RENUMBERS
    # its remaining signals (#01, #02) so the Entry Key signal index is not stable
    # -- matching must use chart time + side and still find exactly the removed one.
    base = [
        _leg("2026-06-30#01", "2026-06-30 08:00", "BUY", +5.0),
        _leg("2026-06-30#02", "2026-06-30 09:00", "SELL", -7.0),   # the one removed
        _leg("2026-06-30#03", "2026-06-30 10:00", "BUY", +3.0),
    ]
    variant = [
        _leg("2026-06-30#01", "2026-06-30 08:00", "BUY", +5.0),
        _leg("2026-06-30#02", "2026-06-30 10:00", "BUY", +3.0),    # renumbered #03 -> #02
    ]
    total, winners, losers = _filtered_breakdown(base, variant)
    assert (total, winners, losers) == (1, 0, 1)   # removed the SELL loser


def test_filtered_breakdown_survives_feed_date_vs_chart_time_boundary():
    # A near-midnight GMT+7 signal: chart time 2026-06-30 21:00 but feed/source
    # date 2026-07-01. Matching on chart-time+side must not be confused by the
    # date being a different calendar day.
    base = [
        Trade(date="2026-07-01", time_chart="2026-06-30 21:00", side="BUY",
              pnl=-9.0, status="x", signal_key="2026-07-01#01"),
        _leg("2026-06-30#05", "2026-06-30 14:00", "SELL", +4.0),
    ]
    variant = [
        _leg("2026-06-30#05", "2026-06-30 14:00", "SELL", +4.0),
    ]
    total, winners, losers = _filtered_breakdown(base, variant)
    assert (total, winners, losers) == (1, 0, 1)   # the boundary BUY loser removed
