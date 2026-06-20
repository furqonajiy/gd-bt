"""Shared-SL option: every entry in the ladder defends one common stop level.

Default (shared_sl off) keeps the per-entry staggered stop. With shared_sl on,
all entries get the same initial_sl (anchored on the first/reference entry) in
the position construction AND the engine plan (the live order SL), and
risk-sizing uses each leg's real distance to that shared level.
"""
from __future__ import annotations

import importlib.util
from dataclasses import replace
from pathlib import Path

from trading.engine import (
    CONTRACT_SIZE_OZ, DEFAULT_CONFIG, compute_lot, entry_stop_levels,
    open_position, parse_one_signal,
)
from trading.engine.strategy.engine import _build_new_signal_plan

ROOT = Path(__file__).resolve().parents[1]

# 8-entry ladder, like the reported case: same SL, different entries.
_SIGNAL = "1. BUY XAUUSD 4364 - 4366 SL 4360 TP1 4369 TP2 4374 TP3 4381 1:39 PM"


def _cfg(shared: bool):
    return replace(
        DEFAULT_CONFIG, sizing_mode="risk", initial_capital=5000.0,
        risk_per_signal=0.05, entry_count=8, entry_ladder="range_to_sl",
        entry_sl_gap=0.5, sl_multiplier=2.1, minimum_lot=0.01, lot_step=0.01,
        shared_sl=shared,
    )


def _sig():
    return parse_one_signal(_SIGNAL, "2026-01-02", 7)


def test_entry_stop_levels_shared_vs_per_entry():
    prices = [4366.0, 4365.0, 4364.0]
    base = 6.3
    per = entry_stop_levels("BUY", prices, base, _cfg(False))
    assert per == [p - base for p in prices]                 # staggered stops
    shared = entry_stop_levels("BUY", prices, base, _cfg(True))
    assert shared == [prices[0] - base] * 3                  # one common level


def test_open_position_shares_one_sl():
    sig = _sig()
    per = open_position(sig, 5000.0, _cfg(False))
    shared = open_position(sig, 5000.0, _cfg(True))

    per_sls = [round(e.initial_sl, 6) for e in per.entries]
    shared_sls = [round(e.initial_sl, 6) for e in shared.entries]

    assert len(set(per_sls)) > 1                             # staggered/distinct
    assert len(set(shared_sls)) == 1                         # all identical
    # the shared level equals the first (reference) entry's per-entry stop
    assert shared_sls[0] == per_sls[0]
    assert shared.shared_sl_level == shared.entries[0].initial_sl
    assert per.shared_sl_level is None


def test_engine_plan_orders_share_sl_for_live():
    # PlannedOrder.initial_sl is what the live executor places, so it must also
    # collapse to a single level in shared mode.
    plan = _build_new_signal_plan(_sig(), 5000.0, _cfg(True), CONTRACT_SIZE_OZ, now=None, chart=None)
    assert plan.action == "FOLLOW"
    assert len({round(o.initial_sl, 6) for o in plan.orders}) == 1


def test_risk_sizing_uses_distance_to_shared_level():
    # Shared mode total risk = sum |entry - shared|; deeper legs risk less than
    # the constant base_stop_distance they'd risk per-entry, so the lot differs.
    sig = _sig()
    lot_per, _ = compute_lot(5000.0, sig, _cfg(False))
    lot_shared, _ = compute_lot(5000.0, sig, _cfg(True))
    assert lot_per > 0 and lot_shared > 0
    assert lot_per != lot_shared


# --- explicit runners expose --shared-sl (so it can be swept) ---------------

def _load_tool(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "tools" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_COMMON = [
    "--initial-capital", "5000", "--sizing-mode", "risk", "--lot", "0.01",
    "--risk", "0.05", "--minimum-lot", "0.01", "--lot-step", "0.01",
    "--bonus-per-closed-lot", "3", "--entries", "8",
    "--entry-ladder", "range_to_sl", "--entry-sl-gap", "0.5",
    "--activation-delay", "0", "--pending-expiry", "180", "--max-hold", "240",
    "--sl-multiplier", "2.1", "--final-target", "TP3",
    "--lock-after-tp1", "true", "--lock-after-tp2", "true",
    "--tp1-lock-delay-minutes", "10", "--tp2-lock-delay-minutes", "0",
    "--profit-lock-mode", "tp_levels", "--bep-trigger-distance", "3",
    "--tp1-lock-fraction", "0.5", "--tp2-lock-target", "TP1",
    "--runner-after-tp3", "false", "--tp3-lock-target", "TP2",
    "--trailing-open-distance", "0", "--trailing-close-distance", "0",
]


def test_backtest_explicit_shared_sl_flows_into_config():
    be = _load_tool("backtest_explicit")
    base = ["--signals", "s.txt", "--charts", "c.csv", "--output-dir", "out",
            "--max-drawdown-limit-pct", "50", "--progress-interval-seconds", "0"]
    cfg_off = be.config_from_args(be.build_parser().parse_args(base + _COMMON))
    assert cfg_off.shared_sl is False
    cfg_on = be.config_from_args(be.build_parser().parse_args(base + _COMMON + ["--shared-sl", "true"]))
    assert cfg_on.shared_sl is True


def test_auto_explicit_shared_sl_flows_into_config():
    ae = _load_tool("auto_explicit")
    base = ["--signals", "s.txt", "--positions-json", "p.json", "--watch-interval", "5",
            "--mt5-symbol", "XAUUSD", "--mt5-server-offset", "3", "--mt5-history-bars", "5000"]
    cfg_off = ae.config_from_args(ae.build_parser().parse_args(base + _COMMON))
    assert cfg_off.shared_sl is False
    cfg_on = ae.config_from_args(ae.build_parser().parse_args(base + _COMMON + ["--shared-sl", "true"]))
    assert cfg_on.shared_sl is True
