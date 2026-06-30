"""Regression: the scalper generator must not crash when progress logging is
disabled. The TWL25 loss sweep passes ``--progress-interval-seconds 0
--progress-every-rows 0`` to silence per-row logging; the scan loop used to
evaluate ``i % args.progress_every_rows`` unconditionally, raising
``ZeroDivisionError('integer modulo by zero')`` on the very first row (every
sweep cell errored with tick_pnl/score = -1e18). With progress disabled the
modulo must never be evaluated.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
for p in (str(ROOT), str(ROOT / "tools")):
    if p not in sys.path:
        sys.path.insert(0, p)

import generate_scalper_signals as gen  # noqa: E402


def _synthetic_chart(rows: int = 60) -> pd.DataFrame:
    base = datetime(2026, 6, 1, 8, 0)
    data = []
    price = 4500.0
    for i in range(rows):
        # gentle zig-zag so indicators have variation but nothing degenerate
        price += 0.5 if i % 2 == 0 else -0.4
        data.append({
            "time": base + timedelta(minutes=i),
            "open": price,
            "high": price + 1.0,
            "low": price - 1.0,
            "close": price + (0.2 if i % 2 == 0 else -0.2),
            "spread": 20,
        })
    return pd.DataFrame(data)


def _args(progress_interval: str, progress_rows: str) -> "object":
    return gen.build_parser().parse_args([
        "--charts", "unused.csv",
        "--output", "unused.txt",
        "--signal-tz", "7",
        "--progress-interval-seconds", progress_interval,
        "--progress-every-rows", progress_rows,
    ])


def test_generate_signals_progress_zero_does_not_crash():
    df = _synthetic_chart()
    # The exact flags the TWL25 sweep passes -- both zero -> progress fully off.
    args = _args("0", "0")
    signals = gen.generate_signals(df, args)
    assert isinstance(signals, list)  # ran to completion, no ZeroDivisionError


def test_generate_signals_progress_rows_zero_interval_on_does_not_crash():
    # Mixed: interval > 0 but rows == 0. progress_enabled requires BOTH > 0, so
    # progress stays off and the modulo must still never be evaluated.
    df = _synthetic_chart()
    args = _args("5", "0")
    signals = gen.generate_signals(df, args)
    assert isinstance(signals, list)


def test_generate_signals_progress_enabled_still_works():
    # Both > 0 -> progress path is exercised (and still must not crash).
    df = _synthetic_chart()
    args = _args("15", "10")
    signals = gen.generate_signals(df, args)
    assert isinstance(signals, list)
