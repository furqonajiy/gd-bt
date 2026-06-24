from __future__ import annotations

import importlib.util
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace


class _EmptyMt5:
    COPY_TICKS_ALL = 0

    def copy_ticks_range(self, symbol, start_epoch, end_epoch, flags):
        return []

    def last_error(self):
        return (0, "OK")


class _Conn:
    mt5 = _EmptyMt5()


def _load_tool(repo_root: Path):
    path = repo_root / "tools" / "export_ticks.py"
    spec = importlib.util.spec_from_file_location("export_ticks", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _args(tmp_path: Path, *, overwrite: bool) -> SimpleNamespace:
    return SimpleNamespace(
        symbol="XAUUSD",
        output_dir=str(tmp_path),
        overwrite=overwrite,
        chunk_hours=6,
        mt5_server_offset=3,
        progress=False,
        sleep_seconds=0.0,
    )


def test_export_month_skips_empty_tick_file(tmp_path: Path) -> None:
    tool = _load_tool(Path(__file__).resolve().parents[1])

    total = tool._export_month(
        _Conn(),
        _args(tmp_path, overwrite=True),
        datetime(2024, 1, 1),
        datetime(2024, 2, 1),
    )

    assert total == 0
    assert not (tmp_path / "XAUUSD_TICK_202401_ELEV8.csv").exists()


def test_export_month_removes_existing_header_only_tick_file(tmp_path: Path) -> None:
    tool = _load_tool(Path(__file__).resolve().parents[1])
    path = tmp_path / "XAUUSD_TICK_202604_ELEV8.csv"
    path.write_text("\t".join(tool.FIELDNAMES) + "\n", encoding="utf-8")

    total = tool._export_month(
        _Conn(),
        _args(tmp_path, overwrite=False),
        datetime(2026, 4, 1),
        datetime(2026, 5, 1),
    )

    assert total == 0
    assert not path.exists()


def test_split_exported_caps_and_removes_source(tmp_path: Path) -> None:
    """--split-mb path: a fetched month file is split into <= cap parts and the
    full file removed, so each part is GitHub-committable."""
    tool = _load_tool(Path(__file__).resolve().parents[1])
    full = tmp_path / "XAUUSD_TICK_202606_ELEV8.csv"
    header = "\t".join(tool.FIELDNAMES) + "\n"
    rows = [f"2026.06.22\t08:{i % 60:02d}:00.000\t{i}\t4212.0\t4213.0\t0\t0\t0\t0\t25\n"
            for i in range(400)]
    full.write_text(header + "".join(rows), encoding="utf-8")

    cap_mb = 4 * 1024 / (1024 * 1024)   # 4 KiB expressed in MiB -> forces several parts
    written = tool._split_exported(str(tmp_path), "XAUUSD", [(2026, 6)], cap_mb)

    parts = sorted(tmp_path.glob("XAUUSD_TICK_202606_p*_ELEV8.csv"))
    assert written == len(parts) > 1
    assert not full.exists()                       # full file removed
    for p in parts:
        assert p.stat().st_size <= int(cap_mb * 1024 * 1024)
        assert p.read_text(encoding="utf-8").startswith(header)
