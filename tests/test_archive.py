"""Tests for the M1 archive merge logic."""
from __future__ import annotations
from pathlib import Path

import pandas as pd

from trading.engine import (
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
    """Boundary-month case: prior fetch saved early-month bars; new fetch
    only covers later bars (MT5 window has shifted). Both halves must
    survive the merge."""
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
    """MT5 sometimes returns corrected values for a timestamp we already
    have. New fetch wins."""
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


def test_count_columns_render_as_integers(tmp_path: Path):
    """TICKVOL/VOL/SPREAD must serialize as 100/0/25, not 100.0/0.0/25.0 --
    matching a manual MT5 'Save As CSV' -- even when the existing file (or the
    MT5 build) carried them as floats."""
    p = tmp_path / "month.csv"
    existing = pd.DataFrame([_row("2026.04.01", "10:00:00", 4500.0)])
    for col in ("<TICKVOL>", "<VOL>", "<SPREAD>"):
        existing[col] = existing[col].astype(float)   # the float source
    existing.to_csv(p, sep="\t", index=False)
    assert "\t100.0\t0.0\t25.0" in p.read_text()       # confirm the problem pre-merge

    new = pd.DataFrame([_row("2026.04.02", "10:00:00", 4510.0)])
    result = _merge_with_existing(new, p)

    for col in ("<TICKVOL>", "<VOL>", "<SPREAD>"):
        assert str(result[col].dtype) == "int64"
    out = tmp_path / "out.csv"
    result.to_csv(out, sep="\t", index=False)
    text = out.read_text()
    assert "\t100\t0\t25" in text                       # integer rendering
    assert "\t100.0\t" not in text and "\t0.0\t" not in text