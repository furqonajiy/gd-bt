#!/usr/bin/env python3
"""Simulate running BOTH books (TSL18 self-scalper + V817 Victor) together on ONE
shared small ($2K) account -- the realistic deployment, where the risk that
matters is the COMBINED drawdown (both can be underwater at once) and the
account-level gates span both feeds.

It merges the two feeds chronologically and replays them on a SINGLE shared
equity curve via run_hybrid_backtest's ``config_resolver`` (each signal uses its
own strategy's geometry; equity compounds across both). One account-level
DeploymentGate caps concurrency / daily loss / min-lot risk across BOTH books.

ALWAYS TICK where covered (May-Jun 2026); M1 fallback before that -- a Jan->Jun
run is hybrid (~20% tick). Reporting only; promotes nothing.

    python tools/sim_portfolio_small_account.py --window june          # gated $2K
    python tools/sim_portfolio_small_account.py --window june --no-gate # ungated contrast
"""
from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for _p in (str(ROOT), str(ROOT / "tools")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from trading.engine import CsvChartSource, StrategyConfig, parse_signals_file  # noqa: E402
import backtest_explicit as bx  # noqa: E402
import tick_backtest as tk  # noqa: E402
from backtest_hybrid import run_hybrid_backtest  # noqa: E402
from sweep_small_account_deploy import (  # noqa: E402
    TSL18_GEOMETRY, V817_GEOMETRY, WINDOWS, metrics, account_floor_table, _pct,
)

FEEDS = {  # tag -> (geometry, feed file)
    "TSL18": (TSL18_GEOMETRY, "signals/t818.txt"),
    "V817": (V817_GEOMETRY, "victor_signals.txt"),
}


def _cfg(geom, capital, entries, gate: dict) -> StrategyConfig:
    return StrategyConfig(initial_capital=capital,
                          **{**geom, "entry_count": entries}, **gate)


def run(window, charts, ticks, capital, entries, gate_on, max_open,
        daily_pct, zone_pct, single_pct, watch_seconds):
    start, end = WINDOWS[window]
    gate = (dict(risk_budget_gate=True, max_single_entry_risk_pct=single_pct,
                 max_zone_risk_pct=zone_pct, daily_loss_limit_pct=daily_pct,
                 max_open_signals=max_open) if gate_on else {})

    # Per-strategy config + tagged signals; merge chronologically.
    cfg_by_id, merged = {}, []
    per_tag_cfg = {}
    for tag, (geom, feed) in FEEDS.items():
        cfg = _cfg(geom, capital, entries, gate)
        per_tag_cfg[tag] = cfg
        sigs = bx.filter_signals_by_date(
            parse_signals_file(Path(feed), tag=tag), start, end)
        for s in sigs:
            cfg_by_id[id(s)] = (tag, cfg)
            merged.append(s)
    merged.sort(key=lambda s: s.signal_time_chart)

    # base config carries the shared capital + account-level gate; the resolver
    # supplies each signal's own strategy geometry.
    base = _cfg(TSL18_GEOMETRY, capital, entries, gate)
    resolver = lambda s: cfg_by_id.get(id(s), (None, base))[1]

    chart = CsvChartSource(bx._expand_chart_paths(charts))
    tickdf = tk.load_ticks(tk._expand(ticks)) if ticks else None
    label = "GATED" if gate_on else "UNGATED"
    print(f"[portfolio] {label} {window}: {len(merged)} merged signals "
          f"(both books), ticks={0 if tickdf is None else len(tickdf)} rows", flush=True)

    result = run_hybrid_backtest(merged, chart, tickdf, base,
                                 config_resolver=resolver, watch_seconds=watch_seconds)
    m = metrics(result, base)

    # per-strategy P&L attribution (signal_key carries the tag prefix)
    by_tag = defaultdict(lambda: {"signals": 0, "pnl": 0.0, "wins": 0})
    for r in result["rows"]:
        if r["pnl"] is None:
            continue
        tag = r["signal_key"].split("-")[0] if "-" in r["signal_key"] else "?"
        by_tag[tag]["signals"] += 1
        by_tag[tag]["pnl"] += r["pnl"]
        by_tag[tag]["wins"] += int(r["pnl"] > 0)

    _report(window, label, base, m, by_tag, result)
    return m


def _report(window, label, cfg, m, by_tag, result):
    ds = result.get("data_sources", {})
    print(f"\n===== PORTFOLIO (TSL18 + V817) on ONE ${cfg.initial_capital:,.0f} account "
          f"-- {label} -- {window} =====")
    print(f"  data: {ds.get('tick_signals',0)} TICK / {ds.get('m1_signals',0)} M1 signals")
    print(f"  net ${m['net_pnl']:,.0f}  ({m['return_pct']:.0f}% on ${cfg.initial_capital:,.0f})  "
          f"final equity ${m['final_equity']:,.0f}")
    print(f"  max DD {m['max_drawdown_pct']:.1f}%   worst day {m['max_daily_loss_pct']:.1f}%   "
          f"daily WR {m['daily_win_rate']:.0f}%")
    print(f"  signal WR {m['signal_win_rate']:.0f}%   payoff 1:{m['payoff_ratio']:.2f}   "
          f"PF {m['profit_factor']:.2f}   max losing-signal streak {m['max_consecutive_losing_signals']}")
    print(f"  PEAK CONCURRENT signals (both books) {m['max_concurrent_open_signals_seen']}   "
          f"peak open lots {m['max_open_lots_seen']}")
    print(f"  gate rejections: risk-budget {m['signals_rejected_by_risk_budget']}  "
          f"daily {m['signals_rejected_by_daily_loss']}  concurrency {m['signals_rejected_by_concurrency']}")
    print(f"  --- per-book P&L attribution ---")
    for tag, d in sorted(by_tag.items()):
        wr = 100 * d["wins"] / d["signals"] if d["signals"] else 0
        print(f"    {tag:6s}: {d['signals']:4d} signals  net ${d['pnl']:,.0f}  win {wr:.0f}%")


def build_parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--window", choices=list(WINDOWS), default="june")
    p.add_argument("--charts", nargs="+", default=["data/XAUUSD_M1_*_ELEV8.csv"])
    p.add_argument("--ticks", nargs="+", default=["data/ticks/XAUUSD_TICK_*_ELEV8.csv"])
    p.add_argument("--capital", type=float, default=2000.0)
    p.add_argument("--entries", type=int, default=2, help="entries per signal (both books)")
    p.add_argument("--no-gate", action="store_true", help="run ungated (full ladder, no gates) for contrast")
    p.add_argument("--max-open-signals", type=int, default=2,
                   help="account-level concurrency cap across BOTH books (default 2: ~one per book)")
    p.add_argument("--daily-loss-limit-pct", type=float, default=0.05)
    p.add_argument("--max-zone-risk-pct", type=float, default=0.06)
    p.add_argument("--max-single-entry-risk-pct", type=float, default=0.04)
    p.add_argument("--watch-seconds", type=int, default=3)
    return p


def main(argv=None) -> int:
    a = build_parser().parse_args(argv)
    run(a.window, a.charts, a.ticks, a.capital, a.entries, not a.no_gate,
        a.max_open_signals, a.daily_loss_limit_pct, a.max_zone_risk_pct,
        a.max_single_entry_risk_pct, a.watch_seconds)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
