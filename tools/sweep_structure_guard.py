#!/usr/bin/env python3
"""Targeted comparison: base TSL18 feed vs structure-guarded variants.

This is deliberately NOT a generic profit sweep. TSL18 already went through the
RSI x Bollinger x R:R feed sweep and the trailing/risk sweeps. The open problem
is narrower: TSL18 takes **sequential losses when it trades against the larger
structure** -- BUY signals while H1 is bearish, SELL signals while H1 is bullish.
So this tool scores the structure guard on the metrics that describe THAT failure
mode, not headline profit:

    net P&L, max drawdown, win rate, profit factor, total trades, loss count,
    max consecutive losses, max daily loss,
    BUY losses during bearish HTF, SELL losses during bullish HTF,
    filtered winners vs filtered losers (what the guard removed).

For each variant it (1) regenerates the self-scalper feed with the variant's
``--structure-*`` flags (the base variant uses none), (2) runs the SAME TSL18
execution geometry through ``backtest_hybrid`` on TICK where available, and
(3) joins each closed trade to the structure-diagnostics so a loss can be tagged
as wrong-side-HTF. The base feed's removed signals are looked up in the base
backtest to report how many filtered signals were winners vs losers.

Run June 2026 first (pure TICK), then Jan->Jun 2026:

    python tools/sweep_structure_guard.py --window june
    python tools/sweep_structure_guard.py --window jan_jun

Output: reports/STRUCTURE_GUARD_<window>/summary.md (+ per-variant workbooks).
The expected result is NOT "the guard is more profitable"; it is a clear,
auditable answer to "does the guard cut wrong-side sequential losses, and at
what cost in filtered winners".
"""
from __future__ import annotations

import argparse
import csv
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# C160 self-scalper feed filter -- identical to TSL18 / T818 (do not change here).
FEED_FILTER = [
    "--session-start", "0", "--session-end", "0", "--signal-tz", "7",
    "--rsi-buy-max", "70", "--rsi-sell-min", "30", "--bb-bandwidth-min", "0.0006",
    "--rr1", "1.2", "--rr2", "2.5", "--rr3", "5",
]

# TSL18 execution geometry -- identical to cli/candidate_TSL18_trailing_tick.txt.
TSL18_GEOMETRY = [
    "--sizing-mode", "risk", "--lot", "0.01", "--risk", "0.01",
    "--minimum-lot", "0.01", "--maximum-lot", "500.0", "--lot-step", "0.01",
    "--bonus-per-closed-lot", "3.0", "--entries", "8", "--entry-ladder", "range_to_sl",
    "--entry-sl-gap", "0.7", "--shared-sl", "false", "--activation-delay", "0",
    "--pending-expiry", "180", "--max-hold", "150", "--sl-multiplier", "1.8",
    "--final-target", "TP3", "--lock-after-tp1", "true", "--lock-after-tp2", "true",
    "--tp1-lock-delay-minutes", "24", "--tp2-lock-delay-minutes", "24",
    "--profit-lock-mode", "tp_levels", "--bep-trigger-distance", "3.0",
    "--tp1-lock-fraction", "0.75", "--tp2-lock-target", "TP1", "--tp3-lock-target", "TP2",
    "--runner-after-tp3", "false", "--trailing-open-distance", "0.5",
    "--trailing-close-distance", "0.5", "--trailing-close-after-stage", "2",
]

# Structure variants to compare. 'base' = no guard (the incumbent TSL18 feed).
# These are starting points keyed to the wrong-side problem, NOT a wide grid.
VARIANTS: dict[str, list[str]] = {
    "base": [],
    "htf_only": ["--structure-filter", "--structure-htf-minutes", "60",
                 "--structure-ema-fast", "20", "--structure-ema-slow", "50"],
    "htf_impulse": ["--structure-filter", "--structure-htf-minutes", "60",
                    "--structure-ema-fast", "20", "--structure-ema-slow", "50",
                    "--structure-impulse-cooldown-bars", "5", "--structure-impulse-atr", "1.5"],
    "htf_vwap_score2": ["--structure-filter", "--structure-htf-minutes", "60",
                        "--structure-ema-fast", "20", "--structure-ema-slow", "50",
                        "--structure-require-vwap-side", "--structure-impulse-cooldown-bars", "5",
                        "--structure-impulse-atr", "1.5", "--structure-min-score", "2"],
}

WINDOWS = {
    "june": ("2026-06-01", "2026-06-30"),
    "jan_jun": ("2026-01-01", "2026-06-30"),
}

ERA_SLIP = ["--lock-tp1-exit-slippage", "2.0", "--lock-tp2-exit-slippage", "1.0"]  # R4


@dataclass
class Trade:
    date: str
    time_chart: str
    side: str
    pnl: float
    status: str


@dataclass
class Metrics:
    variant: str
    trades: int = 0
    net: float = 0.0
    win_rate: float = 0.0
    profit_factor: float = 0.0
    losses: int = 0
    max_consec_losses: int = 0
    max_daily_loss: float = 0.0
    max_drawdown: float = 0.0
    buy_loss_bear_htf: int = 0
    sell_loss_bull_htf: int = 0
    filtered_total: int = 0
    filtered_winners: int = 0
    filtered_losers: int = 0


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)


def _gen_feed(out_txt: Path, struct_csv: Path | None, charts: list[str],
              start: str, structure_flags: list[str]) -> None:
    cmd = [sys.executable, "tools/generate_scalper_signals.py",
           "--charts", *charts, "--output", str(out_txt),
           "--start", start, "--progress-interval-seconds", "0",
           *FEED_FILTER, *structure_flags]
    if struct_csv is not None and structure_flags:
        cmd += ["--structure-diagnostics", str(struct_csv)]
    r = _run(cmd)
    if r.returncode != 0:
        raise SystemExit(f"feed generation failed:\n{r.stderr[-1500:]}")


def _run_backtest(feed: Path, charts: list[str], ticks: list[str],
                  start: str, end: str, out_dir: Path) -> Path:
    cmd = [sys.executable, "tools/backtest_hybrid.py", "--signals", str(feed),
           "--charts", *charts, "--ticks", *ticks,
           "--sync-ticks", "false", "--sync-charts", "false",
           "--max-drawdown-limit-pct", "9999", "--progress-interval-seconds", "0",
           *TSL18_GEOMETRY, *ERA_SLIP,
           "--output-dir", str(out_dir), "--initial-capital", "50000",
           "--start-date", start, "--end-date", end]
    r = _run(cmd)
    if r.returncode != 0:
        raise SystemExit(f"backtest failed:\n{r.stderr[-1500:]}")
    xlsx = out_dir.with_suffix(".xlsx")
    if not xlsx.exists():
        raise SystemExit(f"no workbook at {xlsx}")
    return xlsx


def _read_trades(xlsx: Path) -> list[Trade]:
    from openpyxl import load_workbook

    ws = load_workbook(xlsx, data_only=True)["Per-Entry Detail"]
    hdr = {str(ws.cell(row=2, column=c).value).strip(): c for c in range(1, ws.max_column + 1)}
    out: list[Trade] = []
    for r in range(3, ws.max_row + 1):
        k = ws.cell(row=r, column=hdr["Entry Key"]).value
        pnl = ws.cell(row=r, column=hdr["P&L ($)"]).value
        if not k or pnl is None:
            continue
        out.append(Trade(
            date=str(ws.cell(row=r, column=hdr["Date"]).value),
            time_chart=str(ws.cell(row=r, column=hdr["Time (chart EET/EEST)"]).value),
            side=str(ws.cell(row=r, column=hdr["Side"]).value).upper(),
            pnl=float(pnl),
            status=str(ws.cell(row=r, column=hdr["Status"]).value),
        ))
    return out


def _read_htf(struct_csv: Path) -> dict[tuple[str, str], str]:
    """(chart-time 'YYYY-MM-DD HH:MM', side) -> htf_state, from the diagnostics."""
    out: dict[tuple[str, str], str] = {}
    if not struct_csv.exists():
        return out
    with struct_csv.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            t = row["time"][:16]  # 'YYYY-MM-DD HH:MM'
            out[(t, row["side"].upper())] = row["htf_state"]
    return out


def _metrics(variant: str, trades: list[Trade], htf: dict[tuple[str, str], str]) -> Metrics:
    m = Metrics(variant=variant, trades=len(trades))
    wins = [t.pnl for t in trades if t.pnl > 0]
    losses = [t.pnl for t in trades if t.pnl < 0]
    m.net = round(sum(t.pnl for t in trades), 2)
    m.losses = len(losses)
    m.win_rate = round(100.0 * len(wins) / len(trades), 1) if trades else 0.0
    gross_win, gross_loss = sum(wins), -sum(losses)
    m.profit_factor = round(gross_win / gross_loss, 2) if gross_loss > 0 else float("inf")
    # max consecutive losses (trades are in fill order in the sheet)
    run = best = 0
    for t in trades:
        if t.pnl < 0:
            run += 1
            best = max(best, run)
        else:
            run = 0
    m.max_consec_losses = best
    # max daily loss + a simple equity-curve drawdown (sequential)
    by_day: dict[str, float] = {}
    eq = peak = 0.0
    dd = 0.0
    for t in trades:
        by_day[t.date] = by_day.get(t.date, 0.0) + t.pnl
        eq += t.pnl
        peak = max(peak, eq)
        dd = min(dd, eq - peak)
    m.max_daily_loss = round(min(by_day.values()), 2) if by_day else 0.0
    m.max_drawdown = round(dd, 2)
    # wrong-side-HTF losses
    for t in trades:
        if t.pnl >= 0:
            continue
        state = htf.get((f"{t.time_chart[:16]}", t.side))
        if t.side == "BUY" and state == "bear":
            m.buy_loss_bear_htf += 1
        elif t.side == "SELL" and state == "bull":
            m.sell_loss_bull_htf += 1
    return m


def _filtered_breakdown(base_trades: list[Trade], variant_trades: list[Trade]) -> tuple[int, int, int]:
    """Signals present in base but removed by the variant: winners vs losers."""
    kept = {(t.date, t.time_chart[:16], t.side) for t in variant_trades}
    removed = [t for t in base_trades if (t.date, t.time_chart[:16], t.side) not in kept]
    winners = sum(1 for t in removed if t.pnl > 0)
    losers = sum(1 for t in removed if t.pnl < 0)
    return len(removed), winners, losers


def _write_summary(out_dir: Path, window: str, span: tuple[str, str],
                   results: list[Metrics]) -> Path:
    md = out_dir / "summary.md"
    md.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        f"# Structure-guard comparison — {window} ({span[0]}..{span[1]})",
        "",
        "Base TSL18 feed vs structure-guarded variants, SAME TSL18 geometry, TICK where available.",
        "Judge on the sequential / wrong-side columns, not net alone.",
        "",
        "| variant | trades | net $ | win% | PF | losses | maxConsecL | maxDailyLoss $ | maxDD $ | BUYloss·bearHTF | SELLloss·bullHTF | filtered(W/L) |",
        "|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|",
    ]
    for m in results:
        pf = "inf" if m.profit_factor == float("inf") else f"{m.profit_factor:.2f}"
        lines.append(
            f"| {m.variant} | {m.trades} | {m.net:,.0f} | {m.win_rate} | {pf} | {m.losses} | "
            f"{m.max_consec_losses} | {m.max_daily_loss:,.0f} | {m.max_drawdown:,.0f} | "
            f"{m.buy_loss_bear_htf} | {m.sell_loss_bull_htf} | "
            f"{m.filtered_winners}/{m.filtered_losers} (of {m.filtered_total}) |"
        )
    lines += [
        "",
        "**Read:** a good guard lowers *maxConsecL*, *maxDailyLoss*, *maxDD* and the "
        "wrong-side-HTF loss counts while keeping *filtered losers >> filtered winners*. "
        "If it filters mostly winners, it is hurting — do not promote.",
    ]
    md.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return md


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--window", choices=list(WINDOWS), default="june")
    ap.add_argument("--charts", nargs="+",
                    default=["data/XAUUSD_M1_202601_ELEV8.csv", "data/XAUUSD_M1_202602_ELEV8.csv",
                             "data/XAUUSD_M1_202603_ELEV8.csv", "data/XAUUSD_M1_202604_ELEV8.csv",
                             "data/XAUUSD_M1_202605_ELEV8.csv", "data/XAUUSD_M1_202606_ELEV8.csv"])
    ap.add_argument("--ticks", nargs="+",
                    default=["data/ticks/XAUUSD_TICK_*_ELEV8.csv"])
    ap.add_argument("--variants", nargs="+", default=list(VARIANTS),
                    help=f"subset of {list(VARIANTS)}")
    ap.add_argument("--gen-start", default="2026-01-01",
                    help="feed-generation start (indicator warmup precedes the window).")
    ap.add_argument("--out-root", default="reports")
    args = ap.parse_args(argv)

    start, end = WINDOWS[args.window]
    out_root = Path(args.out_root) / f"STRUCTURE_GUARD_{args.window}"
    out_root.mkdir(parents=True, exist_ok=True)

    base_trades: list[Trade] = []
    results: list[Metrics] = []
    for v in args.variants:
        flags = VARIANTS[v]
        feed = out_root / f"feed_{v}.txt"
        scsv = out_root / f"structure_{v}.csv"
        print(f"[structure-sweep] {v}: generate feed ...", flush=True)
        # always produce a structure CSV (even base) so trades can be HTF-labelled
        label_flags = flags or ["--structure-filter", "--structure-htf-minutes", "60",
                                "--structure-ema-fast", "20", "--structure-ema-slow", "50"]
        if not flags:
            # base feed has NO veto; generate it plain, but ALSO make a label-only
            # structure CSV from a guard-on pass (diagnostics only, feed discarded).
            _gen_feed(feed, None, args.charts, args.gen_start, [])
            _gen_feed(out_root / f"_label_{v}.txt", scsv, args.charts, args.gen_start, label_flags)
        else:
            _gen_feed(feed, scsv, args.charts, args.gen_start, flags)
        print(f"[structure-sweep] {v}: backtest {start}..{end} ...", flush=True)
        xlsx = _run_backtest(feed, args.charts, args.ticks, start, end, out_root / f"bt_{v}")
        trades = _read_trades(xlsx)
        htf = _read_htf(scsv)
        m = _metrics(v, trades, htf)
        if v == "base":
            base_trades = trades
        else:
            m.filtered_total, m.filtered_winners, m.filtered_losers = \
                _filtered_breakdown(base_trades, trades)
        results.append(m)
        print(f"[structure-sweep] {v}: trades={m.trades} net=${m.net:,.0f} "
              f"maxConsecL={m.max_consec_losses} wrongBUY={m.buy_loss_bear_htf} "
              f"wrongSELL={m.sell_loss_bull_htf}", flush=True)

    md = _write_summary(out_root, args.window, (start, end), results)
    print(f"[structure-sweep] summary: {md}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
