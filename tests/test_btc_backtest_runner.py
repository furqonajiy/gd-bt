"""End-to-end wiring smoke for the BTC backtest runner.

Patches the strategy template to BTC-scale test values (the real file ships as a
guarded placeholder) and runs the generate -> format -> parse -> run_backtest
pipeline on a synthetic ~100k-price chart. Asserts the pipeline executes, emits
an xlsx, and produces at least one generated signal.
"""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from trading.engine import DEFAULT_CONFIG, RejectionSignalConfig, SymbolSpec

import trading.btcusd.backtest as bt


_BTC_SPEC = SymbolSpec(symbol="BTCUSD", point_value=0.01, digits=2,
                       contract_size=1.0, min_lot=0.01, lot_step=0.01)

_REJ = RejectionSignalConfig(
    lookback_bars=1, min_wick=100.0, min_bar_range=100.0, wick_body_ratio=1.0,
    zone_buffer=50.0, zone_size=100.0, cooldown_minutes=0, same_zone_cooldown_minutes=0,
    max_spread_points=None, session_start_hour=None, session_end_hour=None,
    entry_range_width=100.0, sl_distance=500.0,
    tp1_distance=1000.0, tp2_distance=2000.0, tp3_distance=4000.0, price_digits=2,
)

_CFG = replace(
    DEFAULT_CONFIG, sizing_mode="fixed", lot_per_entry=0.01, entry_count=1,
    entry_ladder="range_uniform", activation_delay_minutes=0, pending_expiry_minutes=630,
    max_hold_minutes=90, sl_multiplier=1.0, final_target="TP1",
    trailing_open_distance=0.0, trailing_close_distance=0.0, bonus_per_closed_lot=0.0,
)

_ROWS = [
    ("09:00", 100000.00, 100100.00, 99900.00, 100020.00),
    ("09:01", 100020.00, 100080.00, 99000.00, 100060.00),  # rejection -> BUY @ 09:02
    ("09:02", 100060.00, 100200.00, 100040.00, 100150.00),
    ("09:03", 100150.00, 100400.00, 100120.00, 100380.00),
    ("09:04", 100380.00, 100600.00, 100350.00, 100560.00),
    ("09:05", 100560.00, 100900.00, 100540.00, 100880.00),
    ("09:06", 100880.00, 101100.00, 100860.00, 101050.00),
    ("09:07", 101050.00, 101200.00, 101000.00, 101150.00),
    ("09:08", 101150.00, 101300.00, 101100.00, 101250.00),
    ("09:09", 101250.00, 101400.00, 101200.00, 101350.00),
    ("09:10", 101350.00, 101500.00, 101300.00, 101450.00),
]


def _write_chart(tmp_path: Path) -> Path:
    head = "<DATE>\t<TIME>\t<OPEN>\t<HIGH>\t<LOW>\t<CLOSE>\t<TICKVOL>\t<VOL>\t<SPREAD>\n"
    body = "".join(
        f"2026.06.02\t{t}:00\t{o:.2f}\t{h:.2f}\t{l:.2f}\t{c:.2f}\t10\t0\t30\n"
        for (t, o, h, l, c) in _ROWS
    )
    p = tmp_path / "BTCUSD_M1_202606_ELEV8.csv"
    p.write_text(head + body, encoding="utf-8")
    return p


def test_btc_backtest_runs_end_to_end(tmp_path, monkeypatch):
    monkeypatch.setattr(bt, "assert_configured", lambda: None)
    monkeypatch.setattr(bt, "BTC_SPEC", _BTC_SPEC)
    monkeypatch.setattr(bt, "BTC_REJECTION_CONFIG", _REJ)
    monkeypatch.setattr(bt, "BTC_STRATEGY_CONFIG", _CFG)

    chart = _write_chart(tmp_path)
    out_stem = tmp_path / "btc_out"
    result = bt.run([str(chart)], str(out_stem))

    assert isinstance(result, dict)
    # at least one rejection signal generated + an xlsx produced
    assert out_stem.with_suffix(".xlsx").exists()


def test_assert_configured_guard_fires_when_unconfigured(monkeypatch):
    # The guard must raise when the flag is off, regardless of the shipped value
    # (strategy.py ships configured now that mt5-info values are filled).
    import pytest
    import trading.btcusd.strategy as strat
    monkeypatch.setattr(strat, "BTC_SPEC_CONFIGURED", False)
    with pytest.raises(RuntimeError, match="unconfigured template"):
        strat.assert_configured()


def test_btc_backtest_geometry_overrides_apply(tmp_path, monkeypatch):
    # Overrides must flow via dataclasses.replace and still run end-to-end.
    monkeypatch.setattr(bt, "assert_configured", lambda: None)
    monkeypatch.setattr(bt, "BTC_SPEC", _BTC_SPEC)
    monkeypatch.setattr(bt, "BTC_REJECTION_CONFIG", _REJ)
    monkeypatch.setattr(bt, "BTC_STRATEGY_CONFIG", _CFG)
    chart = _write_chart(tmp_path)
    result = bt.run(
        [str(chart)], str(tmp_path / "btc_rr"),
        entry_range_width=5.0, sl_distance=195.0, tp1_distance=300.0,
        tp2_distance=600.0, tp3_distance=1200.0, max_hold_minutes=240,
    )
    assert isinstance(result, dict)
    assert (tmp_path / "btc_rr").with_suffix(".xlsx").exists()