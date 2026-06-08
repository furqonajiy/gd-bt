"""Report data layer: realized R per entry, entry-outcome aggregates, and the
Daily Breakdown trimmed to the traded window (no pre-start padding).

Deterministic: a tiny synthetic M1 chart, so it runs everywhere.
"""
from __future__ import annotations

from dataclasses import replace

from xauusd_trading import CsvChartSource, DEFAULT_CONFIG, parse_one_signal, run_backtest
from xauusd_trading.strategy.backtest import _payoff_ratio, _planned_rr, _realized_rr
from xauusd_trading.reporting.excel_report import (
    _fmt_payoff, _fmt_rr_planned, _fmt_rr_realized,
)


# --- pure helpers -----------------------------------------------------------

def test_realized_rr_is_side_aware():
    # BUY win: up 10 on 5 risk -> +2R; BUY loss: down 5 -> -1R.
    assert _realized_rr("BUY", 100.0, 95.0, 110.0, filled=True) == 2.0
    assert _realized_rr("BUY", 100.0, 95.0, 95.0, filled=True) == -1.0
    # SELL win: price falls 8 on 15.75 risk -> +R (positive, not negative).
    assert round(_realized_rr("SELL", 4314.0, 4329.75, 4306.0, filled=True), 4) == round(8 / 15.75, 4)
    assert _realized_rr("SELL", 4314.0, 4329.75, 4329.75, filled=True) == -1.0


def test_realized_rr_none_when_not_filled_or_no_risk():
    assert _realized_rr("BUY", 100.0, 95.0, None, filled=True) is None     # not closed
    assert _realized_rr("BUY", 100.0, 95.0, 110.0, filled=False) is None   # not filled
    assert _realized_rr("BUY", 100.0, 100.0, 110.0, filled=True) is None   # zero risk


def test_payoff_ratio_avg_win_over_avg_loss():
    assert _payoff_ratio([10.0, 20.0], [-5.0, -5.0]) == 3.0   # avg win 15 / avg loss 5
    assert _payoff_ratio([10.0], []) is None
    assert _payoff_ratio([], [-5.0]) is None


def test_planned_rr_reward_over_risk():
    # reward 10 (100->110) over risk 5 (100->95) = 2.0; positive regardless of side.
    assert _planned_rr(100.0, 95.0, 110.0) == 2.0
    assert _planned_rr(4314.0, 4329.75, 4276.0) == 38.0 / 15.75
    assert _planned_rr(100.0, 100.0, 110.0) is None     # zero risk
    assert _planned_rr(100.0, 95.0, None) is None


def test_rr_formats_as_ratio():
    # planned -> 1:N (one decimal)
    assert _fmt_rr_planned(2.41) == "1:2.4"
    assert _fmt_rr_planned(None) is None
    # realized -> win 1:N (two decimals), loss -N R
    assert _fmt_rr_realized(0.51) == "1:0.51"
    assert _fmt_rr_realized(-1.0) == "-1.0R"
    assert _fmt_rr_realized(None) is None
    # payoff -> 1:N, or "-" when undefined
    assert _fmt_payoff(1.5) == "1:1.50"
    assert _fmt_payoff(None) == "-"


# --- end-to-end through run_backtest ----------------------------------------

_HEADER = "<DATE>\t<TIME>\t<OPEN>\t<HIGH>\t<LOW>\t<CLOSE>\t<TICKVOL>\t<VOL>\t<SPREAD>"


def _bar(d, t, o, h, l, c):
    return f"{d}\t{t}\t{o}\t{h}\t{l}\t{c}\t100.0\t0.0\t2"


def _chart(tmp_path):
    rows = [
        # 2026-06-01: pre-signal day (must be trimmed from Daily Breakdown).
        _bar("2026.06.01", "10:00:00", 100, 100.5, 99.5, 100),
        _bar("2026.06.01", "10:01:00", 100, 100.5, 99.5, 100),
        # 2026-06-02: signal at 11:00, fills ~100, runs to TP1 110.
        _bar("2026.06.02", "11:00:00", 100, 100.2, 99.9, 100),
        _bar("2026.06.02", "11:01:00", 100, 101.0, 98.0, 100),     # fill the BUY
        _bar("2026.06.02", "11:02:00", 100, 103.0, 100.0, 102),
        _bar("2026.06.02", "11:05:00", 105, 111.0, 104.0, 110),    # hit TP1 110
        # 2026-06-03: post-signal day (also outside the traded window).
        _bar("2026.06.03", "09:00:00", 110, 110.5, 109.5, 110),
    ]
    p = tmp_path / "XAUUSD_M1_TEST.csv"
    p.write_text("\n".join([_HEADER, *rows]) + "\n", encoding="utf-8")
    return CsvChartSource([p])


def _config():
    return replace(
        DEFAULT_CONFIG, entry_count=1, entry_ladder="range_uniform",
        sl_multiplier=1.0, activation_delay_minutes=0, pending_expiry_minutes=60,
        max_hold_minutes=120, lock_after_tp1=False, final_target="TP1",
    )


def test_run_backtest_emits_entry_rr_and_outcome_aggregates(tmp_path):
    sig = parse_one_signal(
        "1. BUY XAUUSD 100 - 100 SL 95 TP1 110 TP2 115 TP3 120 11:00 AM",
        source_date="2026-06-02", source_offset=3,
    )
    result = run_backtest([sig], _chart(tmp_path), _config())

    # Every entry row carries a realized-R field.
    assert all("rr" in er for er in result["entry_rows"])
    # New summary aggregates exist.
    for key in ("entry_total", "entry_status_counts", "entry_statuses_present",
                "entry_filled", "entry_rr_avg", "entry_payoff_ratio"):
        assert key in result
    # The single entry filled and hit TP1 -> +2R realized; planned R:R 110/95 = 2.0.
    er = result["entry_rows"][0]
    assert er["entry_status"] == "TP1"
    assert er["rr"] == 2.0
    assert er["rr_planned"] == 2.0
    assert result["entry_rrp_avg"] == 2.0
    assert result["entry_status_counts"]["TP1"] == 1


def test_daily_breakdown_trims_pre_and_post_signal_days(tmp_path):
    sig = parse_one_signal(
        "1. BUY XAUUSD 100 - 100 SL 95 TP1 110 TP2 115 TP3 120 11:00 AM",
        source_date="2026-06-02", source_offset=3,
    )
    result = run_backtest([sig], _chart(tmp_path), _config())
    dates = [d["date"] for d in result["daily"]]
    # Chart spans 06-01..06-03 but the only signal is 06-02, so that's the only day.
    assert dates == ["2026-06-02"]
    day = result["daily"][0]
    assert day["entry_total"] == 1
    assert day["entry_status_counts"].get("TP1") == 1
    assert day["entry_rr_avg"] == 2.0
