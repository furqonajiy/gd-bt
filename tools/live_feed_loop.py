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

Logging mirrors `auto`: a one-line header at start, then output ONLY when the
feed gains a new signal -- ``[ts] Add Signal 75. BUY XAUUSD ...`` per new line.
The generator's own stdout is suppressed and idle passes are silent, so a feed
that fires dozens of signals/day produces one tidy line per signal, nothing
else.

Usage (champion no-trailing config):

    python tools/live_feed_loop.py \\
      --family scalper \\
      --interval 60 \\
      --mt5-symbol XAUUSD --mt5-server-offset 3 \\
      -- \\
      --charts "data/XAUUSD_M1_*_ELEV8.csv" \\
      --output signals/self_scalper24_live.txt \\
      --start 2026-06-10 --session-start 0 --session-end 0

Everything after ``--`` is passed verbatim to the generator's own argparse.
"""
from __future__ import annotations

import argparse
import contextlib
import importlib
import io
import os
import re
import sys
import time
from datetime import datetime, timedelta
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

# The CLI flag each generator family uses for its rolling start. `--gen-start-days`
# rewrites it when present and INJECTS it when absent (see _effective_gen_argv), so
# the live loop's rolling window is honored even if the pass-through omitted the
# flag -- otherwise --gen-start-days is a silent no-op and the feed emits the whole
# loaded chart window (the early, cold-start bars included), diverging from a
# full-archive backtest. None = family has no start flag (nothing to inject).
GENERATOR_START_FLAG = {
    "scalper": "--start",
    "risk02": "--start-date",
    "canonical": "--start-date",
    "better": "--start-date",
    "zones": None,
}


_SIGNAL_LINE = re.compile(r"^\s*\d+\.\s")


def _output_path(gen_argv: list[str]) -> str | None:
    """The generator's --output / --output-feed value (the feed we watch)."""
    for flag in ("--output", "--output-feed", "--out"):
        if flag in gen_argv:
            i = gen_argv.index(flag)
            if i + 1 < len(gen_argv):
                return gen_argv[i + 1]
    return None


def _signal_lines(text: str) -> list[str]:
    """Just the `N. SIDE XAUUSD ...` signal lines, trimmed (skip date headers)."""
    return [ln.strip() for ln in text.splitlines() if _SIGNAL_LINE.match(ln)]


def _new_signals(text: str, seen: set[str]) -> list[str]:
    """Signal lines present in `text` but not yet in `seen` (order preserved)."""
    out = []
    for ln in _signal_lines(text):
        if ln not in seen:
            out.append(ln)
    return out


def _stamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _recent_month_charts(template: str, n: int, today: datetime) -> list[str]:
    """The n most recent monthly chart files matching `template`'s YYYYMM slot.

    `template` is a path containing a `*` where the YYYYMM goes (the glob the
    backtest uses). Returns oldest->newest existing files so EMAs stay warm.
    """
    months = []
    y, m = today.year, today.month
    for _ in range(n):
        months.append(f"{y:04d}{m:02d}")
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    out = []
    for ym in reversed(months):
        cand = template.replace("*", ym, 1)
        if os.path.exists(cand):
            out.append(cand)
    return out


def _effective_gen_argv(gen_argv: list[str], *, start_days: int | None,
                        recent_months: int | None, today: datetime,
                        start_flag: str | None = "--start") -> list[str]:
    """gen_argv with a rolling --start and/or recent-month --charts applied.

    Keeps the single live command identical in *result* to the old per-cycle
    PowerShell loop, but recomputed in-process each pass so it rolls forward
    across day/month boundaries with no shell date math.

    `start_days` REWRITES an existing --start/--start-date when the pass-through
    already carries one, and otherwise INJECTS `start_flag` (the family's start
    flag). Injection is the fix for the silent no-op: without it, omitting --start
    from the pass-through left --gen-start-days doing nothing, so the feed emitted
    the entire loaded chart window (cold-start bars included) instead of the
    intended trailing window -- the root cause of live-vs-backtest signal drift.
    `start_flag=None` (a family with no start flag, e.g. zones) skips injection.
    """
    argv = list(gen_argv)
    if start_days is not None:
        start = (today - timedelta(days=start_days)).strftime("%Y-%m-%d")
        existing = next((f for f in ("--start", "--start-date") if f in argv), None)
        if existing is not None:  # scalper uses --start; risk02/canonical --start-date
            argv[argv.index(existing) + 1] = start
        elif start_flag:
            argv += [start_flag, start]
    if recent_months is not None and "--charts" in argv:
        i = argv.index("--charts")
        # collect the existing chart operands (until the next --flag)
        j = i + 1
        operands = []
        while j < len(argv) and not argv[j].startswith("--"):
            operands.append(argv[j]); j += 1
        template = next((o for o in operands if "*" in o), operands[0] if operands else None)
        if template:
            narrowed = _recent_month_charts(template, recent_months, today)
            if narrowed:
                argv = argv[:i + 1] + narrowed + argv[j:]
    return argv


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
    p.add_argument("--gen-start-days", type=int, default=None,
                   help="Each cycle, set the generator's --start/--start-date "
                        "to (today - N days) so the live feed stays small and "
                        "rolls forward. The flag is rewritten if the pass-through "
                        "carries one and INJECTED if it does not (so it is never a "
                        "silent no-op). Omit to use the --start passed through.")
    p.add_argument("--gen-recent-months", type=int, default=None,
                   help="Each cycle, narrow the generator's --charts glob to the N "
                        "most recent monthly files (fast regen; EMAs stay warm). "
                        "Omit to pass --charts through unchanged.")
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

    from trading.engine import Mt5ChartSource, Mt5Connection, archive_m1_by_month

    conn = Mt5Connection(path=args.mt5_path, login=args.mt5_login,
                         password=args.mt5_password, server=args.mt5_server)
    conn.initialize()
    chart = Mt5ChartSource(conn, symbol=args.mt5_symbol,
                           server_offset_hours=args.mt5_server_offset,
                           history_bars=10)
    feed_path = _output_path(gen_argv)
    last_seen = None        # last closed-bar time we regenerated on
    seen_signals: set[str] = set()
    primed = False          # seeded the seen-set with pre-existing signals yet?
    passes = 0

    # Header once -- like `auto`: announce, then stay quiet until something
    # actually happens (a new signal). No per-cycle "regenerated" noise.
    print(f"[{_stamp()}] live feed loop started | family={args.family} | "
          f"feed={feed_path or '?'} | interval {args.interval:g}s | "
          f"logging each NEW signal as the market forms it (Ctrl+C to stop)",
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
                    eff_argv = _effective_gen_argv(
                        gen_argv, start_days=args.gen_start_days,
                        recent_months=args.gen_recent_months,
                        today=datetime.now(),
                        start_flag=GENERATOR_START_FLAG.get(args.family, "--start"))
                    # Silence the generator's own stdout ("Generated signals: N");
                    # the loop owns user-facing output.
                    with contextlib.redirect_stdout(io.StringIO()):
                        rc = gen_module.main(list(eff_argv))
                    if rc != 0:
                        print(f"[{_stamp()}] generator returned rc={rc}; "
                              f"retrying next pass", flush=True)
                        continue
                    last_seen = last_closed
                    text = ""
                    if feed_path and os.path.exists(feed_path):
                        text = Path(feed_path).read_text()
                    if not primed:
                        # First successful pass: seed without spamming the
                        # whole recent window; only signals added from here on
                        # get logged.
                        seen_signals.update(_signal_lines(text))
                        primed = True
                        print(f"[{_stamp()}] loaded {len(seen_signals)} existing "
                              f"signal(s) from the recent window; will log only "
                              f"new ones from here", flush=True)
                    else:
                        for line in _new_signals(text, seen_signals):
                            seen_signals.add(line)
                            print(f"[{_stamp()}] Add Signal {line}", flush=True)
                # else: no new closed bar -> nothing to do this pass (silent).
            except Exception as e:  # noqa: BLE001 - loop must survive blips
                print(f"[{_stamp()}] pass failed (will retry): {e}", flush=True)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print(f"\n[{_stamp()}] live feed loop stopped after {passes} passes.")
        return 0
    finally:
        conn.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
