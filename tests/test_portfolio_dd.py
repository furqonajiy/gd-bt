"""The portfolio DD auditor must see drawdown the realized curve hides.

Two cases the realized (one-signal-at-a-time) curve cannot capture:
* several positions open and underwater at once (concurrency),
* a single position dipping mid-trade before exiting flat (intra-trade float).
Both are deterministic, so these run everywhere (no market data needed).
"""
from __future__ import annotations

import importlib.util
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load(module_name: str):
    spec = importlib.util.spec_from_file_location(module_name, ROOT / "tools" / f"{module_name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


dd = _load("backtest_portfolio_dd")


def _times(n):
    return [datetime(2024, 1, 1, 0, m) for m in range(n)]


def test_two_overlapping_losers_deepen_mtm_vs_realized():
    times = _times(5)
    closes = [100.0, 99.0, 98.0, 99.5, 100.0]
    spreads = [0.0] * 5
    mk = lambda: dict(side="BUY", entry_price=100.0, lot=1.0,
                      realized=(99.5 - 100.0) * 100.0, fill_idx=0, exit_idx=3)
    entries = [mk(), mk()]
    mtm, conc, _ = dd.mtm_drawdown(entries, times, closes, spreads, 1000.0)
    realized = dd.realized_drawdown_exit_ordered(entries, 1000.0)
    assert conc == 2
    # bar 2: both float (98-100)*100 = -200 -> eq 600 -> -40%
    assert abs(mtm + 40.0) < 1e-6
    # realized lands -50, -50 sequentially -> -10%
    assert abs(realized + 10.0) < 1e-6
    assert mtm < realized


def test_intratrade_floating_dip_is_invisible_to_realized():
    times = _times(5)
    closes = [100.0, 97.0, 99.0, 100.0, 100.0]
    spreads = [0.0] * 5
    # one BUY, enters 100, exits flat at 100 (bar 3): realized 0, but floats to -300 at bar 1
    entries = [dict(side="BUY", entry_price=100.0, lot=1.0, realized=0.0, fill_idx=0, exit_idx=3)]
    mtm, conc, _ = dd.mtm_drawdown(entries, times, closes, spreads, 1000.0)
    realized = dd.realized_drawdown_exit_ordered(entries, 1000.0)
    assert conc == 1
    assert abs(realized) < 1e-9            # realized never dips
    assert abs(mtm + 30.0) < 1e-6          # (97-100)*100 = -300 -> -30%
    assert mtm < realized


def test_sell_floating_uses_ask_side():
    times = _times(3)
    closes = [100.0, 101.0, 100.0]
    spreads = [0.5, 0.5, 0.5]
    # SELL entered at 100; at bar 1 close 101 -> Ask 101.5 -> float (100-101.5)*100 = -150
    entries = [dict(side="SELL", entry_price=100.0, lot=1.0, realized=0.0, fill_idx=0, exit_idx=2)]
    mtm, _, _ = dd.mtm_drawdown(entries, times, closes, spreads, 1000.0)
    assert abs(mtm + 15.0) < 1e-6          # -150 on 1000 = -15%