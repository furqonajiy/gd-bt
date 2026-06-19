from __future__ import annotations

import contextlib
import io
import json
from types import SimpleNamespace

import pandas as pd

import tools.regime_auto as ra


def _chart():
    idx = pd.date_range("2026-01-01", periods=2, freq="1min")
    return pd.DataFrame(
        {"open": [1.0, 1.0], "high": [2.0, 2.0],
         "low": [0.0, 0.0], "close": [1.5, 1.5]},
        index=idx,
    )


def test_regime_auto_emits_full_live_deployment(tmp_path, monkeypatch):
    champ = {
        "feed": "rsi70_sqz6_rr08",
        "edge": 200.0,
        "oos": 100.0,
        "dd": 19.0,
        "config": {"entry_count": 5, "risk_per_signal": 0.01},
    }
    (tmp_path / "CHAMPION_R3strong.json").write_text(json.dumps(champ))
    monkeypatch.setattr(ra, "_load_recent_m1", lambda _charts, _days: _chart())
    monkeypatch.setattr(
        ra, "read_current_regime",
        lambda _m1: SimpleNamespace(regime="R3strong", m15_atr=2.5, trend=0.25),
    )

    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        assert ra.main(["--champions-dir", str(tmp_path)]) == 0
    text = out.getvalue()
    assert "# detected regime: R3strong" in text
    assert "# champion: feed=rsi70_sqz6_rr08" in text
    assert "1. GENERATE" in text
    assert "tools/generate_scalper_signals.py" in text
    assert "3. LIVE FEED LOOP" in text
    assert "tools/live_feed_loop.py" in text
    assert "4. LIVE AUTO EXECUTOR" in text
    assert "--signals generated/self_scalper24_rsi70_sqz6_rr08_live.txt" in text
    assert "--rsi-buy-max 70" in text


def test_regime_auto_backtest_only_keeps_short_command(tmp_path, monkeypatch):
    champ = {
        "feed": "breakout",
        "edge": 200.0,
        "oos": 100.0,
        "dd": 19.0,
        "config": {"entry_count": 5},
    }
    (tmp_path / "CHAMPION_R4parab.json").write_text(json.dumps(champ))
    monkeypatch.setattr(ra, "_load_recent_m1", lambda _charts, _days: _chart())
    monkeypatch.setattr(
        ra, "read_current_regime",
        lambda _m1: SimpleNamespace(regime="R4parab", m15_atr=5.0, trend=0.5),
    )

    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        assert ra.main(["--champions-dir", str(tmp_path), "--backtest-only"]) == 0
    text = out.getvalue()
    assert "tools/backtest_explicit.py" in text or "xauusd_trading.cli backtest" in text
    assert "LIVE FEED LOOP" not in text


def test_regime_auto_no_champion_holds(tmp_path, monkeypatch):
    monkeypatch.setattr(ra, "_load_recent_m1", lambda _charts, _days: _chart())
    monkeypatch.setattr(
        ra, "read_current_regime",
        lambda _m1: SimpleNamespace(regime="R1quiet", m15_atr=0.5, trend=0.0),
    )

    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        assert ra.main(["--champions-dir", str(tmp_path)]) == 0
    assert "HOLD: keep running your incumbent config" in out.getvalue()
