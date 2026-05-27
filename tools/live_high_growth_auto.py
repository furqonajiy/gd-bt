#!/usr/bin/env python3
"""Run live auto-execution with the best bonus-aware backtest contract.

This wrapper avoids live/backtest config drift. The project DEFAULT_CONFIG on
feature/improve is already set to the current best candidate, and this wrapper
also passes the key supported CLI flags explicitly:

- risk: 0.10
- entries: 4
- entry ladder: range_uniform

The auto command should read the FILTERED signal file produced by
``tools/live_provider_signal_filter.py``. Do not execute the raw Telegram signal
file directly.
"""
from __future__ import annotations

import argparse
import subprocess
import sys


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Run high-growth live auto execution.")
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
    p.add_argument("--risk", type=float, default=0.10)
    p.add_argument("--entries", type=int, default=4)
    p.add_argument("--entry-ladder", default="range_uniform", choices=["range_uniform", "range_to_sl"])
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
        "--initial-capital", "10000",
        "--risk", str(args.risk),
        "--entries", str(args.entries),
        "--entry-ladder", args.entry_ladder,
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

    print("Running:")
    print(" ".join(cmd))
    return subprocess.call(cmd)


if __name__ == "__main__":
    raise SystemExit(main())
