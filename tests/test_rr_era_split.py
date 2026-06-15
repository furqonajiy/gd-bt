"""Tests for the R:R-era split diagnostic's pure functions.

Synthetic in-memory inputs only, so they run in a fresh clone with no ``data/``.
The 1R-denominator parity check uses the real signal parser + core.positions so
a drift between this tool's R and the engine's sizing risk would fail here.

Run from repo root:  python -m pytest tests/test_rr_era_split.py
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _load_tool():
    spec = importlib.util.spec_from_file_location(
        "rr_era_split", ROOT / "tools" / "rr_era_split.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


rr = _load_tool()


# ---------------------------------------------------------------------------
# era spec parsing
# ---------------------------------------------------------------------------
def test_parse_era_spec_open_ended():
    assert rr.parse_era_spec("A:2024-01-01:2025-03-31") == ("A", "2024-01-01", "2025-03-31")
    assert rr.parse_era_spec("C:2026-02-01:") == ("C", "2026-02-01", None)
    assert rr.parse_era_spec("X::2025-01-01") == ("X", None, "2025-01-01")


# ---------------------------------------------------------------------------
# signed move + realized move (R numerator)
# ---------------------------------------------------------------------------
def test_signed_move_directionality():
    assert rr._signed_move("BUY", 100.0, 104.0) == 4.0
    assert rr._signed_move("SELL", 100.0, 96.0) == 4.0
    assert rr._signed_move("BUY", 100.0, 98.0) == -2.0


def test_realized_price_move_skips_unfilled_and_sums_filled():
    ents = [
        {"side": "BUY", "entry_price": 100.0, "exit_price": 104.0,
         "fill_time": "t", "exit_time": "t"},          # +4
        {"side": "BUY", "entry_price": 101.0, "exit_price": 99.0,
         "fill_time": "t", "exit_time": "t"},          # -2
        {"side": "BUY", "entry_price": 102.0, "exit_price": None,
         "fill_time": None, "exit_time": None},        # no fill -> 0
    ]
    assert rr.realized_price_move(ents) == 2.0


# ---------------------------------------------------------------------------
# edge summary: R = move / risk, no-fill = 0R, conditional vs portfolio means
# ---------------------------------------------------------------------------
def test_summarize_edge_R_and_fill_accounting():
    result = {
        "rows": [
            {"signal_key": "D#01"},
            {"signal_key": "D#02"},
            {"signal_key": "D#03"},
        ],
        "entry_rows": [
            # D#01 filled, +4 move
            {"signal_key": "D#01", "side": "BUY", "entry_price": 100.0,
             "exit_price": 104.0, "fill_time": "t", "exit_time": "t"},
            # D#02 filled, -2 move
            {"signal_key": "D#02", "side": "BUY", "entry_price": 100.0,
             "exit_price": 98.0, "fill_time": "t", "exit_time": "t"},
            # D#03 no fill
            {"signal_key": "D#03", "side": "BUY", "entry_price": 100.0,
             "exit_price": None, "fill_time": None, "exit_time": None},
        ],
        "win_rate_pct": 50.0,
        "no_fills": 1,
        "trading_pnl": 200.0,
    }
    risk_by_key = {"D#01": 2.0, "D#02": 2.0, "D#03": 2.0}  # 1R = 2 price units each

    s = rr.summarize_edge(result, risk_by_key)
    assert s["signals"] == 3
    assert abs(s["fill_%"] - (2 / 3 * 100.0)) < 1e-9
    assert s["win_%"] == 50.0
    assert s["no_fill"] == 1
    # filled: +2R and -1R -> mean +0.5R
    assert abs(s["meanR_filled"] - 0.5) < 1e-9
    # all three (no-fill = 0R): (2 + -1 + 0)/3 = +0.333R
    assert abs(s["meanR_all"] - (1.0 / 3.0)) < 1e-9
    assert abs(s["medR_filled"] - 0.5) < 1e-9


# ---------------------------------------------------------------------------
# 1R denominator must equal the engine's own intended risk (no drift)
# ---------------------------------------------------------------------------
def test_intended_risk_matches_engine(tmp_path):
    from dataclasses import replace
    from xauusd_trading import DEFAULT_CONFIG, parse_signals_file
    from xauusd_trading.core.positions import compute_entries, compute_lot, initial_stop_for_entry

    sig_file = tmp_path / "one.txt"
    sig_file.write_text(
        "2025-06-02 GMT+7\n"
        "1. BUY XAUUSD 3300 - 3298 SL 3294 TP1 3305 TP2 3312 TP3 3320 10:00 AM\n",
        encoding="utf-8",
    )
    (sig,) = parse_signals_file(sig_file)

    # Exercise a non-trivial ladder so n_entries > 1 and the sum actually matters.
    cfg = replace(DEFAULT_CONFIG, entry_count=3, entry_ladder="range_to_sl",
                  entry_sl_gap=2, sl_multiplier=1.61)

    entries = compute_entries(sig, cfg)
    _, base_stop = compute_lot(cfg.initial_capital, sig, cfg)
    # The engine's total_price_risk = sum over entries of |entry - initial_stop|,
    # which must equal what the tool reports as 1R.
    engine_total = sum(
        abs(e - initial_stop_for_entry(sig.side, e, base_stop)) for e in entries
    )
    tool_total = rr.signal_intended_risk_price(sig, cfg)
    assert abs(tool_total - engine_total) < 1e-9
    assert tool_total > 0