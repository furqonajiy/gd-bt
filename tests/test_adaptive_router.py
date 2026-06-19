"""`auto --adaptive`: classify the live regime each cycle and run that regime's
published champion config, falling back to the incumbent when none exists. Pure
logic (no MT5) -- exercises cli._adaptive_enabled / cli._maybe_adaptive_config.
"""
from __future__ import annotations

import json
from dataclasses import asdict
from types import SimpleNamespace

import numpy as np
import pandas as pd

from xauusd_trading import DEFAULT_CONFIG, StrategyConfig
from xauusd_trading.cli import _adaptive_enabled, _maybe_adaptive_config


def _chart(price: float, wiggle: float, drift: float = 0.0, minutes: int = 60 * 24 * 3):
    idx = pd.date_range("2026-01-01", periods=minutes, freq="1min")
    base = price + np.arange(minutes) * drift + (np.arange(minutes) % 2) * wiggle
    df = pd.DataFrame({"time": idx, "open": base, "high": base + wiggle,
                       "low": base - wiggle, "close": base})
    return SimpleNamespace(dataframe=df)


def test_adaptive_enabled_parsing():
    assert _adaptive_enabled(SimpleNamespace(adaptive=True))
    assert _adaptive_enabled(SimpleNamespace(adaptive="true"))
    assert not _adaptive_enabled(SimpleNamespace(adaptive="false"))
    assert not _adaptive_enabled(SimpleNamespace(adaptive=False))
    assert not _adaptive_enabled(SimpleNamespace())


def test_adaptive_uses_champion_when_present(tmp_path):
    # A parabolic chart classifies R4parab; its published champion (entry_count=8)
    # is loaded and replaces the incumbent for the cycle.
    champ = {"config": {**asdict(DEFAULT_CONFIG), "entry_count": 8}}
    (tmp_path / "CHAMPION_R4parab.json").write_text(json.dumps(champ))
    args = SimpleNamespace(adaptive="true", champions_dir=str(tmp_path),
                           adaptive_window_days=20)
    state: dict = {}
    cfg = _maybe_adaptive_config(args, DEFAULT_CONFIG, _chart(4500.0, 6.0), state)
    assert cfg.entry_count == 8
    assert state["__regime__"].startswith("R4parab|champion")


def test_adaptive_can_use_custom_regime_thresholds(tmp_path):
    # Raising the R3/R4 boundary routes this high-vol chart to R3strong instead
    # of the built-in R4parab, so the R3 champion is selected deliberately.
    champ = {"config": {**asdict(DEFAULT_CONFIG), "entry_count": 7}}
    (tmp_path / "CHAMPION_R3strong.json").write_text(json.dumps(champ))
    args = SimpleNamespace(
        adaptive="true",
        champions_dir=str(tmp_path),
        adaptive_window_days=20,
        regime_thresholds_json=None,
        regime_use_learned_boundaries=False,
        regime_vol_tier_low_max=6.23,
        regime_vol_tier_mid_max=20.0,
        regime_bull_trend_min=None,
    )
    state: dict = {}
    cfg = _maybe_adaptive_config(args, DEFAULT_CONFIG, _chart(4500.0, 6.0), state)
    assert cfg.entry_count == 7
    assert state["__regime__"].startswith("R3strong|champion")


def test_adaptive_falls_back_to_incumbent_when_no_champion(tmp_path):
    # A quiet chart classifies R1quiet; with no CHAMPION_R1quiet.json the incumbent
    # (base) config is returned unchanged.
    args = SimpleNamespace(adaptive="true", champions_dir=str(tmp_path),
                           adaptive_window_days=20)
    base = StrategyConfig(entry_count=6)
    cfg = _maybe_adaptive_config(args, base, _chart(1800.0, 0.3), {})
    assert cfg is base


def test_adaptive_detection_failure_keeps_incumbent():
    # A malformed chart (no dataframe columns) must not raise -- keep the incumbent.
    args = SimpleNamespace(adaptive="true", champions_dir="nope", adaptive_window_days=20)
    bad = SimpleNamespace(dataframe=pd.DataFrame({"x": [1, 2, 3]}))
    cfg = _maybe_adaptive_config(args, DEFAULT_CONFIG, bad, {})
    assert cfg is DEFAULT_CONFIG


# --- shared champion-config resolver (live + backtest) -----------------------

def test_champion_config_loads_and_falls_back(tmp_path):
    from xauusd_trading import champion_config
    champ = {"config": {**asdict(DEFAULT_CONFIG), "entry_count": 7}}
    (tmp_path / "CHAMPION_R3strong.json").write_text(json.dumps(champ))
    # present -> loaded
    cfg = champion_config("R3strong", tmp_path, DEFAULT_CONFIG)
    assert cfg.entry_count == 7
    # absent / bad dir -> fallback (same object)
    assert champion_config("R4parab", tmp_path, DEFAULT_CONFIG) is DEFAULT_CONFIG
    assert champion_config("R3strong", "no/such/dir", DEFAULT_CONFIG) is DEFAULT_CONFIG


def test_regime_config_resolver_maps_signal_to_champion(tmp_path):
    from xauusd_trading import make_regime_config_resolver, parse_one_signal
    # Parabolic chart over the signal's window -> R4parab; its champion (entries=8).
    champ = {"config": {**asdict(DEFAULT_CONFIG), "entry_count": 8}}
    (tmp_path / "CHAMPION_R4parab.json").write_text(json.dumps(champ))
    idx = pd.date_range("2026-01-01", periods=60 * 24 * 3, freq="1min")
    wig = 6.0
    base = 4500.0 + (np.arange(len(idx)) % 2) * wig
    df = pd.DataFrame({"time": idx, "open": base, "high": base + wig,
                       "low": base - wig, "close": base})
    resolver = make_regime_config_resolver(df, champions_dir=tmp_path,
                                           base_config=DEFAULT_CONFIG, window_days=20)
    sig = parse_one_signal(
        "1. BUY XAUUSD 4500 - 4498 SL 4490 TP1 4510 TP2 4515 TP3 4520 2:00 PM",
        "2026-01-03", 3)
    cfg = resolver(sig)
    assert resolver.regime_at(sig.signal_time_chart) == "R4parab"
    assert cfg.entry_count == 8       # the R4parab champion, not the base


def test_run_backtest_calls_config_resolver_per_signal(tmp_path):
    # run_backtest must invoke config_resolver once per surviving signal and use
    # the returned config (here a sentinel with entry_count=1 vs the base 3).
    from xauusd_trading import (CsvChartSource, parse_signals_file, run_backtest,
                                StrategyConfig)
    import textwrap
    chart_csv = tmp_path / "XAUUSD_M1_202601_ELEV8.csv"
    rows = ["<DATE>\t<TIME>\t<OPEN>\t<HIGH>\t<LOW>\t<CLOSE>\t<TICKVOL>\t<VOL>\t<SPREAD>"]
    import datetime as _dt
    t = _dt.datetime(2026, 1, 2, 1, 0)
    for i in range(600):
        p = 4500.0
        rows.append(f"{t:%Y.%m.%d}\t{t:%H:%M:%S}\t{p}\t{p+1}\t{p-1}\t{p}\t1\t0\t10")
        t += _dt.timedelta(minutes=1)
    chart_csv.write_text("\n".join(rows) + "\n")
    feed = tmp_path / "f.txt"
    # GMT+2 == winter chart EET, so the signal time is not shifted out of the chart.
    feed.write_text("2026-01-02 GMT+2\n"
                    "1. BUY XAUUSD 4500 - 4498 SL 4495 TP1 4505 TP2 4510 TP3 4515 3:00 AM\n")
    signals = parse_signals_file(feed)
    chart = CsvChartSource([str(chart_csv)])
    sentinel = StrategyConfig(entry_count=1)
    seen = []
    res = run_backtest(signals, chart, DEFAULT_CONFIG,
                       config_resolver=lambda s: (seen.append(s.signal_key) or sentinel))
    assert seen == [signals[0].signal_key]            # called once, per signal
    assert res["entry_filled"] + res["entry_no_fill"] == 1   # sentinel entry_count=1 used
