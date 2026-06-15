"""Trailing-distance flags on the explicit backtest/live tools feed StrategyConfig.

The explicit tools (tools/auto_explicit.py, tools/backtest_explicit.py) require
every strategy parameter, so the trailing distances must be passed through to
StrategyConfig rather than silently defaulting. trend_runner_enabled is a
separate field from runner_after_tp3 and stays at its default here.
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load(module_name: str):
    # tools/ is not a package, so load each runner module from its file path.
    spec = importlib.util.spec_from_file_location(module_name, ROOT / "tools" / f"{module_name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


backtest_explicit = _load("backtest_explicit")
auto_explicit = _load("auto_explicit")


_COMMON_STRATEGY = [
    "--initial-capital", "5000", "--sizing-mode", "risk", "--lot", "0.5",
    "--risk", "0.02607", "--minimum-lot", "0.01", "--lot-step", "0.01",
    "--bonus-per-closed-lot", "3", "--entries", "3",
    "--entry-ladder", "range_to_sl", "--entry-sl-gap", "1",
    "--activation-delay", "3", "--pending-expiry", "630", "--max-hold", "90",
    "--sl-multiplier", "1.61", "--final-target", "TP3",
    "--lock-after-tp1", "true", "--lock-after-tp2", "false",
    "--tp1-lock-delay-minutes", "0", "--tp2-lock-delay-minutes", "0",
    "--profit-lock-mode", "tp_levels", "--bep-trigger-distance", "3",
    "--tp1-lock-fraction", "0.5", "--tp2-lock-target", "TP1",
    "--runner-after-tp3", "false", "--tp3-lock-target", "TP2",
]


def _backtest_argv(*extra):
    return [
        "--signals", "s.txt", "--charts", "c.csv", "--output-dir", "out",
        "--max-drawdown-limit-pct", "40", "--progress-interval-seconds", "0",
        *_COMMON_STRATEGY, *extra,
    ]


def _auto_argv(*extra):
    return [
        "--signals", "s.txt", "--positions-json", "p.json", "--watch-interval", "5",
        "--mt5-symbol", "XAUUSD", "--mt5-server-offset", "3", "--mt5-history-bars", "5000",
        *_COMMON_STRATEGY, *extra,
    ]


def test_backtest_explicit_trailing_flows_into_config():
    args = backtest_explicit.build_parser().parse_args(
        _backtest_argv("--trailing-open-distance", "2", "--trailing-close-distance", "2")
    )
    cfg = backtest_explicit.config_from_args(args)
    assert cfg.trailing_open_distance == 2.0
    assert cfg.trailing_close_distance == 2.0
    assert cfg.trend_runner_enabled is False
    assert cfg.runner_after_tp3 is False


def test_auto_explicit_trailing_flows_into_config():
    args = auto_explicit.build_parser().parse_args(
        _auto_argv("--trailing-open-distance", "2", "--trailing-close-distance", "2")
    )
    cfg = auto_explicit.config_from_args(args)
    assert cfg.trailing_open_distance == 2.0
    assert cfg.trailing_close_distance == 2.0
    assert cfg.trend_runner_enabled is False
    assert cfg.runner_after_tp3 is False


def test_backtest_explicit_trailing_is_required():
    # argparse prints its usage block to stderr before exiting; redirect it so a
    # passing required-flag assertion does not look like a failure under `-s`.
    with contextlib.redirect_stderr(io.StringIO()), pytest.raises(SystemExit):
        backtest_explicit.build_parser().parse_args(_backtest_argv())


def test_auto_explicit_trailing_is_required():
    with contextlib.redirect_stderr(io.StringIO()), pytest.raises(SystemExit):
        auto_explicit.build_parser().parse_args(_auto_argv())