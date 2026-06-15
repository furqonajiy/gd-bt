#!/usr/bin/env python3
"""Run live auto-execution with the current DD40 research contract.

The project DEFAULT_CONFIG on feature/improve is set to the current best tested
candidate with max drawdown <= 40%. This wrapper passes only the strategy flags
that ``xauusd_trading.cli auto`` supports directly and relies on DEFAULT_CONFIG
for the rest of the execution contract.

Current backtest-aligned contract:

- initial capital: 1000
- risk: 0.05575
- entries: 3
- entry ladder: range_to_sl
- entry-to-SL gap: 2.0
- activation delay: 3 minutes    (from DEFAULT_CONFIG)
- pending expiry: 630 minutes    (from DEFAULT_CONFIG)
- max hold: 90 minutes           (from DEFAULT_CONFIG)
- SL multiplier: 1.61            (from DEFAULT_CONFIG)
- final target: TP3              (from DEFAULT_CONFIG)
- lock after TP1: true           (from DEFAULT_CONFIG)
- lock after TP2: false          (from DEFAULT_CONFIG)

The auto command should read the FILTERED signal file produced by
``tools/live_provider_signal_filter.py``. Do not execute the raw Telegram signal
file directly.
"""
from __future__ import annotations

import argparse
import subprocess
import sys


DD40_INITIAL_CAPITAL = 5000.0
DD40_RISK_PER_SIGNAL = 0.05575
DD40_ENTRY_COUNT = 3
DD40_ENTRY_LADDER = "range_to_sl"
DD40_ENTRY_SL_GAP = 2.0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Run DD40 high-growth live auto execution.")
    p.add_argument("--signals", default="generated/live_provider_high_growth.txt")
    p.add_argument("--positions-json", default="positions_high_growth.json")
    p.add_argument("--watch-interval", type=float, default=5.0)
    p.add_argument("--mt5-symbol", default="XAUUSD")
    p.add_argument("--mt5-server-offset", type=int, default=3)
    p.add_argument("--mt5-history-bars", type=int, default=5000)
    p.add_argument("--mt5-path", default=None)
    p.add_argument("--mt5-login", default=None)
    p.add_argument("--mt5-password", default=None)
    p.add_argument("--mt5-server", default=None)
    p.add_argument("--initial-capital", type=float, default=DD40_INITIAL_CAPITAL)
    p.add_argument("--risk", type=float, default=DD40_RISK_PER_SIGNAL)
    p.add_argument("--entries", type=int, default=DD40_ENTRY_COUNT)
    p.add_argument("--entry-ladder", default=DD40_ENTRY_LADDER, choices=["range_uniform", "range_to_sl"])
    p.add_argument("--entry-sl-gap", type=float, default=DD40_ENTRY_SL_GAP)
    p.add_argument("--no-clear", action="store_true")
    p.add_argument("--no-notifications", action="store_true")
    p.add_argument("--no-forensic", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cmd = [
        sys.executable, "-m", "xauusd_trading.cli", "auto",
        "--signals", args.signals,
        "--positions-json", args.positions_json,
        "--watch-interval", str(args.watch_interval),
        "--mt5-symbol", args.mt5_symbol,
        "--mt5-server-offset", str(args.mt5_server_offset),
        "--mt5-history-bars", str(args.mt5_history_bars),
        "--initial-capital", str(args.initial_capital),
        "--risk", str(args.risk),
        "--entries", str(args.entries),
        "--entry-ladder", args.entry_ladder,
        "--entry-sl-gap", str(args.entry_sl_gap),
    ]
    for flag, value in [
        ("--mt5-path", args.mt5_path),
        ("--mt5-login", args.mt5_login),
        ("--mt5-password", args.mt5_password),
        ("--mt5-server", args.mt5_server),
    ]:
        if value is not None:
            cmd.extend([flag, str(value)])
    if args.no_clear:
        cmd.append("--no-clear")
    if args.no_notifications:
        cmd.append("--no-notifications")
    if args.no_forensic:
        cmd.append("--no-forensic")

    return subprocess.call(cmd)


if __name__ == "__main__":
    raise SystemExit(main())
