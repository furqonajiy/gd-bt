from __future__ import annotations

from xauusd_trading.cli_orig import build_parser, _config_from_args
from xauusd_trading.core.config import DEFAULT_CONFIG, StrategyConfig


def _parse(argv):
    return build_parser().parse_args(argv)


def _backtest(*extra):
    return _parse(["backtest", "--signals", "s.txt", "--charts", "c.csv", *extra])


def test_backtest_defaults_keep_research_off():
    cfg = _config_from_args(_backtest())
    assert cfg.trailing_open_distance == 0.0
    assert cfg.trailing_close_distance == 0.0
    assert cfg.trend_runner_enabled is False
    assert cfg.trend_runner_override_max_hold is True


def test_research_flags_flow_into_config():
    cfg = _config_from_args(_backtest(
        "--trailing-open-distance", "2.5",
        "--trailing-close-distance", "3.0",
        "--trend-runner",
        "--trend-runner-ema-fast", "8",
        "--trend-runner-ema-slow", "21",
        "--trend-runner-atr-period", "5",
        "--trend-runner-atr-multiplier", "2.0",
        "--trend-runner-no-override-max-hold",
    ))
    assert cfg.trailing_open_distance == 2.5
    assert cfg.trailing_close_distance == 3.0
    assert cfg.trend_runner_enabled is True
    assert cfg.trend_runner_ema_fast == 8
    assert cfg.trend_runner_ema_slow == 21
    assert cfg.trend_runner_atr_period == 5
    assert cfg.trend_runner_atr_multiplier == 2.0
    assert cfg.trend_runner_override_max_hold is False


def test_default_config_ignores_env_vars(monkeypatch):
    monkeypatch.setenv("XAUUSD_TRAILING_OPEN_DISTANCE", "9")
    monkeypatch.setenv("XAUUSD_TRAILING_CLOSE_DISTANCE", "9")
    monkeypatch.setenv("XAUUSD_TREND_RUNNER_ENABLED", "true")
    cfg = StrategyConfig()
    assert cfg.trailing_open_distance == 0.0
    assert cfg.trailing_close_distance == 0.0
    assert cfg.trend_runner_enabled is False
    assert DEFAULT_CONFIG.trailing_open_distance == 0.0