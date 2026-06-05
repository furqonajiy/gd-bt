"""SymbolSpec defaults + the chart point_value parameter.

Pins that gold stays byte-identical: XAU_SPEC must equal the constants the
codebase has always used, and load_chart's default must reproduce the old
spread_price math. A custom point_value must flow through so non-2-digit
instruments (e.g. BTCUSD) scale their spread correctly.
"""
from __future__ import annotations

from pathlib import Path

from xauusd_trading import (
    CONTRACT_SIZE_OZ,
    POINT_VALUE,
    SymbolSpec,
    XAU_SPEC,
    load_chart,
)
from xauusd_trading.io.mt5_adapter import _archive_filename, _normalize_file_symbol


def test_xau_spec_matches_legacy_constants():
    assert XAU_SPEC.symbol == "XAUUSD"
    assert XAU_SPEC.point_value == POINT_VALUE == 0.01
    assert XAU_SPEC.contract_size == CONTRACT_SIZE_OZ == 100.0
    assert XAU_SPEC.digits == 2
    assert XAU_SPEC.min_lot == 0.01
    assert XAU_SPEC.lot_step == 0.01


def test_symbol_spec_is_constructible_for_other_instruments():
    btc = SymbolSpec(symbol="BTCUSD", point_value=0.01, digits=2,
                     contract_size=1.0, min_lot=0.01, lot_step=0.01)
    assert btc.symbol == "BTCUSD"
    assert btc.contract_size == 1.0


_CSV = (
    "<DATE>\t<TIME>\t<OPEN>\t<HIGH>\t<LOW>\t<CLOSE>\t<TICKVOL>\t<VOL>\t<SPREAD>\n"
    "2026.06.02\t09:00:00\t100.0\t101.0\t99.0\t100.5\t10\t0\t25\n"
)


def _write_csv(tmp_path: Path) -> Path:
    p = tmp_path / "XAUUSD_M1_202606_ELEV8.csv"
    p.write_text(_CSV, encoding="utf-8")
    return p


def test_load_chart_default_point_value_is_gold_identical(tmp_path):
    chart = load_chart([_write_csv(tmp_path)])
    # 25 points * 0.01 == 0.25, exactly the pre-parameter behaviour.
    assert chart.iloc[0]["spread_price"] == 25 * 0.01


def test_load_chart_custom_point_value_scales_spread(tmp_path):
    chart = load_chart([_write_csv(tmp_path)], point_value=1.0)
    assert chart.iloc[0]["spread_price"] == 25 * 1.0


def test_archive_filename_normalizes_symbol():
    assert _normalize_file_symbol("XAUUSD") == "XAUUSD"
    assert _normalize_file_symbol("XAUUSD.r") == "XAUUSD"
    assert _normalize_file_symbol("XAUUSDm") == "XAUUSD"
    assert _normalize_file_symbol("GOLD") == "XAUUSD"
    assert _normalize_file_symbol("BTCUSD") == "BTCUSD"
    assert _normalize_file_symbol("BTCUSD.r") == "BTCUSD"
    assert _archive_filename("XAUUSD", 2026, 6) == "XAUUSD_M1_202606_ELEV8.csv"
    assert _archive_filename("BTCUSD", 2026, 6) == "BTCUSD_M1_202606_ELEV8.csv"