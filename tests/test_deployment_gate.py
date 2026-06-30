"""Tests for the small-account deployment-safety gates (DeploymentGate).

The gates are default-OFF, parity-preserving signal-acceptance filters: risk
budget, daily-loss circuit breaker, max-concurrent-signals. They only REJECT
signals (never add/modify), keyed off live account state. Covered here:
default-off parity, each gate's accept/reject boundary, entries->zone-risk
scaling, group-not-entry counting, daily reset + start-of-day basis,
determinism, and only-removes via run_backtest.
"""
from __future__ import annotations

import types
from datetime import datetime, timedelta

from trading.engine import (
    CONTRACT_SIZE_OZ, DeploymentGate, StrategyConfig,
)


# --- helpers ----------------------------------------------------------------

def _sig(key="0601#01", t="2026-06-01 10:00", src=None):
    dt = datetime.fromisoformat(t)
    return types.SimpleNamespace(
        signal_key=key, signal_time_chart=dt,
        signal_time_source=datetime.fromisoformat(src) if src else dt)


def _built(*, status="SL", equity_before=2000.0, equity_after=1900.0,
           legs=((4500.0, 4480.0),), fill="2026-06-01 10:05",
           exit="2026-06-01 11:00", lot=0.01, key="0601#01"):
    """legs: list of (entry_price, effective_SL)."""
    ft = datetime.fromisoformat(fill) if fill else None
    xt = datetime.fromisoformat(exit) if exit else None
    ers = [{"signal_key": key, "entry_price": ep, "effective_SL": sl,
            "fill_time": ft, "exit_time": xt, "lot": lot,
            "pnl": -10.0, "entry_status": status, "signal_status": status}
           for ep, sl in legs]
    return {"row": {"equity_before": equity_before}, "status": status,
            "equity_after": equity_after, "entry_rows": ers}


# --- default-off parity -----------------------------------------------------

def test_default_config_yields_no_gate():
    assert DeploymentGate.maybe(StrategyConfig()) is None


def test_any_flag_enables_gate():
    for over in (dict(risk_budget_gate=True, max_zone_risk_pct=0.06),
                 dict(daily_loss_limit_pct=0.05),
                 dict(max_open_signals=1)):
        assert DeploymentGate.maybe(StrategyConfig(**over)) is not None


# --- risk-budget gate -------------------------------------------------------

def test_worst_case_risk_math():
    g = DeploymentGate(StrategyConfig(minimum_lot=0.01))
    # leg dists 20 / 23 price -> $20 / $23 at 0.01 lot x 100 contract
    single, zone = g.worst_case_risk(
        [{"entry_price": 4500.0, "effective_SL": 4480.0},
         {"entry_price": 4502.0, "effective_SL": 4479.0}])
    assert round(single, 6) == 23.0 and round(zone, 6) == 43.0


def test_risk_budget_rejects_single_over_cap():
    g = DeploymentGate(StrategyConfig(minimum_lot=0.01, risk_budget_gate=True,
                                      max_single_entry_risk_pct=0.04))
    # $90 single leg vs 4% of $2000 = $80 -> reject
    rows = [{"entry_price": 4500.0, "effective_SL": 4410.0}]
    assert g.risk_budget_check(rows, 2000.0) == "risk_budget_single"


def test_risk_budget_rejects_zone_over_cap():
    g = DeploymentGate(StrategyConfig(minimum_lot=0.01, risk_budget_gate=True,
                                      max_zone_risk_pct=0.06))
    # 8 legs x $20 = $160 vs 6% of $2000 = $120 -> reject
    rows = [{"entry_price": 4500.0, "effective_SL": 4480.0}] * 8
    assert g.risk_budget_check(rows, 2000.0) == "risk_budget_zone"


def test_risk_budget_allows_tight_stop():
    g = DeploymentGate(StrategyConfig(minimum_lot=0.01, risk_budget_gate=True,
                                      max_single_entry_risk_pct=0.04,
                                      max_zone_risk_pct=0.06))
    # 2 legs x $10 = $20 zone, $10 single -> well under caps on $2000
    rows = [{"entry_price": 4500.0, "effective_SL": 4490.0}] * 2
    assert g.risk_budget_check(rows, 2000.0) is None


def test_fewer_entries_lower_zone_risk():
    g = DeploymentGate(StrategyConfig(minimum_lot=0.01))
    leg = {"entry_price": 4500.0, "effective_SL": 4480.0}
    _, zone8 = g.worst_case_risk([leg] * 8)
    _, zone2 = g.worst_case_risk([leg] * 2)
    assert zone2 < zone8 and round(zone2, 6) == 40.0 and round(zone8, 6) == 160.0


# --- max concurrent open signals -------------------------------------------

def test_max_open_rejects_when_slot_full():
    g = DeploymentGate(StrategyConfig(max_open_signals=1, pending_expiry_minutes=180))
    a = _sig("A", "2026-06-01 10:00")
    assert g.pre_check(a, 2000.0) is None
    g.register(a, _built(key="A", fill="2026-06-01 10:05", exit="2026-06-01 12:00"))
    # B arrives at 11:00 while A open (10:00->12:00) -> reject
    b = _sig("B", "2026-06-01 11:00")
    assert g.pre_check(b, 2000.0) == "max_open_signals"
    # C arrives at 12:30 after A closed -> allowed
    c = _sig("C", "2026-06-01 12:30")
    assert g.pre_check(c, 2000.0) is None


def test_multi_entry_signal_counts_as_one_group():
    g = DeploymentGate(StrategyConfig(max_open_signals=1, pending_expiry_minutes=180))
    a = _sig("A", "2026-06-01 10:00")
    g.pre_check(a, 2000.0)
    # one signal, EIGHT filled legs -> still one open group
    g.register(a, _built(key="A", legs=((4500.0, 4480.0),) * 8,
                         fill="2026-06-01 10:05", exit="2026-06-01 12:00"))
    # a second signal that itself has 8 legs is the 2nd GROUP -> blocked while A open
    b = _sig("B", "2026-06-01 11:00")
    assert g.pre_check(b, 2000.0) == "max_open_signals"


def test_no_fill_signal_holds_slot_until_expiry():
    g = DeploymentGate(StrategyConfig(max_open_signals=1, pending_expiry_minutes=180))
    a = _sig("A", "2026-06-01 10:00")
    g.pre_check(a, 2000.0)
    g.register(a, _built(key="A", status="NO_FILL", fill=None, exit=None,
                         equity_after=2000.0))
    # within the 180-min pending window the slot is held
    assert g.pre_check(_sig("B", "2026-06-01 12:00"), 2000.0) == "max_open_signals"
    # after expiry it is free
    assert g.pre_check(_sig("C", "2026-06-01 13:30"), 2000.0) is None


# --- daily-loss circuit breaker ---------------------------------------------

def test_daily_breaker_blocks_after_threshold_same_day():
    g = DeploymentGate(StrategyConfig(daily_loss_limit_pct=0.05))
    # start-of-day equity 2000 -> 5% = $100 loss budget
    s1 = _sig("A", "2026-06-01 09:00")
    assert g.pre_check(s1, 2000.0) is None
    g.register(s1, _built(equity_before=2000.0, equity_after=1900.0))  # -$100 -> breach
    # next signal same day is blocked
    assert g.pre_check(_sig("B", "2026-06-01 10:00"), 1900.0) == "daily_loss_breaker"


def test_daily_breaker_uses_start_of_day_equity_not_current():
    g = DeploymentGate(StrategyConfig(daily_loss_limit_pct=0.10))
    s1 = _sig("A", "2026-06-01 09:00")
    g.pre_check(s1, 2000.0)                       # SOD equity pinned at 2000
    g.register(s1, _built(equity_before=2000.0, equity_after=1950.0))  # -$50 (<10% of 2000)
    assert g.pre_check(_sig("B", "2026-06-01 10:00"), 1950.0) is None  # -2.5% so far, ok
    g.register(_sig("B", "2026-06-01 10:00"), _built(equity_before=1950.0, equity_after=1790.0))  # -$160 more
    # cumulative -$210 vs 10% of SOD 2000 = $200 -> breach
    assert g.pre_check(_sig("C", "2026-06-01 11:00"), 1790.0) == "daily_loss_breaker"


def test_daily_breaker_resets_next_day():
    g = DeploymentGate(StrategyConfig(daily_loss_limit_pct=0.05))
    s1 = _sig("A", "2026-06-01 09:00")
    g.pre_check(s1, 2000.0)
    g.register(s1, _built(equity_before=2000.0, equity_after=1800.0))  # breach day 1
    assert g.pre_check(_sig("B", "2026-06-01 10:00"), 1800.0) == "daily_loss_breaker"
    # new day -> fresh budget
    assert g.pre_check(_sig("C", "2026-06-02 09:00"), 1800.0) is None


def test_daily_breaker_uses_source_day():
    # source (feed-zone) date differs from chart date around midnight; the day key
    # is the SOURCE date so it lines up with the report's daily breakdown.
    g = DeploymentGate(StrategyConfig(daily_loss_limit_pct=0.05))
    s1 = _sig("A", t="2026-06-01 23:30", src="2026-06-02 06:30")
    assert g._signal_day(s1) == datetime.fromisoformat("2026-06-02 06:30").date()


# --- determinism + only-removes (integration) -------------------------------

def test_gate_is_deterministic():
    def run():
        g = DeploymentGate(StrategyConfig(max_open_signals=1, daily_loss_limit_pct=0.05,
                                          pending_expiry_minutes=180))
        out = []
        for i in range(5):
            s = _sig(f"S{i}", f"2026-06-01 {10+i}:00")
            out.append(g.pre_check(s, 2000.0))
            g.register(s, _built(key=f"S{i}", fill=f"2026-06-01 {10+i}:05",
                                 exit=f"2026-06-01 {10+i}:30", equity_after=1990.0))
        return out
    assert run() == run()


# --- run_backtest integration (parity + only-removes) -----------------------

import glob  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

_FEED = Path("signals/t818.txt")
_CHART = sorted(glob.glob("data/XAUUSD_M1_202606_ELEV8.csv"))
_HAVE_DATA = _FEED.exists() and bool(_CHART)
_SKIP = pytest.mark.skipif(not _HAVE_DATA, reason="needs signals/t818.txt + June M1")

_GEO = dict(
    sizing_mode="risk", lot_per_entry=0.01, risk_per_signal=0.01, minimum_lot=0.01,
    lot_step=0.01, entry_count=8, entry_ladder="range_to_sl", entry_sl_gap=0.7,
    activation_delay_minutes=0, pending_expiry_minutes=180, max_hold_minutes=150,
    sl_multiplier=1.8, final_target="TP3", lock_after_tp1=True, lock_after_tp2=True,
    tp1_lock_delay_minutes=24, tp2_lock_delay_minutes=24, trailing_open_distance=0.5,
    trailing_close_distance=0.5, trailing_close_after_stage=2,
)


def _slice():
    import sys
    sys.path.insert(0, "tools")
    import backtest_explicit as bx
    from trading.engine import CsvChartSource, parse_signals_file
    sigs = bx.filter_signals_by_date(parse_signals_file(_FEED), "2026-06-02", "2026-06-03")
    return list(sigs), CsvChartSource(bx._expand_chart_paths(_CHART))


@_SKIP
def test_run_backtest_gate_off_is_byte_identical():
    from trading.engine import run_backtest
    sigs, chart = _slice()
    plain = run_backtest(sigs, chart, StrategyConfig(initial_capital=2000.0, **_GEO))
    # explicit neutral gate fields (all off) must reproduce the plain run exactly
    neutral = run_backtest(sigs, chart, StrategyConfig(
        initial_capital=2000.0, risk_budget_gate=False, max_zone_risk_pct=0.0,
        max_single_entry_risk_pct=0.0, daily_loss_limit_pct=0.0, max_open_signals=0, **_GEO))
    assert plain["net_profit"] == neutral["net_profit"]
    assert plain["signals_included"] == neutral["signals_included"]
    assert "deployment_gate" not in plain


@_SKIP
def test_run_backtest_gate_only_removes_signals():
    from trading.engine import run_backtest
    sigs, chart = _slice()
    base = run_backtest(sigs, chart, StrategyConfig(initial_capital=2000.0, **_GEO))
    gated = run_backtest(sigs, chart, StrategyConfig(
        initial_capital=2000.0, max_open_signals=1, risk_budget_gate=True,
        max_zone_risk_pct=0.06, max_single_entry_risk_pct=0.04,
        daily_loss_limit_pct=0.05, **_GEO))
    base_keys = {r["signal_key"] for r in base["rows"]}
    gated_keys = {r["signal_key"] for r in gated["rows"]}
    assert gated_keys <= base_keys              # never invents a signal
    assert gated["signals_included"] < base["signals_included"]  # it removed some
    assert gated["deployment_gate"]["rejected"]["max_open_signals"] > 0
