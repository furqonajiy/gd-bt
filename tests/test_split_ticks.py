"""Tests for tools/split_ticks.py -- the pure date-split + grow-only merge path (no MT5).

Seeds full monthly tick files in the exact on-disk schema via export_ticks'
_tick_rows/_write_rows, then exercises split_month_file: ticks route to H1/H2 by
calendar day, a re-split appends nothing (idempotent, byte-identical), a grown full
file appends only the new ticks to the right part, and --overwrite rebuilds parts
from scratch.
"""
from __future__ import annotations

import calendar
import importlib.util
from datetime import datetime
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]


def _load(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


export_ticks = _load("export_ticks", "tools/export_ticks.py")
split_ticks = _load("split_ticks", "tools/split_ticks.py")


_TICK_DTYPE = np.dtype([
    ("time", "i8"), ("bid", "f8"), ("ask", "f8"), ("last", "f8"),
    ("volume", "i8"), ("time_msc", "i8"), ("flags", "i4"), ("volume_real", "f8"),
])


def _msc(year, month, day, hh=12, mm=0, ss=0) -> int:
    # server_offset 3 => chart time == broker time == UTC epoch via timegm (see export_ticks).
    return calendar.timegm(datetime(year, month, day, hh, mm, ss).timetuple()) * 1000


def _seed_full(path: Path, mscs) -> None:
    arr = np.zeros(len(mscs), dtype=_TICK_DTYPE)
    for i, m in enumerate(mscs):
        arr[i] = (m // 1000, 100.0 + i, 100.5 + i, 0.0, 0, m, 6, 0.0)
    rows = export_ticks._tick_rows(arr, 3)
    export_ticks._write_rows(path, rows, write_header=True)


def _data_mscs(path: Path) -> list[int]:
    lines = path.read_text(encoding="utf-8").splitlines()
    return [int(l.split("\t")[2]) for l in lines[1:] if l.strip()]


def _header(path: Path) -> str:
    return path.read_text(encoding="utf-8").splitlines()[0]


def test_routes_by_calendar_date(tmp_path):
    full = tmp_path / "BTCUSD_TICK_202601_ELEV8.csv"
    _seed_full(full, [
        _msc(2026, 1, 1), _msc(2026, 1, 15, 23, 59, 59),  # -> H1
        _msc(2026, 1, 16), _msc(2026, 1, 31),             # -> H2
    ])
    h1, h2 = tmp_path / "h1.csv", tmp_path / "h2.csv"

    w1, w2 = split_ticks.split_month_file(full, h1, h2)

    assert (w1, w2) == (2, 2)
    assert _data_mscs(h1) == [_msc(2026, 1, 1), _msc(2026, 1, 15, 23, 59, 59)]
    assert _data_mscs(h2) == [_msc(2026, 1, 16), _msc(2026, 1, 31)]
    assert _header(h1) == export_ticks.HEADER_LINE
    assert _header(h2) == export_ticks.HEADER_LINE


def test_resplit_is_idempotent_and_byte_identical(tmp_path):
    full = tmp_path / "BTCUSD_TICK_202601_ELEV8.csv"
    _seed_full(full, [_msc(2026, 1, 5), _msc(2026, 1, 20)])
    h1, h2 = tmp_path / "h1.csv", tmp_path / "h2.csv"
    split_ticks.split_month_file(full, h1, h2)
    before1, before2 = h1.read_bytes(), h2.read_bytes()

    w1, w2 = split_ticks.split_month_file(full, h1, h2)

    assert (w1, w2) == (0, 0)
    assert h1.read_bytes() == before1
    assert h2.read_bytes() == before2


def test_split_extends_when_full_grows(tmp_path):
    full = tmp_path / "BTCUSD_TICK_202601_ELEV8.csv"
    _seed_full(full, [_msc(2026, 1, 5), _msc(2026, 1, 20)])
    h1, h2 = tmp_path / "h1.csv", tmp_path / "h2.csv"
    split_ticks.split_month_file(full, h1, h2)

    # Full grows: a later second-half tick arrives.
    _seed_full(full, [_msc(2026, 1, 5), _msc(2026, 1, 20), _msc(2026, 1, 25)])
    w1, w2 = split_ticks.split_month_file(full, h1, h2)

    assert w1 == 0  # first half untouched
    assert w2 == 1  # only the new H2 tick appended
    assert _data_mscs(h2) == [_msc(2026, 1, 20), _msc(2026, 1, 25)]


def test_overwrite_rebuilds_parts(tmp_path):
    full = tmp_path / "BTCUSD_TICK_202601_ELEV8.csv"
    _seed_full(full, [_msc(2026, 1, 5), _msc(2026, 1, 20)])
    h1, h2 = tmp_path / "h1.csv", tmp_path / "h2.csv"
    _seed_full(h1, [_msc(2026, 1, 9)])   # stale parts that must be discarded
    _seed_full(h2, [_msc(2026, 1, 28)])

    split_ticks.split_month_file(full, h1, h2, overwrite=True)

    assert _data_mscs(h1) == [_msc(2026, 1, 5)]
    assert _data_mscs(h2) == [_msc(2026, 1, 20)]


def test_empty_or_missing_full_is_noop(tmp_path):
    h1, h2 = tmp_path / "h1.csv", tmp_path / "h2.csv"
    assert split_ticks.split_month_file(tmp_path / "nope.csv", h1, h2) == (0, 0)
    assert not h1.exists() and not h2.exists()


def test_path_naming(tmp_path):
    full = split_ticks._full_path(str(tmp_path), "BTCUSD", 2026, 1)
    h1, h2 = split_ticks._half_paths(str(tmp_path), "BTCUSD", 2026, 1)
    assert full.name == "BTCUSD_TICK_202601_ELEV8.csv"
    assert h1.name == "BTCUSD_TICK_202601_1_ELEV8.csv"
    assert h2.name == "BTCUSD_TICK_202601_2_ELEV8.csv"


def test_iter_months_crosses_year_boundary():
    assert list(split_ticks._iter_months((2025, 11), (2026, 2))) == [
        (2025, 11), (2025, 12), (2026, 1), (2026, 2),
    ]