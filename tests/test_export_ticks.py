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
