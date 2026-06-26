"""tools/split_ticks_by_size.py: size-capped, line-aligned tick-file splitting."""
from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
for p in (str(_REPO_ROOT), str(_REPO_ROOT / "tools")):
    if p not in sys.path:
        sys.path.insert(0, p)

from split_ticks_by_size import _part_path, join_parts, parts_for, split_file  # noqa: E402

_HEADER = "<DATE>\t<TIME>\t<TIME_MSC>\t<BID>\t<ASK>\t<LAST>\t<VOLUME>\t<VOLUME_REAL>\t<FLAGS>\t<SPREAD>\n"


def _write_ticks(path: Path, n: int) -> list[str]:
    rows = [f"2026.06.22\t08:{i % 60:02d}:00.000\t{i}\t4212.{i % 100:02d}\t4213.0\t0\t0\t0\t0\t25\n"
            for i in range(n)]
    path.write_text(_HEADER + "".join(rows), encoding="utf-8")
    return rows


def test_part_path_inserts_pN_before_elev8_tag():
    src = Path("data/ticks/XAUUSD_TICK_202606_ELEV8.csv")
    assert _part_path(src, 1).name == "XAUUSD_TICK_202606_p1_ELEV8.csv"
    assert _part_path(src, 12).name == "XAUUSD_TICK_202606_p12_ELEV8.csv"
    assert _part_path(Path("ticks.csv"), 2).name == "ticks_p2.csv"


def test_splits_into_multiple_capped_parts_preserving_rows(tmp_path):
    src = tmp_path / "XAUUSD_TICK_202606_ELEV8.csv"
    rows = _write_ticks(src, 300)
    # ~60 bytes/row; cap at 4 KiB forces several parts.
    cap = 4 * 1024
    parts = split_file(src, cap, remove_source=True)

    assert len(parts) > 1
    for p in parts:
        assert p.stat().st_size <= cap                 # every part under the cap
        assert p.read_text(encoding="utf-8").startswith(_HEADER)  # header in each
    assert not src.exists()                            # --remove-source honored

    # Concatenated data rows (header stripped from each part) == the original.
    recovered = []
    for p in parts:
        recovered.extend(p.read_text(encoding="utf-8").splitlines(keepends=True)[1:])
    assert recovered == rows


def test_file_already_under_cap_is_left_untouched(tmp_path):
    src = tmp_path / "XAUUSD_TICK_202606_ELEV8.csv"
    _write_ticks(src, 5)
    parts = split_file(src, 10 * 1024 * 1024, remove_source=True)
    assert parts == []          # nothing to split
    assert src.exists()         # not removed when it wasn't split


def test_parts_for_returns_numeric_order(tmp_path):
    # 12 parts so a string sort (p1, p10, p11, p12, p2, ...) would be wrong.
    for n in range(1, 13):
        (tmp_path / f"XAUUSD_TICK_202606_p{n}_ELEV8.csv").write_text(_HEADER, encoding="utf-8")
    full = tmp_path / "XAUUSD_TICK_202606_ELEV8.csv"
    got = [int(p.name.split("_p")[1].split("_")[0]) for p in parts_for(full)]
    assert got == list(range(1, 13))


def test_part_path_generalizes_to_any_source_tag():
    # A separate broker (DEMO) splits/joins with the same machinery as ELEV8.
    src = Path("data/demo/ticks/XAUUSD_TICK_202606_DEMO.csv")
    assert _part_path(src, 1).name == "XAUUSD_TICK_202606_p1_DEMO.csv"
    assert _part_path(src, 12).name == "XAUUSD_TICK_202606_p12_DEMO.csv"
    # A purely-numeric suffix is NOT a tag -> legacy "_pN before .csv" fallback.
    assert _part_path(Path("XAUUSD_TICK_202606.csv"), 2).name == "XAUUSD_TICK_202606_p2.csv"


def test_demo_tag_split_join_roundtrip_and_parts_discovery(tmp_path):
    src = tmp_path / "XAUUSD_TICK_202606_DEMO.csv"
    original = _write_ticks(src, 300) and src.read_bytes()
    parts = split_file(src, 4 * 1024, remove_source=True)
    assert len(parts) > 1 and not src.exists()
    assert all(p.name.endswith("_DEMO.csv") and "_p" in p.name for p in parts)
    # parts_for derives the DEMO tag from the full path and finds them in order.
    found = parts_for(src)
    assert found == parts
    join_parts(found, src, remove_parts=True)
    assert src.read_bytes() == original


def test_split_then_join_is_byte_identical(tmp_path):
    src = tmp_path / "XAUUSD_TICK_202606_ELEV8.csv"
    _write_ticks(src, 300)
    original = src.read_bytes()
    parts = split_file(src, 4 * 1024, remove_source=True)
    assert len(parts) > 1 and not src.exists()

    # parts_for must rediscover them (numeric order) and join back byte-identically.
    found = parts_for(src)
    assert found == parts
    join_parts(found, src, remove_parts=True)
    assert src.read_bytes() == original          # round-trip is lossless
    assert all(not p.exists() for p in parts)    # parts consumed
