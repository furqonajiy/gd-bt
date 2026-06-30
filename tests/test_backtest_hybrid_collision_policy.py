"""tools/backtest_hybrid.py collision-policy integration.

PR #329 added the TSL18 collision layer to the M1 ``run_backtest``; this hotfix
wires the SAME layer into the hybrid/tick backtest so the quality-entry sweep
(which runs through ``backtest_hybrid.py``) actually honours collision flags. These
tests pin that the hybrid M1-fallback loop:

  * applies the collision layer exactly like run_backtest (a ``reject_opposite``
    run removes the opposite signal and never invents one);
  * publishes the collision summary on the result and the collision counters in
    the score-json (``build_score``) ONLY when a non-baseline policy ran;
  * stays byte-clean on a baseline policy -- no collision block on the result, no
    collision keys in the score-json, no collision fields stamped on the rows.

Deterministic: a tiny synthetic flat M1 chart + two opposing signals, ticks=None
so every signal takes the M1 fallback. (A tick-path fixture is not required for
this hotfix -- the wiring is shared by both paths.)
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

from trading.engine import CsvChartSource, StrategyConfig, parse_one_signal

ROOT = Path(__file__).resolve().parents[1]


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "tools" / f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


bh = _load("backtest_hybrid")

_HEADER = "<DATE>\t<TIME>\t<OPEN>\t<HIGH>\t<LOW>\t<CLOSE>\t<TICKVOL>\t<VOL>\t<SPREAD>"


def _bar(d, t, o, h, l, c):
    return f"{d}\t{t}\t{o}\t{h}\t{l}\t{c}\t100.0\t0.0\t2"


def _flat_chart(tmp_path):
    """40 flat one-minute bars at 100 on 2026-06-02 from 10:00 (no TP/SL ever
    hit, so an opened signal stays active for the next signal to collide with)."""
    rows = [_bar("2026.06.02", f"{10 + (i // 60):02d}:{i % 60:02d}:00", 100, 100.2, 99.9, 100)
            for i in range(40)]
    p = tmp_path / "C.csv"
    p.write_text("\n".join([_HEADER, *rows]) + "\n", encoding="utf-8")
    return CsvChartSource([p])


def _two_opposing_signals():
    buy = parse_one_signal(
        "1. BUY XAUUSD 100 - 100 SL 90 TP1 110 TP2 120 TP3 130 10:00 AM",
        source_date="2026-06-02", source_offset=3)
    sell = parse_one_signal(
        "2. SELL XAUUSD 100 - 100 SL 110 TP1 90 TP2 80 TP3 70 10:10 AM",
        source_date="2026-06-02", source_offset=3)
    return [buy, sell]


_GEO = dict(initial_capital=5000.0, sizing_mode="risk", risk_per_signal=0.01,
            minimum_lot=0.01, entry_count=1, entry_ladder="range_uniform",
            sl_multiplier=1.0, activation_delay_minutes=0,
            pending_expiry_minutes=60, max_hold_minutes=60)


def test_hybrid_m1_fallback_respects_reject_opposite(tmp_path):
    base = bh.run_hybrid_backtest(_two_opposing_signals(), _flat_chart(tmp_path), None,
                                  StrategyConfig(**_GEO))
    gated = bh.run_hybrid_backtest(_two_opposing_signals(), _flat_chart(tmp_path), None,
                                   StrategyConfig(opposite_signal_policy="reject_opposite", **_GEO))
    # ticks=None -> every signal took the M1 fallback path
    assert base["data_sources"]["tick_signals"] == 0
    assert gated["data_sources"]["tick_signals"] == 0
    # baseline makes no interventions -> no collision block; reject_opposite -> block present
    assert "collision_policy" not in base
    assert "collision_policy" in gated
    # reject_opposite removes exactly one (the opposite) signal, never invents one
    assert gated["signals_included"] == base["signals_included"] - 1
    assert {r["signal_key"] for r in gated["rows"]} <= {r["signal_key"] for r in base["rows"]}
    assert gated["collision_policy"]["opposite_collisions_rejected"] == 1
    # surviving rows carry the collision reporting fields
    for r in gated["rows"]:
        assert "collision_type" in r
        assert "collision_policy_action" in r


def test_hybrid_score_json_includes_collision_metrics_when_non_baseline(tmp_path):
    gated = bh.run_hybrid_backtest(_two_opposing_signals(), _flat_chart(tmp_path), None,
                                   StrategyConfig(opposite_signal_policy="reject_opposite", **_GEO))
    score = bh.build_score(gated)
    for k in ("collision_policy_pnl", "opposite_collisions_total",
              "opposite_collisions_allowed", "opposite_collisions_rejected",
              "opposite_collisions_flipped", "opposite_collisions_profit_bank_rearmed",
              "same_side_clusters_total", "same_side_clusters_accepted",
              "same_side_clusters_rejected", "same_side_clusters_downsized",
              "max_same_side_cluster_risk", "max_opposite_exposure"):
        assert k in score, f"missing collision metric {k} in score-json"
    assert score["opposite_collisions_rejected"] == 1


def test_hybrid_baseline_has_no_collision_block(tmp_path):
    base = bh.run_hybrid_backtest(_two_opposing_signals(), _flat_chart(tmp_path), None,
                                  StrategyConfig(**_GEO))
    assert "collision_policy" not in base
    score = bh.build_score(base)
    # a baseline / pure-M1 score-json is unchanged: no collision keys at all
    assert "collision_policy_pnl" not in score
    assert "opposite_collisions_total" not in score
    # and the rows carry NO collision fields (apply_collision_to_built never ran)
    for r in base["rows"]:
        assert "collision_type" not in r
