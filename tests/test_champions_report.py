"""Unit tests for the champion/challenger deploy report (tools/champions_report).

These pin the deploy logic the self-regime-grid aggregate relies on:
  * best_challenger respects the DD<=40% gate and ranks OOS then edge;
  * update_champion is MONOTONIC (never regresses across passes);
  * render_champions_md emits HOLD vs SWITCH correctly, flags the live regime,
    and the rendered champion CLI is a runnable backtest command whose flags all
    exist in tools/backtest_explicit.py.

No network, no MT5 -- pure render/decision logic.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import tools.champions_report as cr

ROOT = Path(__file__).resolve().parents[1]


def _chrow(feed, edge, oos, dd, cfg):
    return {
        "_feed": feed,
        "fixed_no_bonus_profit": edge,
        "oos_fixed_no_bonus_profit": oos,
        "concurrent_risk_max_dd_pct": dd,
        "config": cfg,
        "config_json": json.dumps(cfg, sort_keys=True),
    }


def test_best_challenger_respects_dd_gate_and_ranks_oos_then_edge():
    rows = [
        _chrow("adC_wideSL", 9000.0, 9000.0, 55.0, {"entry_count": 8}),   # DD>40 excluded
        _chrow("adB_tightSL", 4000.0, 1500.0, 30.0, {"entry_count": 6}),  # passes, lower oos
        _chrow("adE_farTP", 3000.0, 1500.0, 20.0, {"entry_count": 4}),    # ties oos, lower edge
    ]
    best = cr.best_challenger(rows, dd_gate=40.0)
    assert best is not None
    assert best["_feed"] == "adB_tightSL"  # tie on oos broken by higher edge

    # No survivor when every row breaches the gate.
    assert cr.best_challenger(
        [_chrow("x", 1.0, 1.0, 99.0, {})], dd_gate=40.0) is None


def test_update_champion_is_monotonic(tmp_path):
    good = _chrow("adE_farTP", 1800.0, 900.0, 22.0, {"entry_count": 4})
    ch = cr.update_champion(tmp_path, "R3strong", cr.best_challenger([good]))
    assert ch["oos"] == 900.0

    # A strictly worse challenger must NOT replace the stored champion.
    worse = _chrow("adA_base", 100.0, 50.0, 10.0, {"entry_count": 1})
    ch2 = cr.update_champion(tmp_path, "R3strong", cr.best_challenger([worse]))
    assert ch2["oos"] == 900.0
    assert ch2["feed"] == "adE_farTP"

    # A strictly better challenger DOES replace it.
    better = _chrow("adG_wideSL_farTP", 5000.0, 2500.0, 15.0, {"entry_count": 8})
    ch3 = cr.update_champion(tmp_path, "R3strong", cr.best_challenger([better]))
    assert ch3["oos"] == 2500.0
    assert ch3["feed"] == "adG_wideSL_farTP"


def test_render_hold_and_switch(tmp_path):
    inc_hold = {"edge": 7726.34, "oos": 3461.30, "dd": 72.53,
                "config": {"entry_count": 6, "sl_multiplier": 2.1,
                           "tp1_lock_delay_minutes": 24}}
    inc_switch = {"edge": 500.0, "oos": 200.0, "dd": 35.0,
                  "config": {"entry_count": 6, "sl_multiplier": 2.1,
                             "tp1_lock_delay_minutes": 24}}

    # R4parab (live): best DD-passing challenger oos 1500 < incumbent 3461 -> HOLD.
    r4 = cr.best_challenger([
        _chrow("adC_wideSL", 9000.0, 9000.0, 55.0, {"entry_count": 8}),
        _chrow("adB_tightSL", 4000.0, 1500.0, 30.0, {"entry_count": 6}),
    ])
    # R3strong: challenger oos 900 > incumbent 200 -> SWITCH.
    r3 = cr.best_challenger([
        _chrow("adE_farTP", 1800.0, 900.0, 22.0,
               {"entry_count": 4, "sl_multiplier": 2.1, "final_target": "TP3",
                "risk_per_signal": 0.03}),
    ])
    ch4 = cr.update_champion(tmp_path, "R4parab", r4)
    ch3 = cr.update_champion(tmp_path, "R3strong", r3)

    md = cr.render_champions_md(
        ["R1quiet", "R2bull", "R3strong", "R4parab"],
        {"R4parab": inc_hold, "R3strong": inc_switch},
        {"R4parab": ch4, "R3strong": ch3},
    )

    assert "HOLD (keep incumbent)" in md
    assert "SWITCH" in md
    assert ">>> RUN THIS NOW <<<" in md           # live regime flagged
    assert "R4parab" in md and "live regime: R4parab" in md


def test_feed_name_resolves_to_archive_path():
    assert cr.feed_signals("adE_farTP") == "generated/adaptive_adE_farTP.txt"
    # An already-resolved path round-trips unchanged.
    assert cr.feed_signals("generated/self_better.txt") == "generated/self_better.txt"


def test_rendered_cli_is_runnable_backtest_explicit():
    cfg = {"entry_count": 4, "sl_multiplier": 2.1, "tp1_lock_delay_minutes": 24,
           "final_target": "TP3", "risk_per_signal": 0.03}
    # Pass the matrix feed NAME; the renderer must resolve it to the archive path.
    cli = cr.render_champion_cli(cfg, regime="R3strong", feed="adE_farTP")
    assert cli.lstrip().startswith("python ")
    assert "generated/adaptive_adE_farTP.txt" in cli
    # This regime's charts are substituted, not the full-history glob.
    assert "data/XAUUSD_M1_2025*_ELEV8.csv" in cli

    if "backtest_explicit.py" in cli:
        # Orchestrate renderer available (the CI runtime path): every flag the CLI
        # emits must exist in backtest_explicit's parser, and the regime start is
        # substituted.
        help_text = subprocess.run(
            [sys.executable, "tools/backtest_explicit.py", "--help"],
            cwd=ROOT, capture_output=True, text=True).stdout
        for tok in cli.split():
            if tok.startswith("--"):
                assert tok in help_text, f"unknown flag {tok} in rendered CLI"
        assert "2025-01-01" in cli
    else:
        # Fallback path (sweep2021.orchestrate not importable in this tree): the
        # plain CLI line is still runnable and regime-targeted.
        assert "python -m xauusd_trading.cli backtest" in cli
        assert "--entries 4" in cli
