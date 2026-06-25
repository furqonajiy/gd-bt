"""Daily-drawdown circuit breaker (``StrategyConfig.daily_drawdown_stop_pct``).

Mirrors the live ``auto_self._evaluate_guards`` "DAILY-LOSS HALT": once a
feed-zone day's realized equity drops the configured percent below the day's
opening equity, the rest of that day's signals are skipped (recorded as
excluded ``daily-drawdown-halt``) and the guard resets at the next day.

The contract:
  * 0.0 (default) and any threshold the day never breaches -> byte-identical to
    a plain ``run_backtest`` (parity preserved).
  * a threshold the first losing signal breaches -> the rest of that day is
    halted, and the next day trades again.
"""
from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

from trading.engine import CsvChartSource, DEFAULT_CONFIG, parse_one_signal, run_backtest


def _write_chart(tmp_path: Path) -> Path:
    """Two days of M1 bars: 2026-06-01 trends DOWN hard (BUYs hit SL -> loss),
    2026-06-02 is flat. ELEV8 tab CSV, chart tz == signal source tz (GMT+3)."""
    head = "<DATE>\t<TIME>\t<OPEN>\t<HIGH>\t<LOW>\t<CLOSE>\t<TICKVOL>\t<VOL>\t<SPREAD>"
    out = [head]

    def emit(start: datetime, bars: int, drift: float, px0: float) -> float:
        px = px0
        t = start
        for _ in range(bars):
            o = px
            c = px + drift
            h = max(o, c) + 0.3
            lo = min(o, c) - 0.3
            out.append(f"{t:%Y.%m.%d}\t{t:%H:%M:%S}\t{o:.2f}\t{h:.2f}\t{lo:.2f}\t{c:.2f}\t100.0\t0.0\t20")
            px = c
            t += timedelta(minutes=1)
        return px

    # Day 1: steep downtrend from 100 -> well below the SL (97) of the signals.
    emit(datetime(2026, 6, 1, 1, 0, 0), 400, -0.05, 100.0)
    # Day 2: flat around 100.
    emit(datetime(2026, 6, 2, 1, 0, 0), 400, 0.0, 100.0)

    p = tmp_path / "XAUUSD_M1_TEST_ELEV8.csv"
    p.write_text("\n".join(out))
    return p


def _buy(time_text: str, source_date: str):
    return parse_one_signal(
        f"1. BUY XAUUSD 100 - 99 SL 97 TP1 101 TP2 102 TP3 103 {time_text}",
        source_date=source_date, source_offset=3,
    )


def _signals():
    # Three signals on day 1 (the first loses and trips the guard), one on day 2.
    return [
        _buy("01:10 AM", "2026-06-01"),
        _buy("01:40 AM", "2026-06-01"),
        _buy("02:10 AM", "2026-06-01"),
        _buy("01:10 AM", "2026-06-02"),
    ]


def _eq(a: dict, b: dict) -> bool:
    return json.dumps(a, default=str, sort_keys=True) == json.dumps(b, default=str, sort_keys=True)


def test_disabled_is_parity(tmp_path):
    """0.0 (default) changes nothing vs a plain run_backtest."""
    chart = CsvChartSource([_write_chart(tmp_path)])
    sigs = _signals()
    base = run_backtest(sigs, chart, DEFAULT_CONFIG)
    off = run_backtest(sigs, chart, replace(DEFAULT_CONFIG, daily_drawdown_stop_pct=0.0))
    assert _eq(base, off)


def test_high_threshold_never_halts(tmp_path):
    """A threshold the day never breaches is byte-identical to disabled."""
    chart = CsvChartSource([_write_chart(tmp_path)])
    sigs = _signals()
    base = run_backtest(sigs, chart, DEFAULT_CONFIG)
    loose = run_backtest(sigs, chart, replace(DEFAULT_CONFIG, daily_drawdown_stop_pct=99.0))
    # The echoed `config` differs by the threshold value; the trading outcome
    # (the parity contract) must be byte-identical.
    assert _eq({k: v for k, v in base.items() if k != "config"},
               {k: v for k, v in loose.items() if k != "config"})
    assert base["signals_included"] == len(sigs)


def test_breach_halts_rest_of_day_then_resumes(tmp_path):
    """A losing day-1 signal trips a tiny threshold: the rest of day 1 is
    skipped, and day 2 trades again (the guard resets at the day boundary)."""
    chart = CsvChartSource([_write_chart(tmp_path)])
    sigs = _signals()
    base = run_backtest(sigs, chart, DEFAULT_CONFIG)
    # day-1 signal #1 must actually realize a loss for the guard to fire.
    assert base["rows"][0]["pnl"] < 0

    cfg = replace(DEFAULT_CONFIG, daily_drawdown_stop_pct=0.0001)
    stopped = run_backtest(sigs, chart, cfg)

    # day1 sig#1 traded (and lost), day1 sig#2 & #3 halted, day2 sig#1 traded.
    assert stopped["signals_included"] == 2
    assert stopped["signals_excluded"] == base["signals_excluded"] + 2
    traded_days = {r["signal_time_source"].strftime("%Y-%m-%d") for r in stopped["rows"]}
    assert traded_days == {"2026-06-01", "2026-06-02"}
    # exactly one day-1 row survived (the trigger), and day 2 resumed.
    day1_rows = [r for r in stopped["rows"] if r["signal_time_source"].strftime("%Y-%m-%d") == "2026-06-01"]
    assert len(day1_rows) == 1
