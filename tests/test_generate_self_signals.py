from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timedelta
from pathlib import Path


def _load_tool(repo_root: Path):
    path = repo_root / "tools" / "generate_self_signals.py"
    spec = importlib.util.spec_from_file_location("generate_self_signals", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _write_m15_chart(path: Path) -> None:
    rows = ["<DATE>\t<TIME>\t<OPEN>\t<HIGH>\t<LOW>\t<CLOSE>\t<TICKVOL>\t<VOL>\t<SPREAD>"]
    t = datetime(2025, 1, 1, 0, 0)
    price = 2600.0
    for i in range(90):
        open_price = price + i * 0.35
        close = open_price + 0.25
        high = close + 0.55
        low = open_price - 0.55
        rows.append(
            f"{t:%Y.%m.%d}\t{t:%H:%M:%S}\t{open_price:.2f}\t{high:.2f}\t{low:.2f}\t{close:.2f}\t1\t0\t25"
        )
        t += timedelta(minutes=15)
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")


def test_generate_self_signals_writes_gmt3_pullback_signals(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    tool = _load_tool(repo_root)

    chart_path = tmp_path / "XAUUSD_M15_202501_ELEV8.csv"
    output_path = tmp_path / "live_provider_all.txt"
    _write_m15_chart(chart_path)

    rc = tool.main([
        "--m15-charts", str(chart_path),
        "--m1-charts", str(tmp_path / "missing_m1.csv"),
        "--output", str(output_path),
        "--start-date", "2025-01-01",
        "--same-side-spacing-minutes", "30",
        "--max-signals-per-day", "40",
    ])

    assert rc == 0
    text = output_path.read_text(encoding="utf-8")
    assert "2025-01-01 GMT+3" in text
    assert "BUY XAUUSD" in text
    assert " SL " in text and " TP1 " in text and " TP2 " in text and " TP3 " in text
