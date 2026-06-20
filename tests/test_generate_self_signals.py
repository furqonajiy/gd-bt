from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from trading.engine import Bar


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


def _live_args_without_dates() -> SimpleNamespace:
    return SimpleNamespace(
        ema_fast=21,
        ema_slow=55,
        atr_period=14,
        min_atr=0.30,
        max_atr=80.0,
        same_side_spacing_minutes=30,
        max_signals_per_day=40,
        entry_offset=1.0,
        range_width=2.0,
        sl_gap_from_range=3.5,
        tp1_distance=4.0,
        tp2_distance=7.0,
        tp3_distance=12.0,
    )


def _trend_bars(start: datetime, minutes: int) -> list[Bar]:
    bars: list[Bar] = []
    for i in range(minutes):
        price = 2600.0 + i * 0.02
        bars.append(
            Bar(
                time=start + timedelta(minutes=i),
                open=price,
                high=price + 0.20,
                low=price - 0.20,
                close=price + 0.05,
                spread_points=25,
                spread_price=0.25,
            )
        )
    return bars


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


def test_live_m1_generation_does_not_require_batch_date_filters() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    tool = _load_tool(repo_root)

    signals = tool.generate_signals_from_m1_bars(
        _trend_bars(datetime(2025, 1, 1, 0, 0), 90 * 15),
        _live_args_without_dates(),
    )

    assert signals
    assert signals[0].side == "BUY"
    assert hasattr(signals[0], "signal_time_chart")


def test_live_m1_generation_ignores_incomplete_current_m15_bucket() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    tool = _load_tool(repo_root)

    bars = _trend_bars(datetime(2026, 6, 5, 0, 0), 5 * 60 + 50)
    signals = tool.generate_signals_from_m1_bars(bars, _live_args_without_dates())

    assert signals
    assert max(signal.signal_time_chart for signal in signals) <= datetime(2026, 6, 5, 5, 45)
