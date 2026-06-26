"""Date-window tick splitting: _D<start> parts, deterministic 3-day membership."""
from __future__ import annotations

from pathlib import Path

from tools.split_ticks_by_days import (
    _window_start_day,
    day_parts_for,
    split_file_by_days,
)
from tools.split_ticks_by_size import join_parts, parts_for, split_file

_HEADER = "<DATE>\t<TIME>\t<TIME_MSC>\t<BID>\t<ASK>\t<LAST>\t<VOLUME>\t<VOLUME_REAL>\t<FLAGS>\t<SPREAD>\n"


def _row(day: int, msc: int) -> str:
    return f"2026.06.{day:02d}\t01:00:00.000\t{msc}\t4500.0\t4500.5\t\t0\t0\t4\t25\n"


def _write_month(path: Path, days: range) -> int:
    """One tick per day; returns row count."""
    n = 0
    with path.open("w", encoding="utf-8", newline="") as f:
        f.write(_HEADER)
        for d in days:
            f.write(_row(d, 1_000 + d))
            n += 1
    return n


def test_window_start_day_3day():
    # day -> start day of its 3-day window
    assert _window_start_day(1, 3) == 1
    assert _window_start_day(3, 3) == 1
    assert _window_start_day(4, 3) == 4
    assert _window_start_day(6, 3) == 4
    assert _window_start_day(7, 3) == 7
    assert _window_start_day(31, 3) == 31


def test_split_by_days_membership_and_naming(tmp_path):
    full = tmp_path / "XAUUSD_TICK_202606_ELEV8.csv"
    _write_month(full, range(1, 11))  # days 1..10

    parts = split_file_by_days(full, days=3, remove_source=True)

    names = [p.name for p in parts]
    # _D1 (1-3), _D4 (4-6), _D7 (7-9), _D10 (10): start-day windows, each _p1
    # (small windows never exceed the cap, so a single sub-part).
    assert names == [
        "XAUUSD_TICK_202606_D1_p1_ELEV8.csv",
        "XAUUSD_TICK_202606_D4_p1_ELEV8.csv",
        "XAUUSD_TICK_202606_D7_p1_ELEV8.csv",
        "XAUUSD_TICK_202606_D10_p1_ELEV8.csv",
    ]
    assert not full.exists()  # remove_source

    # Every part starts with the header and holds only its window's days.
    d1 = (tmp_path / "XAUUSD_TICK_202606_D1_p1_ELEV8.csv").read_text().splitlines()
    assert d1[0].startswith("<DATE>")
    days_in_d1 = {int(line.split("\t")[0].rsplit(".", 1)[1]) for line in d1[1:]}
    assert days_in_d1 == {1, 2, 3}

    d10 = (tmp_path / "XAUUSD_TICK_202606_D10_p1_ELEV8.csv").read_text().splitlines()
    assert {int(line.split("\t")[0].rsplit(".", 1)[1]) for line in d10[1:]} == {10}


def test_oversized_window_subsplits_into_pN(tmp_path):
    """A window bigger than the cap is size-split into _D{start}_p1, _p2, ...;
    a small window stays a single _p1. Always GitHub-pushable."""
    full = tmp_path / "XAUUSD_TICK_202606_ELEV8.csv"
    # Days 1-3 land in window _D1; pack many rows so the window exceeds a tiny cap.
    with full.open("w", encoding="utf-8", newline="") as f:
        f.write(_HEADER)
        for i in range(400):
            f.write(_row(1 + (i % 3), 1_000 + i))  # all within days 1-3 -> _D1
        f.write(_row(4, 99_999))                    # one row in _D4

    parts = split_file_by_days(full, days=3, max_mb=(len(_HEADER) + 40) / (1024 * 1024),
                               remove_source=True)
    names = [p.name for p in parts]
    # _D1 split into several _pN; _D4 a lone _p1.
    assert names[0] == "XAUUSD_TICK_202606_D1_p1_ELEV8.csv"
    assert "XAUUSD_TICK_202606_D1_p2_ELEV8.csv" in names
    assert names[-1] == "XAUUSD_TICK_202606_D4_p1_ELEV8.csv"


def test_day_parts_for_numeric_order(tmp_path):
    full = tmp_path / "XAUUSD_TICK_202606_ELEV8.csv"
    _write_month(full, range(1, 32))  # full month -> _D1_p1.._D31_p1
    split_file_by_days(full, days=3, remove_source=True)
    found = day_parts_for(full)
    starts = [int(p.name.split("_D")[1].split("_")[0]) for p in found]
    # numeric, not lexicographic (D31 last, not D10<D13<...<D7)
    assert starts == [1, 4, 7, 10, 13, 16, 19, 22, 25, 28, 31]


def test_round_trip_join_matches_original(tmp_path):
    full = tmp_path / "XAUUSD_TICK_202606_ELEV8.csv"
    _write_month(full, range(1, 13))
    original = full.read_text()

    split_file_by_days(full, days=3, remove_source=True)
    rejoined = tmp_path / "XAUUSD_TICK_202606_ELEV8.csv"
    join_parts(day_parts_for(rejoined), rejoined)
    assert rejoined.read_text() == original  # byte-identical reassembly


def test_migrates_from_legacy_pN_size_parts(tmp_path):
    """Re-splitting a committed _pN SIZE archive yields _D<start> date parts and
    removes the stale _pN parts (the real ELEV8 migration)."""
    full = tmp_path / "XAUUSD_TICK_202606_ELEV8.csv"
    _write_month(full, range(1, 10))
    original = full.read_text()
    # Make a size archive (tiny cap -> several _pN parts), drop the full file.
    split_file(full, max_bytes=len(_HEADER) + 80, remove_source=True)
    assert parts_for(full) and not full.exists()

    # Day-split tool reassembles the _pN parts, re-splits by day, removes _pN.
    from tools.split_ticks_by_days import _resolve_full
    resolved = _resolve_full(full)
    assert resolved == full
    split_file_by_days(full, days=3, remove_source=True)

    assert not parts_for(full)            # legacy size parts gone
    assert day_parts_for(full)            # date parts present
    rejoined = tmp_path / "XAUUSD_TICK_202606_ELEV8.csv"
    join_parts(day_parts_for(rejoined), rejoined)
    assert rejoined.read_text() == original
