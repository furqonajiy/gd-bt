#!/usr/bin/env python3
"""Pool TWO deployment books on ONE shared account and emit the standard workbook.

Runs both books' feeds on a single shared-equity account via
``run_hybrid_backtest``'s ``config_resolver``: each signal uses its OWN book's
geometry + risk sizing, equity compounds across both, and ONE account-level
``DeploymentGate`` (from the ``--gate-from`` book's config) caps concurrency /
daily-loss across BOTH feeds. The COMBINED drawdown -- both books can be
underwater at once -- is the number that matters for a shared small account.

Each book is described by its EXACT ``backtest_hybrid.py`` command (a file
holding that one line), so the pooled per-book geometry is byte-identical to the
single-book run. The window / capital / output-dir come from THIS tool's flags
(overriding whatever the per-book command carried). TICK-preferred where the
archive covers the lifecycle, M1 fallback before that, exactly like every other
hybrid backtest.

    python tools/sim_pool_two_books.py \
      --book V073A=cmd_v073a.txt --book TS3K=cmd_ts3k.txt --gate-from TS3K \
      --capital 3000 --start-date 2026-01-01 --end-date 2026-07-03 \
      --output-dir reports/V073A_TS3K_202601_3K

Research/reporting only; promotes nothing, never trades live.
"""
from __future__ import annotations

import argparse
import shlex
import sys
from collections import defaultdict
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for _p in (str(ROOT), str(ROOT / "tools")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from trading.engine import CsvChartSource, parse_signals_file, write_backtest_outputs  # noqa: E402
import backtest_explicit as bx  # noqa: E402
import tick_backtest as tk  # noqa: E402
from backtest_hybrid import build_parser as hybrid_parser, run_hybrid_backtest  # noqa: E402


def _config_from_command(cmd_line: str, capital: float):
    """Parse a full ``python tools/backtest_hybrid.py ...`` line into (config, feed).

    The geometry/sizing/gate come from ``config_from_args`` on the command's own
    flags, so the pooled per-book config matches the single-book run exactly;
    only ``initial_capital`` is overridden to the shared account size."""
    if "backtest_hybrid.py" not in cmd_line:
        raise SystemExit(f"book command is not a backtest_hybrid.py line: {cmd_line[:80]}")
    argv = shlex.split(cmd_line.split("backtest_hybrid.py", 1)[1])
    ns = hybrid_parser().parse_args(argv)
    cfg = replace(bx.config_from_args(ns), initial_capital=capital)
    return cfg, ns.signals


def _load_book(spec: str, capital: float):
    """`TAG=path/to/cmdfile` -> (tag, config, feed_path)."""
    if "=" not in spec:
        raise SystemExit(f"--book must be TAG=cmdfile, got {spec!r}")
    tag, path = spec.split("=", 1)
    cmd = Path(path).read_text(encoding="utf-8").strip()
    cfg, feed = _config_from_command(cmd, capital)
    return tag, cfg, feed


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--book", action="append", required=True,
                   help="TAG=cmdfile (pass exactly twice).")
    p.add_argument("--gate-from", required=True,
                   help="which book's config supplies the account-level DeploymentGate.")
    p.add_argument("--capital", type=float, default=3000.0)
    p.add_argument("--start-date", required=True)
    p.add_argument("--end-date", default=None, help="EXCLUSIVE (as in backtest_hybrid).")
    p.add_argument("--output-dir", required=True)
    p.add_argument("--charts", nargs="+", default=["data/XAUUSD_M1_*_ELEV8.csv"])
    p.add_argument("--ticks", nargs="+", default=["data/ticks/XAUUSD_TICK_*_ELEV8.csv"])
    p.add_argument("--watch-seconds", type=int, default=5)
    a = p.parse_args(argv)
    if len(a.book) != 2:
        raise SystemExit("pass --book exactly twice (two books to pool).")

    books = {}
    for spec in a.book:
        tag, cfg, feed = _load_book(spec, a.capital)
        books[tag] = (cfg, feed)
    if a.gate_from not in books:
        raise SystemExit(f"--gate-from {a.gate_from} is not one of {list(books)}")
    base = books[a.gate_from][0]   # account-level gate + shared capital come from here

    cfg_by_id, merged = {}, []
    for tag, (cfg, feed) in books.items():
        sigs = bx.filter_signals_by_date(
            parse_signals_file(Path(feed), tag=tag), a.start_date, a.end_date)
        for s in sigs:
            cfg_by_id[id(s)] = cfg
            merged.append(s)
    merged.sort(key=lambda s: s.signal_time_chart)
    resolver = lambda s: cfg_by_id.get(id(s), base)

    chart = CsvChartSource(bx._expand_chart_paths(a.charts))
    tickdf = tk.load_ticks(tk._expand(a.ticks)) if a.ticks else None
    tags = " + ".join(books)
    print(f"[pool] {tags} on ONE ${a.capital:,.0f} account | gate-from={a.gate_from} "
          f"| {len(merged)} merged signals | ticks={0 if tickdf is None else len(tickdf):,} rows "
          f"| {a.start_date}..{a.end_date or 'end'}", flush=True)

    result = run_hybrid_backtest(merged, chart, tickdf, base,
                                 config_resolver=resolver, watch_seconds=a.watch_seconds)
    path = write_backtest_outputs(result, Path(a.output_dir))

    # combined headline + per-book P&L attribution (signal_key carries the tag).
    agg = result.get("summary", result)
    by_tag = defaultdict(lambda: {"signals": 0, "pnl": 0.0, "wins": 0})
    for r in result["rows"]:
        if r.get("pnl") is None:
            continue
        tag = r["signal_key"].split("-")[0] if "-" in r["signal_key"] else "?"
        by_tag[tag]["signals"] += 1
        by_tag[tag]["pnl"] += r["pnl"]
        by_tag[tag]["wins"] += int(r["pnl"] > 0)
    ds = result.get("data_sources", {})
    print(f"\n===== POOLED {tags} on ${a.capital:,.0f} =====")
    print(f"  data: {ds.get('tick_signals',0)} TICK / {ds.get('m1_signals',0)} M1")
    print(f"  report: {path}")
    print(f"  --- per-book P&L attribution ---")
    for tag, d in sorted(by_tag.items()):
        wr = 100 * d["wins"] / d["signals"] if d["signals"] else 0
        print(f"    {tag:6s}: {d['signals']:4d} closed  net ${d['pnl']:,.0f}  win {wr:.0f}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
