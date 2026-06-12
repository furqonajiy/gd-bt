#!/usr/bin/env python3
"""Single-process live feed loop: regenerate a self-signal feed only when a
new CLOSED M1 bar exists.

Why this exists: the naive live loop shells out ``cli fetch`` plus a generator
script every cycle, paying ~2 s of Python/pandas startup each pass and
re-pulling two months of history for at most one new bar. This loop keeps ONE
process alive and, each tick:

  1. asks the terminal for the last CLOSED M1 bar (one tiny call,
     ``recent_closed_bars(1)`` -- the forming bar is excluded, same rule that
     keeps live generation in parity with the M1 backtest);
  2. if that bar has not advanced since the previous pass (market closed,
     weekend, daily break) it does NOTHING -- zero fetch, zero generation;
  3. otherwise it refreshes only the current month's CSV
     (``archive_m1_by_month(months_back=1)``; prior months are immutable once
     rolled over) and re-runs the UNMODIFIED CSV-based generator in-process.

Signal logic and data format are byte-identical to the backtest path -- the
generator module is imported and invoked with the same argv it takes on the
command line. Parity is the contract; only the scheduling is new.

Usage (champion no-trailing config):

    python tools/live_feed_loop.py \\
      --family scalper \\
      --interval 60 \\
      --mt5-symbol XAUUSD --mt5-server-offset 3 \\
      -- \\
      --charts "data/XAUUSD_M1_*_ELEV8.csv" \\
      --output generated/self_scalper24_live.txt \\
      --start 2026-06-10 --session-start 0 --session-end 0

Everything after ``--`` is passed verbatim to the generator's own argparse.
"""
from __future__ import annotations

import argparse
import importlib
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "tools") not in sys.path:
    sys.path.insert(0, str(ROOT / "tools"))

# Family -> generator module under tools/. All are CSV-based and expose
# main(argv) -> int; importing keeps the signal logic identical to the
# backtest archives (no fork of generation code).
GENERATOR_MODULES = {
    "scalper": "generate_scalper_signals",
    "risk02": "generate_aggressive_limit_risk02",
    "canonical": "generate_self_signals",
    "better": "generate_better_self_signals",
    "zones": "gen_zone_signals",
}


def _should_regenerate(last_closed, last_seen) -> bool:
    """True when a new closed bar exists (or we have never generated yet).

    ``last_closed`` is None when the terminal has no data (disconnected) --
    never regenerate on that, the previous feed stays in place.
    """
    if last_closed is None:
        return False
    return last_seen is None or last_closed != last_seen


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Efficient live self-signal feed loop (fetch+generate only "
                    "on a new closed M1 bar).",
    )
    p.add_argument("--family", required=True, choices=sorted(GENERATOR_MODULES),
                   help="Which tools/ generator to run (imported in-process).")
    p.add_argument("--interval", type=float, default=60.0,
                   help="Seconds between checks (default 60; new scalper signals "
                        "can only form once per closed M1 bar, so faster than "
                        "~60s buys nothing).")
    p.add_argument("--fetch-months", type=int, default=1,
                   help="months_back for the CSV refresh (default 1: only the "
                        "current month changes while live).")
    p.add_argument("--data-dir", default=str(ROOT / "data"))
    p.add_argument("--mt5-symbol", default="XAUUSD")
    p.add_argument("--mt5-server-offset", type=int, default=3)
    p.add_argument("--mt5-path", default=None)
    p.add_argument("--mt5-login", type=int, default=None)
    p.add_argument("--mt5-password", default=None)
    p.add_argument("--mt5-server", default=None)
    p.add_argument("gen_args", nargs=argparse.REMAINDER,
                   help="Arguments after `--` are forwarded to the generator.")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.interval < 5.0:
        raise SystemExit("--interval must be >= 5 seconds")
    gen_argv = [a for a in args.gen_args if a != "--"]
    if not gen_argv:
        raise SystemExit("no generator arguments given (pass them after `--`)")

    gen_module = importlib.import_module(GENERATOR_MODULES[args.family])

    from xauusd_trading import Mt5ChartSource, Mt5Connection, archive_m1_by_month

    conn = Mt5Connection(path=args.mt5_path, login=args.mt5_login,
                         password=args.mt5_password, server=args.mt5_server)
    conn.initialize()
    chart = Mt5ChartSource(conn, symbol=args.mt5_symbol,
                           server_offset_hours=args.mt5_server_offset,
                           history_bars=10)
    last_seen = None
    passes = regens = 0
    print(f"[feed-loop] family={args.family} interval={args.interval:g}s "
          f"fetch_months={args.fetch_months} gen_args={' '.join(gen_argv)}",
          flush=True)
    try:
        while True:
            passes += 1
            try:
                closed = chart.recent_closed_bars(1)
                last_closed = closed[-1].time if closed else None
                if _should_regenerate(last_closed, last_seen):
                    archive_m1_by_month(
                        conn, args.mt5_symbol, Path(args.data_dir),
                        months_back=args.fetch_months,
                        server_offset_hours=args.mt5_server_offset,
                        overwrite=False,
                    )
                    rc = gen_module.main(list(gen_argv))
                    if rc == 0:
                        last_seen = last_closed
                        regens += 1
                        print(f"[feed-loop] regenerated (bar {last_closed}, "
                              f"{regens}/{passes} passes did work)", flush=True)
                    else:
                        print(f"[feed-loop] generator rc={rc}; will retry next "
                              f"pass", flush=True)
                # else: no new closed bar -> nothing to do this pass.
            except Exception as e:  # noqa: BLE001 - loop must survive blips
                print(f"[feed-loop] pass failed (will retry): {e}", flush=True)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print(f"\n[feed-loop] interrupted; {regens}/{passes} passes regenerated.")
        return 0
    finally:
        conn.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
