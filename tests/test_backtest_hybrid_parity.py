"""tools/backtest_hybrid.py parity: with no covering ticks it must reproduce
run_backtest EXACTLY.

The hybrid backtest routes each signal to the TICK executor where the tick
archive covers it and to the M1 engine otherwise, then aggregates with the SAME
aggregate_backtest_result run_backtest uses. Its M1-fallback loop mirrors
run_backtest's loop, so this test pins that mirror: a hybrid run with ticks=None
(or ticks that do not cover the signals) is byte-identical to run_backtest apart
from the additive ``data_source`` / ``data_sources`` tags. That makes the M1 CLI
behaviour provably unchanged and catches any future drift between the two loops.

Deterministic (a tiny synthetic M1 chart written to tmp), so it runs everywhere.
"""
from __future__ import annotations

import importlib.util
import json
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

from trading.engine import CsvChartSource, DEFAULT_CONFIG, parse_one_signal, run_backtest

ROOT = Path(__file__).resolve().parents[1]


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "tools" / f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


bh = _load("backtest_hybrid")


def _write_chart(tmp_path: Path) -> Path:
    """600 one-minute bars on 2026-06-01 from 01:00 (mild drift), ELEV8 tab CSV."""
    head = "<DATE>\t<TIME>\t<OPEN>\t<HIGH>\t<LOW>\t<CLOSE>\t<TICKVOL>\t<VOL>\t<SPREAD>"
    out = [head]
    t = datetime(2026, 6, 1, 1, 0, 0)
    px = 100.0
    for _ in range(600):
        o = px
        h = px + 0.6
        lo = px - 0.6
        c = px + 0.08
        out.append(f"{t:%Y.%m.%d}\t{t:%H:%M:%S}\t{o:.2f}\t{h:.2f}\t{lo:.2f}\t{c:.2f}\t100.0\t0.0\t20")
        px = c
        t += timedelta(minutes=1)
    p = tmp_path / "XAUUSD_M1_TEST_ELEV8.csv"
    p.write_text("\n".join(out))
    return p


def _sig(time_text: str):
    # GMT+3 source vs GMT+3 chart -> signal_time_chart matches the source clock.
    return parse_one_signal(
        f"1. BUY XAUUSD 100 - 99 SL 97 TP1 101 TP2 102 TP3 103 {time_text}",
        source_date="2026-06-01", source_offset=3,
    )


def _strip_hybrid_tags(result: dict) -> dict:
    r = dict(result)
    r.pop("data_sources", None)
    r["rows"] = [{k: v for k, v in row.items() if k != "data_source"} for row in r["rows"]]
    r["entry_rows"] = [{k: v for k, v in er.items() if k != "data_source"} for er in r["entry_rows"]]
    return r


def _eq(a: dict, b: dict) -> bool:
    return json.dumps(a, default=str, sort_keys=True) == json.dumps(b, default=str, sort_keys=True)


def test_hybrid_no_ticks_equals_run_backtest(tmp_path):
    chart = CsvChartSource([_write_chart(tmp_path)])
    sigs = [_sig("01:30 AM"), _sig("02:15 AM"), _sig("03:00 AM")]
    base = run_backtest(sigs, chart, DEFAULT_CONFIG)
    hyb = bh.run_hybrid_backtest(sigs, chart, None, DEFAULT_CONFIG)
    assert hyb["data_sources"] == {"tick_signals": 0, "m1_signals": len(base["rows"])}
    assert all(r["data_source"] == "M1" for r in hyb["rows"])
    assert _eq(base, _strip_hybrid_tags(hyb))


def test_m1_only_flag_parses():
    """--m1-only is the clean 'no ticks' switch for the M1 side of an M1-vs-TICK
    comparison (a non-matching --ticks glob is a hard error). It is a store_true
    defaulting off, so the normal hybrid path is unchanged. Its behaviour (ticks
    ignored -> pure M1) is the same path pinned by the ticks=None parity test."""
    act = next((a for a in bh.build_parser()._actions if a.dest == "m1_only"), None)
    assert act is not None, "--m1-only flag is missing from backtest_hybrid"
    assert act.default is False and act.const is True  # store_true, default off


def test_hybrid_uncovered_ticks_equals_run_backtest(tmp_path):
    """Ticks present but on a different day -> every signal falls back to M1."""
    chart = CsvChartSource([_write_chart(tmp_path)])
    sigs = [_sig("01:30 AM"), _sig("02:15 AM")]
    ticks = pd.DataFrame({
        "time": pd.to_datetime(["2026-05-01 01:00:00.000", "2026-05-01 01:00:01.000"]),
        "bid": [100.0, 100.0], "ask": [100.1, 100.1],
    })
    base = run_backtest(sigs, chart, DEFAULT_CONFIG)
    hyb = bh.run_hybrid_backtest(sigs, chart, ticks, DEFAULT_CONFIG)
    assert hyb["data_sources"]["tick_signals"] == 0
    assert _eq(base, _strip_hybrid_tags(hyb))
