"""Per-entry-target strategy: each leg exits at its own TP; RUN legs trail past
TP3; a leg ratchets to break-even+ once it's bep_after_move in favour.

Deterministic synthetic M1 charts, so it runs everywhere.
"""
from __future__ import annotations

import importlib.util
from dataclasses import replace
from pathlib import Path

from xauusd_trading import DEFAULT_CONFIG, CsvChartSource, parse_one_signal, run_backtest
from xauusd_trading.core.positions import open_position, target_price_for_label

ROOT = Path(__file__).resolve().parents[1]
_HEADER = "<DATE>\t<TIME>\t<OPEN>\t<HIGH>\t<LOW>\t<CLOSE>\t<TICKVOL>\t<VOL>\t<SPREAD>"


def _bar(d, t, o, h, l, c):
    return f"{d}\t{t}\t{o}\t{h}\t{l}\t{c}\t100.0\t0.0\t2"


def _chart(tmp_path, rows, name="C.csv"):
    p = tmp_path / name
    p.write_text("\n".join([_HEADER, *rows]) + "\n", encoding="utf-8")
    return CsvChartSource([p])


def _sig():
    return parse_one_signal(
        "1. BUY XAUUSD 100 - 100 SL 90 TP1 110 TP2 120 TP3 130 11:00 AM",
        source_date="2026-06-02", source_offset=3,
    )


def _by_key(result):
    return {er["entry_key"]: er for er in result["entry_rows"]}


def test_target_price_for_label():
    sig = _sig()
    assert target_price_for_label("TP1", sig) == 110
    assert target_price_for_label("TP2", sig) == 120
    assert target_price_for_label("TP3", sig) == 130
    assert target_price_for_label("RUN", sig) == 130   # runner aims at TP3


def test_open_position_assigns_per_entry_targets():
    cfg = replace(DEFAULT_CONFIG, entry_count=4, entry_ladder="range_uniform",
                  per_entry_targets=("TP1", "TP2", "TP3", "RUN"))
    pos = open_position(_sig(), 5000.0, cfg)
    assert [e.target_label for e in pos.entries] == ["TP1", "TP2", "TP3", "RUN"]
    assert [e.target_price for e in pos.entries] == [110, 120, 130, 130]


def test_each_leg_exits_at_its_own_target_and_runner_trails(tmp_path):
    cfg = replace(
        DEFAULT_CONFIG, entry_count=4, entry_ladder="range_uniform",
        sl_multiplier=1.0, activation_delay_minutes=0, pending_expiry_minutes=60,
        max_hold_minutes=240, lock_after_tp1=False,
        per_entry_targets=("TP1", "TP2", "TP3", "RUN"),
        trailing_close_distance=5.0, bep_after_move=5.0, bep_buffer=1.0,
    )
    rows = [
        _bar("2026.06.02", "11:00:00", 100, 100.2, 99.9, 100),
        _bar("2026.06.02", "11:01:00", 100, 101, 98, 100),     # fill all 4 @100
        _bar("2026.06.02", "11:02:00", 100, 111, 100, 110),    # TP1 -> leg1; BEP arms
        _bar("2026.06.02", "11:03:00", 110, 121, 110, 120),    # TP2 -> leg2
        _bar("2026.06.02", "11:04:00", 120, 131, 120, 130),    # TP3 -> leg3; leg4 engages, trail 126
        _bar("2026.06.02", "11:05:00", 130, 141, 130, 140),    # leg4 trail -> 136
        _bar("2026.06.02", "11:06:00", 140, 140, 135, 137),    # leg4 low 135 <= 136 -> TRAILING_STOP
    ]
    result = run_backtest([_sig()], _chart(tmp_path, rows), cfg)
    rk = _by_key(result)
    assert rk["2026-06-02#01.1"]["entry_status"] == "TP1"
    assert rk["2026-06-02#01.1"]["exit_price"] == 110
    assert rk["2026-06-02#01.2"]["entry_status"] == "TP2"
    assert rk["2026-06-02#01.2"]["exit_price"] == 120
    assert rk["2026-06-02#01.3"]["entry_status"] == "TP3"
    assert rk["2026-06-02#01.3"]["exit_price"] == 130
    # runner held past TP3 and trailed out (5.0 below the 141 high).
    assert rk["2026-06-02#01.4"]["entry_status"] == "TRAILING_STOP"
    assert rk["2026-06-02#01.4"]["exit_price"] == 136


def test_bep_after_move_locks_break_even_plus(tmp_path):
    cfg = replace(
        DEFAULT_CONFIG, entry_count=1, entry_ladder="range_uniform",
        sl_multiplier=1.0, activation_delay_minutes=0, pending_expiry_minutes=60,
        max_hold_minutes=240, lock_after_tp1=False,
        per_entry_targets=("TP3",), bep_after_move=5.0, bep_buffer=1.0,
    )
    rows = [
        _bar("2026.06.02", "11:00:00", 100, 100.2, 99.9, 100),
        _bar("2026.06.02", "11:01:00", 100, 101, 98, 100),     # fill @100
        _bar("2026.06.02", "11:02:00", 100, 106, 103, 105),    # +6 -> arm BEP, SL->101
        _bar("2026.06.02", "11:03:00", 105, 105, 100, 101),    # low 100 <= 101 -> BEP exit
    ]
    result = run_backtest([_sig()], _chart(tmp_path, rows), cfg)
    er = result["entry_rows"][0]
    assert er["entry_status"] == "BEP"
    assert er["exit_price"] == 101                              # entry 100 + buffer 1


def test_runner_trail_engages_at_configured_tp_not_from_entry(tmp_path):
    # Price reaches TP2 (120) and reverses, never touching TP3 (130).
    rows = [
        _bar("2026.06.02", "11:00:00", 100, 100.2, 99.9, 100),
        _bar("2026.06.02", "11:01:00", 100, 101, 98, 100),     # fill @100
        _bar("2026.06.02", "11:02:00", 100, 121, 100, 120),    # touch TP2 -> engage (trail 116)
        _bar("2026.06.02", "11:03:00", 120, 126, 120, 125),    # trail -> 121
        _bar("2026.06.02", "11:04:00", 125, 125, 119, 120),    # low 119 <= 121 -> TRAILING_STOP
    ]
    base = dict(entry_count=1, entry_ladder="range_uniform", sl_multiplier=1.0,
                activation_delay_minutes=0, pending_expiry_minutes=60, max_hold_minutes=240,
                lock_after_tp1=False, per_entry_targets=("RUN",), trailing_close_distance=5.0)

    # runner_trail_from=TP2: engages at TP2 and trails out at 121, before TP3.
    cfg_tp2 = replace(DEFAULT_CONFIG, runner_trail_from="TP2", **base)
    er = run_backtest([_sig()], _chart(tmp_path, rows, "tp2.csv"), cfg_tp2)["entry_rows"][0]
    assert er["entry_status"] == "TRAILING_STOP"
    assert er["exit_price"] == 121

    # Default TP3: never touched (peak 126), so the runner never engages -> stays OPEN.
    cfg_tp3 = replace(DEFAULT_CONFIG, runner_trail_from="TP3", **base)
    er3 = run_backtest([_sig()], _chart(tmp_path, rows, "tp3.csv"), cfg_tp3)["entry_rows"][0]
    assert er3["entry_status"] == "OPEN"


# --- CLI parsing ------------------------------------------------------------

def _load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "tools" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_entry_targets_parsing_and_validation():
    import pytest
    be = _load("backtest_explicit")
    assert be._parse_entry_targets(None, 8) == ()
    assert be._parse_entry_targets("TP1,TP1,TP2,TP2,TP3,TP3,RUN,RUN", 8) == \
        ("TP1", "TP1", "TP2", "TP2", "TP3", "TP3", "RUN", "RUN")
    with pytest.raises(SystemExit):
        be._parse_entry_targets("TP1,TP9", 2)          # bad token
    with pytest.raises(SystemExit):
        be._parse_entry_targets("TP1,TP2,TP3", 8)      # wrong length
