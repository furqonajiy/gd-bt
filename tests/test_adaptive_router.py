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
