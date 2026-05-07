"""Tests for the M1 archive merge/overwrite logic."""
from __future__ import annotations
from pathlib import Path

import pandas as pd
import pytest

from xauusd_trading.mt5_adapter import (
    _MT5_EXPORT_COLUMNS, _merge_with_existing,
)


def _row(date: str, time: str, value: float = 1.0) -> dict:
    return {
        "<DATE>": date, "<TIME>": time, "<OPEN>": value, "<HIGH>": value + 0.5,
        "<LOW>": value - 0.5, "<CLOSE>": value + 0.1,
        "<TICKVOL>": 100, "<VOL>": 0, "<SPREAD>": 25,
    }


def test_merge_creates_new_file_when_none_exists(tmp_path: Path):
    new = pd.DataFrame([_row("2026.04.01", "10:00:00", 4500.0)])
    result = _merge_with_existing(new, tmp_path / "missing.csv")
    assert len(result) == 1
    assert result.iloc[0]["<OPEN>"] == 4500.0


def test_merge_preserves_existing_bars_not_in_new_fetch(tmp_path: Path):
    """The boundary-month case: prior fetch saved early-month bars, new
    fetch only covers later-month bars (because MT5 window has shifted).
    Merge must keep both halves."""
    p = tmp_path / "month.csv"
    pd.DataFrame([
        _row("2026.04.01", "10:00:00", 4500.0),
        _row("2026.04.05", "12:00:00", 4510.0),
    ]).to_csv(p, sep="\t", index=False)
    new = pd.DataFrame([
        _row("2026.04.20", "08:00:00", 4520.0),
        _row("2026.04.25", "16:00:00", 4530.0),
    ])
    result = _merge_with_existing(new, p)
    assert len(result) == 4
    dates = result["<DATE>"].tolist()
    assert dates == ["2026.04.01", "2026.04.05", "2026.04.20", "2026.04.25"]


def test_merge_keeps_new_value_on_timestamp_collision(tmp_path: Path):
    """If MT5 returns corrected/updated values for a timestamp we already
    have, the new fetch wins."""
    p = tmp_path / "month.csv"
    pd.DataFrame([_row("2026.04.10", "09:00:00", 4500.0)]).to_csv(p, sep="\t", index=False)
    new = pd.DataFrame([_row("2026.04.10", "09:00:00", 9999.0)])
    result = _merge_with_existing(new, p)
    assert len(result) == 1
    assert result.iloc[0]["<OPEN>"] == 9999.0


def test_columns_match_mt5_export_format(tmp_path: Path):
    new = pd.DataFrame([_row("2026.04.01", "10:00:00")])
    result = _merge_with_existing(new, tmp_path / "x.csv")
    assert list(result.columns) == _MT5_EXPORT_COLUMNS
