"""backtest_explicit syncs the M1 chart archive from MT5 before running.

Default is on (the user wants fresh bars every backtest); it soft-fails to the
existing CSVs when MetaTrader5 is unavailable (e.g. this Linux/CI env), so the
backtest never breaks just because the terminal can't be reached.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load():
    spec = importlib.util.spec_from_file_location("backtest_explicit", ROOT / "tools" / "backtest_explicit.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


be = _load()

_REQUIRED = [
    "--signals", "s.txt", "--charts", "data/XAUUSD_M1_*.csv", "--output-dir", "out",
    "--max-drawdown-limit-pct", "50", "--progress-interval-seconds", "0",
    "--initial-capital", "5000", "--sizing-mode", "risk", "--lot", "0.01",
    "--risk", "0.05", "--minimum-lot", "0.01", "--lot-step", "0.01",
    "--bonus-per-closed-lot", "3", "--entries", "8",
    "--entry-ladder", "range_to_sl", "--entry-sl-gap", "0.5",
    "--activation-delay", "0", "--pending-expiry", "180", "--max-hold", "240",
    "--sl-multiplier", "2.1", "--final-target", "TP3",
    "--lock-after-tp1", "true", "--lock-after-tp2", "true",
    "--tp1-lock-delay-minutes", "10", "--tp2-lock-delay-minutes", "0",
    "--profit-lock-mode", "tp_levels", "--bep-trigger-distance", "3",
    "--tp1-lock-fraction", "0.5", "--tp2-lock-target", "TP1",
    "--runner-after-tp3", "false", "--tp3-lock-target", "TP2",
    "--trailing-open-distance", "0", "--trailing-close-distance", "0",
]


def _argv(*extra):
    return _REQUIRED + list(extra)


def test_sync_charts_defaults_on():
    args = be.build_parser().parse_args(_argv())
    assert args.sync_charts is True
    assert args.mt5_symbol == "XAUUSD"
    assert args.mt5_server_offset == 3
    assert args.sync_months == be.ARCHIVE_MONTHS == 2


def test_sync_charts_can_be_disabled_and_overridden():
    args = be.build_parser().parse_args(
        _argv("--sync-charts", "false", "--mt5-symbol", "XAUUSDm",
              "--mt5-server-offset", "2", "--sync-months", "1")
    )
    assert args.sync_charts is False
    assert args.mt5_symbol == "XAUUSDm"
    assert args.mt5_server_offset == 2
    assert args.sync_months == 1


def test_sync_soft_fails_without_mt5(capsys):
    # No MetaTrader5 here: must not raise; it warns and returns so the run
    # continues on existing CSVs.
    be._sync_charts_from_mt5("XAUUSD", 3, 2)
    assert "skipped chart sync" in capsys.readouterr().err
