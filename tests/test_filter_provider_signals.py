from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timedelta
from pathlib import Path


def _load_tool(repo_root: Path):
    path = repo_root / "tools" / "filter_provider_signals_by_indicator.py"
    spec = importlib.util.spec_from_file_location("filter_provider_signals_by_indicator", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _write_m1_day(path: Path, date: datetime) -> None:
    """Full-day M1 chart (gentle uptrend) — enough bars for all indicators."""
    rows = ["<DATE>\t<TIME>\t<OPEN>\t<HIGH>\t<LOW>\t<CLOSE>\t<TICKVOL>\t<VOL>\t<SPREAD>"]
    t = datetime(date.year, date.month, date.day, 0, 0)
    for i in range(1440):
        op = 2600.0 + i * 0.01
        cl = op + 0.02
        rows.append(f"{t:%Y.%m.%d}\t{t:%H:%M:%S}\t{op:.2f}\t{cl + 0.05:.2f}\t{op - 0.05:.2f}\t{cl:.2f}\t1\t0\t20")
        t += timedelta(minutes=1)
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")


def _write_feed(path: Path) -> None:
    path.write_text("\n".join([
        "2025-01-02 GMT+7",
        "1. BUY XAUUSD 2606 - 2604 SL 2599 TP1 2614 TP2 2624 TP3 2644 10:00 AM",
        "2. SELL XAUUSD 2610 - 2612 SL 2617 TP1 2602 TP2 2592 TP3 2572 11:00 AM",
        "",
        "2025-01-09 GMT+7",   # outside the charted day -> must be dropped (no bar)
        "3. BUY XAUUSD 2606 - 2604 SL 2599 TP1 2614 TP2 2624 TP3 2644 10:00 AM",
    ]) + "\n", encoding="utf-8")


def test_base_keeps_in_range_drops_out_of_range(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    tool = _load_tool(repo_root)
    chart = tmp_path / "XAUUSD_M1_202501_ELEV8.csv"
    feed = tmp_path / "victor.txt"
    out = tmp_path / "out.txt"
    _write_m1_day(chart, datetime(2025, 1, 2))
    _write_feed(feed)

    rc = tool.main(["--input", str(feed), "--output", str(out), "--charts", str(chart)])
    assert rc == 0
    text = out.read_text(encoding="utf-8")
    # Both 01-02 signals are in range; the 01-09 signal has no chart bar -> dropped.
    assert "BUY XAUUSD" in text and "SELL XAUUSD" in text
    assert "2025-01-09" not in text


def test_rsi_filter_drops_buys_keeps_sells(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    tool = _load_tool(repo_root)
    chart = tmp_path / "XAUUSD_M1_202501_ELEV8.csv"
    feed = tmp_path / "victor.txt"
    out = tmp_path / "out.txt"
    _write_m1_day(chart, datetime(2025, 1, 2))
    _write_feed(feed)

    # --rsi-buy-max 0 keeps a BUY only if RSI<=0 (never) -> every BUY dropped;
    # --rsi-sell-min 0 is OFF, so SELLs are untouched. Deterministic.
    rc = tool.main([
        "--input", str(feed), "--output", str(out), "--charts", str(chart),
        "--rsi-buy-max", "0",
    ])
    assert rc == 0
    text = out.read_text(encoding="utf-8")
    assert "BUY XAUUSD" not in text   # all BUYs filtered out
    assert "SELL XAUUSD" in text      # SELL survives
