"""Test tools/resample_m1_to_m15.resample_to_m15 (pure OHLC aggregation)."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("resample_m1_to_m15", ROOT / "tools" / "resample_m1_to_m15.py")
rs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rs)


def test_resample_aggregates_ohlc_on_15min_buckets():
    idx = pd.date_range("2026-01-15 10:00:00", periods=16, freq="1min")  # 10:00 .. 10:15
    m1 = pd.DataFrame({
        "OPEN": [100.0 + i for i in range(16)],
        "HIGH": [101.0 + i for i in range(16)],
        "LOW": [99.0 + i for i in range(16)],
        "CLOSE": [100.5 + i for i in range(16)],
        "TICKVOL": [1.0] * 16,
        "VOL": [0.0] * 16,
        "SPREAD": [28] * 16,
    }, index=idx)

    out = rs.resample_to_m15(m1)

    # Bucket 10:00 spans the first 15 bars (10:00..10:14); 10:15 holds the 16th.
    b0 = out.loc[pd.Timestamp("2026-01-15 10:00:00")]
    assert b0.OPEN == 100.0                 # first
    assert b0.HIGH == 101.0 + 14            # max over bars 0..14
    assert b0.LOW == 99.0                   # min over bars 0..14
    assert b0.CLOSE == 100.5 + 14           # last
    assert b0.TICKVOL == 15.0               # summed

    b1 = out.loc[pd.Timestamp("2026-01-15 10:15:00")]
    assert b1.OPEN == 100.0 + 15
    assert b1.CLOSE == 100.5 + 15
    assert b1.TICKVOL == 1.0