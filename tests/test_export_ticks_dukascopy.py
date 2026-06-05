"""Tests for tools/export_ticks_dukascopy.py (no network).

Covers LZMA round-trip decode, exact ELEV8-schema row formatting, the
fresh/merge/header-only orchestration via an injected fake fetcher, and that the
output parses under the same contract tick_backtest.load_ticks uses (lowercased
<...> headers, GMT+3 DATE+TIME, %Y.%m.%d %H:%M:%S.%f).
"""
from __future__ import annotations

import argparse
import calendar
import importlib.util
import lzma
from datetime import datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("export_ticks_dukascopy", ROOT / "tools" / "export_ticks_dukascopy.py")
duka = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(duka)


_HOUR = datetime(2026, 4, 1, 10)            # broker GMT+3 hour
_URL = duka._hour_url("XAUUSD", datetime(2026, 4, 1, 7))  # UTC = chart - 3
_BASE_MS = calendar.timegm(_HOUR.timetuple()) * 1000


def _bi5(records):
    # records: list of (ms_off, ask_pts, bid_pts, ask_vol, bid_vol)
    raw = b"".join(duka._RECORD.pack(*r) for r in records)
    return lzma.compress(raw, format=lzma.FORMAT_ALONE)


def _fake_fetch(mapping):
    def fetch(url):
        return mapping.get(url)
    return fetch


def _args(tmp_path, **over):
    base = dict(symbol="XAUUSD", output_dir=str(tmp_path), server_offset=3, price_scale=1000.0,
                sleep_seconds=0.0, timeout=5.0, retries=0, progress=False, overwrite=False, merge=False)
    base.update(over)
    return argparse.Namespace(**base)


def _stats():
    return {"failed": 0, "scale_checked": False}


def test_decompress_roundtrip():
    raw = b"".join(duka._RECORD.pack(1500, 4441860, 4441580, 1.0, 2.0) for _ in range(3))
    assert duka._decompress_bi5(lzma.compress(raw, format=lzma.FORMAT_ALONE)) == raw


def test_bi5_to_rows_exact_values():
    raw = duka._decompress_bi5(_bi5([(1500, 4441860, 4441580, 1.0, 2.0)]))
    (row,) = list(duka._bi5_to_rows(raw, 1000.0, _HOUR))
    assert row["<DATE>"] == "2026.04.01"
    assert row["<TIME>"] == "10:00:01.500"
    assert row["<TIME_MSC>"] == str(_BASE_MS + 1500)
    assert row["<BID>"] == "4441.58"
    assert row["<ASK>"] == "4441.86"
    assert row["<SPREAD>"] == "28"          # round(0.28 / 0.01)
    assert row["<VOLUME_REAL>"] == "3"
    assert row["<FLAGS>"] == "6"


def test_fresh_fetch_writes_schema_compatible_file(tmp_path):
    blob = _bi5([(1500, 4441860, 4441580, 1.0, 2.0),
                 (2500, 4441900, 4441620, 1.0, 1.0),
                 (3000, 4441700, 4441450, 0.5, 0.5)])
    total = duka._export_month(
        _args(tmp_path), datetime(2026, 4, 1, 9), datetime(2026, 4, 1, 12),
        fetch=_fake_fetch({_URL: blob}), stats=_stats(),
    )
    assert total == 3
    path = Path(tmp_path) / "XAUUSD_TICK_202604_DUKASCOPY.csv"
    assert path.exists()

    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines[0] == duka.HEADER_LINE
    assert [l.split("\t")[1] for l in lines[1:]] == ["10:00:01.500", "10:00:02.500", "10:00:03.000"]

    # Parse exactly as tick_backtest.load_ticks does -> proves interchangeability.
    df = pd.read_csv(path, sep="\t")
    df.columns = [c.strip("<>").lower() for c in df.columns]
    ts = pd.to_datetime(df["date"] + " " + df["time"], format="%Y.%m.%d %H:%M:%S.%f")
    assert str(ts.dtype).startswith("datetime64")
    assert df["bid"].astype(float).iloc[0] == 4441.58
    assert df["ask"].astype(float).iloc[0] == 4441.86


def test_merge_appends_only_newer_and_drops_boundary_dup(tmp_path):
    path = Path(tmp_path) / "XAUUSD_TICK_202604_DUKASCOPY.csv"
    seed = list(duka._bi5_to_rows(duka._decompress_bi5(_bi5([(1000, 4441800, 4441500, 1.0, 1.0),
                                                             (2000, 4441820, 4441520, 1.0, 1.0)])),
                                  1000.0, _HOUR))
    duka._write_rows(path, seed, write_header=True)
    last_msc = _BASE_MS + 2000

    blob = _bi5([(2000, 4441820, 4441520, 1.0, 1.0),   # boundary dup -> dropped
                 (2500, 4441860, 4441560, 1.0, 1.0),
                 (3000, 4441900, 4441600, 1.0, 1.0)])
    appended = duka._export_month(
        _args(tmp_path, merge=True), datetime(2026, 4, 1), datetime(2026, 4, 1, 12),
        fetch=_fake_fetch({_URL: blob}), stats=_stats(),
    )
    assert appended == 2

    data = [l for l in path.read_text(encoding="utf-8").splitlines()[1:] if l.strip()]
    assert [int(l.split("\t")[2]) for l in data] == [_BASE_MS + 1000, last_msc, _BASE_MS + 2500, _BASE_MS + 3000]


def test_header_only_file_is_removed(tmp_path):
    path = Path(tmp_path) / "XAUUSD_TICK_202604_DUKASCOPY.csv"
    path.write_text(duka.HEADER_LINE + "\n", encoding="utf-8")
    assert duka._is_header_only_tick_file(path)
    appended = duka._export_month(
        _args(tmp_path, merge=True), datetime(2026, 4, 1), datetime(2026, 4, 1, 12),
        fetch=_fake_fetch({}), stats=_stats(),
    )
    assert appended == 0
    assert not path.exists()