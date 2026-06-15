"""Merge-mode tests for tools/export_ticks.py.

Drives the resume-from-last-tick append path with a mock MT5 broker (no live
terminal): the boundary-second duplicate is dropped, strictly-newer ticks are
appended in order, a no-op merge leaves the existing file byte-identical, and a
header-only file is purged rather than treated as data.
"""
from __future__ import annotations

import argparse
import importlib.util
from datetime import datetime
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("export_ticks", ROOT / "tools" / "export_ticks.py")
export_ticks = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(export_ticks)


_TICK_DTYPE = np.dtype([
    ("time", "i8"), ("bid", "f8"), ("ask", "f8"), ("last", "f8"),
    ("volume", "i8"), ("time_msc", "i8"), ("flags", "i4"), ("volume_real", "f8"),
])

# Anchored to real June-2026 GMT+3 millisecond epochs; server_offset 3 => no shift.
_LAST = 1780643048056
_PRIOR = [1780643047645, 1780643047954, _LAST]


def _ticks(rows):
    arr = np.zeros(len(rows), dtype=_TICK_DTYPE)
    for i, (msc, bid, ask) in enumerate(rows):
        arr[i] = (msc // 1000, bid, ask, 0.0, 0, msc, 6, 0.0)
    return arr


class _FakeMt5:
    COPY_TICKS_ALL = 0

    def __init__(self, master):
        self._master = master
        self.calls = []

    def copy_ticks_range(self, symbol, start_epoch, end_epoch, flags):
        self.calls.append((start_epoch, end_epoch))
        lo, hi = start_epoch * 1000, end_epoch * 1000
        m = self._master
        return m[(m["time_msc"] >= lo) & (m["time_msc"] < hi)]

    def last_error(self):
        return (0, "ok")


class _FakeConn:
    def __init__(self, mt5):
        self.mt5 = mt5


def _args(tmp_path, **over):
    base = dict(symbol="XAUUSD", output_dir=str(tmp_path), mt5_server_offset=3,
                chunk_hours=6, sleep_seconds=0.0, overwrite=False, merge=False, progress=False)
    base.update(over)
    return argparse.Namespace(**base)


def _seed_existing(tmp_path):
    path = Path(tmp_path) / "XAUUSD_TICK_202606_ELEV8.csv"
    rows = export_ticks._tick_rows(_ticks([(m, 4441.0, 4441.3) for m in _PRIOR]), 3)
    export_ticks._write_rows(path, rows, write_header=True)
    return path


def test_last_tick_msc_reads_final_row(tmp_path):
    path = _seed_existing(tmp_path)
    assert export_ticks._last_tick_msc(path) == _LAST


def test_last_tick_msc_none_for_header_only(tmp_path):
    path = Path(tmp_path) / "h.csv"
    path.write_text(export_ticks.HEADER_LINE + "\n", encoding="utf-8")
    assert export_ticks._last_tick_msc(path) is None


def test_merge_appends_only_newer_and_drops_boundary_dup(tmp_path):
    path = _seed_existing(tmp_path)
    master = _ticks([
        (_LAST, 4441.0, 4441.3),          # boundary duplicate -> dropped (strict >)
        (_LAST + 200, 4441.2, 4441.5),
        (_LAST + 1000, 4441.4, 4441.7),
        (_LAST + 2000, 4441.6, 4441.9),
    ])
    conn = _FakeConn(_FakeMt5(master))
    appended = export_ticks._export_month(
        conn, _args(tmp_path, merge=True), datetime(2026, 6, 1), datetime(2026, 6, 6)
    )
    assert appended == 3

    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines[0] == export_ticks.HEADER_LINE
    data = [l for l in lines[1:] if l.strip()]
    assert [int(l.split("\t")[2]) for l in data] == _PRIOR + [_LAST + 200, _LAST + 1000, _LAST + 2000]
    # Resume fetch must start at the whole second containing the last stored tick.
    assert conn.mt5.calls[0][0] == _LAST // 1000


def test_merge_noop_keeps_existing_file_intact(tmp_path):
    path = _seed_existing(tmp_path)
    before = path.read_text(encoding="utf-8")
    conn = _FakeConn(_FakeMt5(_ticks([(_LAST, 4441.0, 4441.3)])))  # only the dup; nothing newer
    appended = export_ticks._export_month(
        conn, _args(tmp_path, merge=True), datetime(2026, 6, 1), datetime(2026, 6, 6)
    )
    assert appended == 0
    assert path.exists()
    assert path.read_text(encoding="utf-8") == before


def test_header_only_file_is_removed(tmp_path):
    path = Path(tmp_path) / "XAUUSD_TICK_202604_ELEV8.csv"
    path.write_text(export_ticks.HEADER_LINE + "\n", encoding="utf-8")
    assert export_ticks._is_header_only_tick_file(path)
    conn = _FakeConn(_FakeMt5(_ticks([])))  # broker has no April ticks
    appended = export_ticks._export_month(
        conn, _args(tmp_path, merge=True), datetime(2026, 4, 1), datetime(2026, 5, 1)
    )
    assert appended == 0
    assert not path.exists()