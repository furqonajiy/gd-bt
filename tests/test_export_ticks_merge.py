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


def _seed_split(tmp_path, cap_bytes, n=2000):
    """Seed a full June file with n ticks, split it into _pN parts, drop the full
    file -- mimicking the committed (size-split) archive. Returns (parts, last_msc)."""
    import sys as _sys
    if str(ROOT / "tools") not in _sys.path:
        _sys.path.insert(0, str(ROOT / "tools"))
    from split_ticks_by_size import split_file
    base = 1780643040000
    msc = [base + i * 100 for i in range(n)]
    full = Path(tmp_path) / "XAUUSD_TICK_202606_ELEV8.csv"
    rows = export_ticks._tick_rows(_ticks([(m, 4441.0, 4441.3) for m in msc]), 3)
    export_ticks._write_rows(full, rows, write_header=True)
    parts = split_file(full, cap_bytes, remove_source=True)
    assert len(parts) >= 3 and not full.exists()
    return parts, msc[-1]


def _msc_seq(parts):
    """All ticks' <TIME_MSC>, in part+line order (header stripped from each part)."""
    seq = []
    for p in sorted(parts, key=lambda q: int(q.name.split("_p")[1].split("_")[0])):
        for line in p.read_text().splitlines()[1:]:
            if line.strip():
                seq.append(int(line.split("\t")[2]))
    return seq


def test_merge_append_to_split_archive_only_touches_tail(tmp_path):
    parts, last = _seed_split(tmp_path, cap_bytes=40 * 1024)
    p1_before, p2_before = parts[0].read_bytes(), parts[1].read_bytes()
    orig_seq = _msc_seq(parts)

    master = _ticks([
        (last, 4441.0, 4441.3),         # boundary dup -> dropped (strict >)
        (last + 100, 4441.2, 4441.5),
        (last + 200, 4441.4, 4441.7),
    ])
    conn = _FakeConn(_FakeMt5(master))
    appended = export_ticks._merge_append_split_month(
        conn, _args(tmp_path, merge=True, split_mb=40 / 1024),  # 40 KiB cap
        datetime(2026, 6, 1), datetime(2026, 6, 6))
    assert appended == 2

    new_parts = list(Path(tmp_path).glob("XAUUSD_TICK_202606_p*_ELEV8.csv"))
    # p1, p2 byte-identical (only the tail re-split); no leftover full file.
    by_n = {int(p.name.split("_p")[1].split("_")[0]): p for p in new_parts}
    assert by_n[1].read_bytes() == p1_before
    assert by_n[2].read_bytes() == p2_before
    assert not (Path(tmp_path) / "XAUUSD_TICK_202606_ELEV8.csv").exists()
    # Full sequence = original + the 2 new ticks, in order.
    assert _msc_seq(new_parts) == orig_seq + [last + 100, last + 200]


def test_merge_append_noop_leaves_all_parts_untouched(tmp_path):
    parts, last = _seed_split(tmp_path, cap_bytes=40 * 1024)
    before = [p.read_bytes() for p in parts]
    conn = _FakeConn(_FakeMt5(_ticks([(last, 4441.0, 4441.3)])))  # only the dup
    appended = export_ticks._merge_append_split_month(
        conn, _args(tmp_path, merge=True, split_mb=40 / 1024),
        datetime(2026, 6, 1), datetime(2026, 6, 6))
    assert appended == 0
    after = [p.read_bytes() for p in parts]
    assert after == before                       # every part untouched
    assert not (Path(tmp_path) / "XAUUSD_TICK_202606_ELEV8.csv").exists()


import calendar


def _msc_on(day: int, extra_ms: int = 0) -> int:
    """GMT+3-server (no shift) millisecond epoch at noon on 2026-06-<day>."""
    return calendar.timegm(datetime(2026, 6, day, 12, 0, 0).timetuple()) * 1000 + extra_ms


def _seed_day_split(tmp_path):
    """Seed a committed DAY-WINDOW June archive: ticks on Jun 2 (_D1), Jun 5 (_D4)
    and Jun 8 (_D7), split by 3-day windows, full file removed. Returns (parts,
    last_msc)."""
    import sys as _sys
    if str(ROOT / "tools") not in _sys.path:
        _sys.path.insert(0, str(ROOT / "tools"))
    from split_ticks_by_days import split_file_by_days
    msc = [_msc_on(2), _msc_on(5), _msc_on(8, 100), _msc_on(8, 200)]
    full = Path(tmp_path) / "XAUUSD_TICK_202606_ELEV8.csv"
    rows = export_ticks._tick_rows(_ticks([(m, 4441.0, 4441.3) for m in msc]), 3)
    export_ticks._write_rows(full, rows, write_header=True)
    parts = split_file_by_days(full, days=3, remove_source=True)
    assert not full.exists()
    return parts, msc[-1]


def _win_map(tmp_path):
    from split_ticks_by_days import _DAY_PART_RE
    out: dict[int, list] = {}
    for p in Path(tmp_path).glob("XAUUSD_TICK_202606_D*_p*_ELEV8.csv"):
        out.setdefault(int(_DAY_PART_RE.search(p.name).group(1)), []).append(p)
    return out


def test_merge_append_split_days_only_touches_affected_windows(tmp_path):
    parts, last = _seed_day_split(tmp_path)
    before = _win_map(tmp_path)
    d1_before = before[1][0].read_bytes()
    d4_before = before[4][0].read_bytes()

    master = _ticks([
        (last, 4441.0, 4441.3),            # boundary dup -> dropped (strict >)
        (_msc_on(9, 50), 4441.2, 4441.5),  # window _D7 (days 7-9): appended to tail
        (_msc_on(11, 50), 4441.4, 4441.7), # window _D10 (days 10-12): brand-new window
    ])
    conn = _FakeConn(_FakeMt5(master))
    appended = export_ticks._merge_append_split_days(
        conn, _args(tmp_path, merge=True, split_days=3),
        datetime(2026, 6, 1), datetime(2026, 7, 1))
    assert appended == 2

    # No stray full file, earlier windows byte-identical, new window created.
    assert not (Path(tmp_path) / "XAUUSD_TICK_202606_ELEV8.csv").exists()
    after = _win_map(tmp_path)
    assert after[1][0].read_bytes() == d1_before
    assert after[4][0].read_bytes() == d4_before
    assert 10 in after                                  # new _D10 window
    # Total data rows = 4 seeded + 2 appended.
    total = sum(sum(1 for ln in p.read_text().splitlines()[1:] if ln.strip())
                for ps in after.values() for p in ps)
    assert total == 6


def test_merge_append_split_days_noop_leaves_windows_untouched(tmp_path):
    parts, last = _seed_day_split(tmp_path)
    before = {p.name: p.read_bytes() for p in parts}
    conn = _FakeConn(_FakeMt5(_ticks([(last, 4441.0, 4441.3)])))  # only the dup
    appended = export_ticks._merge_append_split_days(
        conn, _args(tmp_path, merge=True, split_days=3),
        datetime(2026, 6, 1), datetime(2026, 7, 1))
    assert appended == 0
    after = {p.name: p.read_bytes()
             for p in Path(tmp_path).glob("XAUUSD_TICK_202606_D*_p*_ELEV8.csv")}
    assert after == before                               # every window untouched
    assert not (Path(tmp_path) / "XAUUSD_TICK_202606_ELEV8.csv").exists()


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