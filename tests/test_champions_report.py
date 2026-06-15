"""Unit tests for the champion/challenger deploy report (tools/champions_report).

These pin the deploy logic the self-regime-grid aggregate relies on:
  * best_challenger respects the DD<=40% gate + OOS>0 guard and ranks by
    net+bonus (the deploy objective), then OOS, then edge;
  * stretch_challenger surfaces a DD 40-50% config only when it beats the
    DD<=40% champion's net+bonus by the configured margin;
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


def _chrow(feed, edge, oos, dd, cfg, net=None):
    row = {
        "_feed": feed,
        "fixed_no_bonus_profit": edge,
        "oos_fixed_no_bonus_profit": oos,
        "concurrent_risk_max_dd_pct": dd,
        "config": cfg,
        "config_json": json.dumps(cfg, sort_keys=True),
    }
    if net is not None:
        row["risk_net_profit_with_bonus"] = net
    return row


def test_best_challenger_ranks_by_net_bonus_then_oos():
    # The deploy objective is net+bonus: the highest-net DD<=40 / OOS>0 row wins
    # even when another compliant row has higher OOS/edge.
    rows = [
        _chrow("adA_base", 9000.0, 5000.0, 30.0, {"entry_count": 4}, net=120000.0),
        _chrow("scalper24", 4000.0, 1500.0, 38.0, {"entry_count": 6}, net=600000.0),
        _chrow("adC_wideSL", 9000.0, 9000.0, 55.0, {"entry_count": 8}, net=900000.0),
    ]
    best = cr.best_challenger(rows, dd_gate=40.0)
    assert best["_feed"] == "scalper24"      # highest net+bonus among DD<=40
    # The DD>40 row (900k net) is excluded by the gate.


def test_best_challenger_requires_positive_oos():
    # An in-sample blowup with huge net+bonus but OOS<=0 is rejected (overfit).
    rows = [_chrow("x", 9000.0, -10.0, 20.0, {}, net=999999.0)]
    assert cr.best_challenger(rows, dd_gate=40.0) is None


def test_strictly_beats_uses_net_bonus():
    hi = {"net_bonus": 600000.0, "oos": 100.0, "edge": 100.0}
    lo = {"net_bonus": 400000.0, "oos": 9000.0, "edge": 9000.0}
    assert cr.strictly_beats(hi, lo)        # higher net wins despite lower oos
    assert not cr.strictly_beats(lo, hi)


def test_stretch_challenger_surfaces_only_with_margin():
    champ40 = {"net_bonus": 400000.0, "oos": 1000.0, "dd": 35.0}
    rows = [
        _chrow("scalper24", 4000.0, 1500.0, 35.0, {}, net=400000.0),   # the 40% champ
        _chrow("scalperwide24", 8000.0, 2000.0, 47.0, {}, net=560000.0),  # +40% @ DD47
    ]
    s = cr.stretch_challenger(rows, champ40)
    assert s is not None and s["_feed"] == "scalperwide24" and cr._dd(s) == 47.0

    # If the high-DD config only beats the champ by a hair (<25%), no stretch.
    rows2 = [_chrow("scalperwide24", 8000.0, 2000.0, 47.0, {}, net=440000.0)]
    assert cr.stretch_challenger(rows2, champ40) is None


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


def test_render_deployment_cli_full_format():
    """The deployment CLI carries the header, all three sections (generate /
    backtest / live), the correct feed + regime, and a runnable auto_explicit
    live command -- the GENERATE/BACKTEST/LIVE deployment format."""
    cfg = {"entry_count": 4, "sl_multiplier": 1.61, "tp1_lock_delay_minutes": 24,
           "final_target": "TP3", "risk_per_signal": 0.05575}
    text = cr.render_deployment_cli(
        cfg, regime="R4parab", feed="breakout",
        edge=6031.2, oos=1742.8, dd=35.95)
    assert text.strip()  # non-empty
    # Header block: regime, champion line, detector, risk caveat.
    assert "R4parab champion" in text
    assert "feed=breakout" in text
    assert "edge $6,031" in text and "OOS $1,743" in text and "DD 36.0%" in text
    assert "python tools/regime_auto.py" in text
    assert "<=5% cap" in text or "--risk 0.05" in text
    # Section 1 GENERATE: the breakout generator + its archive output.
    assert "1. GENERATE" in text
    assert "tools/generate_breakout_signals.py" in text
    assert "generated/adaptive_breakout.txt" in text
    # Section 2 BACKTEST: reuses render_champion_cli (regime slice + charts).
    assert "2. BACKTEST" in text
    assert ("tools/backtest_explicit.py" in text
            or "xauusd_trading.cli backtest" in text)
    assert "data/XAUUSD_M1_2026*_ELEV8.csv" in text
    # Section 3 LIVE: auto_explicit with the live tail; backtest-only flags gone.
    assert "3. LIVE AUTO EXECUTOR" in text
    assert "tools/auto_explicit.py" in text
    assert "--positions-json positions_R4parab.json" in text
    assert "--reopen-missing-positions true" in text
    assert "--watch-interval 15" in text
    assert "--charts" not in text.split("3. LIVE")[1]
    assert "--output-dir" not in text.split("3. LIVE")[1]
    assert "--max-drawdown-limit-pct" not in text.split("3. LIVE")[1]


def test_render_deployment_cli_feed_routes_to_generator():
    """meanrev and ad* feeds route to their generators; backtest signals match."""
    mr = cr.render_deployment_cli(
        {"entry_count": 6}, regime="R3strong", feed="meanrev",
        edge=100.0, oos=50.0, dd=20.0)
    assert "tools/generate_meanrev_signals.py" in mr
    assert "generated/adaptive_meanrev.txt" in mr
    assert "R3strong" in mr

    adf = cr.render_deployment_cli(
        {"entry_count": 6}, regime="R3strong", feed="adF_tightSL_closeTP",
        edge=13331.0, oos=2242.0, dd=29.7)
    assert "tools/generate_adaptive_self_signals.py" in adf
    assert "generated/adaptive_adF_tightSL_closeTP.txt" in adf


def test_write_deployment_cli_files_champion_and_placeholder(tmp_path):
    """write_deployment_cli_files emits cli/best_<regime>.txt: full deployment
    text when a champion exists, the no-champion note otherwise."""
    out = tmp_path / "sweep_regime_out_grid"
    out.mkdir()
    champ = {"feed": "breakout", "edge": 6031.0, "oos": 1742.0, "dd": 35.9,
             "config": {"entry_count": 4, "sl_multiplier": 1.61}}
    champions = {"R3strong": None, "R4parab": champ}
    cli_dir = cr.write_deployment_cli_files(
        out, ["R3strong", "R4parab"], champions)
    assert cli_dir == tmp_path / "cli"

    r4 = (cli_dir / "best_R4parab.txt").read_text()
    assert "tools/auto_explicit.py" in r4
    assert "feed=breakout" in r4

    r3 = (cli_dir / "best_R3strong.txt").read_text()
    assert "no DD<=40% champion yet" in r3
    assert "incumbent" in r3


def test_noncompliant_incumbent_is_disqualified():
    """An incumbent that exceeds the DD gate cannot HOLD against a compliant
    champion, even if its raw OOS is higher (DD<=40% is a hard constraint)."""
    champ = {"feed": "adD_closeTP", "edge": 4740.0, "oos": 1163.0, "dd": 37.4,
             "config": {"entry_count": 3, "sl_multiplier": 1.3}}
    inc = {"edge": 7726.0, "oos": 3461.0, "dd": 72.5, "config": {}}
    md = cr.render_champions_md(
        regimes=["R4parab"], champions={"R4parab": champ},
        incumbents={"R4parab": inc}, live_regime="R4parab")
    row = [ln for ln in md.splitlines() if ln.startswith("| **R4parab**")][0]
    assert "SWITCH" in row and "exceeds" in row
    assert "HOLD" not in row


def test_compliant_incumbent_beats_weaker_champion():
    """A DD-compliant incumbent with higher OOS holds against a weaker champion."""
    champ = {"feed": "adD_closeTP", "edge": 500.0, "oos": 200.0, "dd": 30.0,
             "config": {"entry_count": 3}}
    inc = {"edge": 2000.0, "oos": 900.0, "dd": 35.0, "config": {}}
    md = cr.render_champions_md(
        regimes=["R4parab"], champions={"R4parab": champ},
        incumbents={"R4parab": inc}, live_regime="R4parab")
    row = [ln for ln in md.splitlines() if ln.startswith("| **R4parab**")][0]
    assert "HOLD" in row
