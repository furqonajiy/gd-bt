#!/usr/bin/env python3
"""TSL18 quality-entry research sweep — SKELETON.

Compares the base TSL18 self-scalper feed against **quality-entry** variants
(`--quality-profile`, `--min-quality-score`, `--extreme-entry-mode` on
`tools/generate_scalper_signals.py`) under the SAME TSL18 execution geometry, on
TICK where the archive covers the lifecycle. It scores candidates with the
**rebate-aware** objective (`tools/rebate_scoring.py`) so a dense feed that is
green only on the $3/closed-lot rebate — but flat/negative on pure trading P&L —
is never promoted.

This is a SKELETON: the orchestration, windows, exclusion gates, and the
results/summary/JSON writers are complete and runnable, but it is **not** meant
to be run as a full aggressive sweep in this branch (per the task contract). Use
the tiny `--mode smoke` (or `--skeleton`, which emits the schema with placeholder
rows and runs no backtests) for a fast structural check.

Modes / windows (``--end-date`` is EXCLUSIVE in backtest_hybrid, so a window's
end is the day AFTER the last day kept):

    smoke         2026-06-27 .. 2026-07-01   (last few June days, pure TICK)
    full_june     2026-06-01 .. 2026-07-01
    validate_top  2026-01-01 .. 2026-07-01   (re-score a prior run's top via --top-json)

Outputs (under ``reports/TSL18_QUALITY_<mode>/``):

    results.csv          one row per candidate, full schema (incl. placeholder
                         collision columns — collision logic is NOT in this branch)
    top_candidates.json  ranked survivors (gates passed) by score
    summary.md           human-readable leaderboard + the rebate-guard notes

    python tools/sweep_tsl18_quality_entry.py --mode smoke
    python tools/sweep_tsl18_quality_entry.py --mode smoke --skeleton   # no backtests
"""
from __future__ import annotations

import argparse
import csv
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.rebate_scoring import (  # noqa: E402
    DEFAULT_REBATE_PER_LOT, SCORE_OBJECTIVES, compute_rebate_metrics,
    passes_rebate_guards, score_candidate,
)

# C160 self-scalper feed filter — identical to TSL18 / T818 (mirror; do not tune).
FEED_FILTER = [
    "--session-start", "0", "--session-end", "0", "--signal-tz", "7",
    "--rsi-buy-max", "70", "--rsi-sell-min", "30", "--bb-bandwidth-min", "0.0006",
    "--rr1", "1.2", "--rr2", "2.5", "--rr3", "5",
]

# TSL18 execution geometry — mirror of cli/candidate_TSL18_trailing_tick.txt.
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
ERA_SLIP = ["--lock-tp1-exit-slippage", "2.0", "--lock-tp2-exit-slippage", "1.0"]  # R4

WINDOWS = {
    "smoke": ("2026-06-27", "2026-07-01"),
    "june": ("2026-06-01", "2026-07-01"),
    "jan_jun": ("2026-01-01", "2026-07-01"),
}
MODE_WINDOW = {"smoke": "smoke", "full_june": "june", "validate_top": "jan_jun"}

# Results schema. Placeholder collision columns are intentionally present but
# UNPOPULATED here — collision policy/logic is owned by a separate branch and is
# explicitly NOT implemented in this one.
RESULTS_COLUMNS = [
    "label", "quality_profile", "min_quality_score", "extreme_entry_mode",
    "window", "window_start", "window_end",
    "pure_trading_pnl", "rebate_pnl", "net_pnl", "closed_lots",
    "pure_pnl_per_lot", "net_pnl_per_lot", "rebate_share_of_profit",
    "score_objective", "score",
    "passes_rebate_guards", "rebate_guard_reason",
    "max_drawdown_pct", "win_rate_pct", "signals_included",
    "tick_signals", "m1_signals", "data_source_mixed",
    "partial_tick_lifecycle_excluded", "open_or_pending_left", "included_in_ranking",
    # --- placeholder collision metrics (NOT implemented in this branch) ---
    "collision_policy", "collisions_detected", "collision_pnl_delta",
]

QUALITY_PROFILES = ["off", "trend_only", "reversal_extreme", "hybrid_quality",
                    "high_frequency_quality"]
EXTREME_MODES = ["support_demand", "supply_resistance", "both"]


def build_candidate_grid(mode: str) -> list[dict]:
    """Return the candidate list for a mode. ``base`` (no quality layer) is always
    first so the report can read every variant against it."""
    base = {"label": "base", "quality_profile": "off", "min_quality_score": 0.0,
            "extreme_entry_mode": "off"}
    if mode == "smoke":
        return [
            base,
            {"label": "trend_only", "quality_profile": "trend_only",
             "min_quality_score": 0.0, "extreme_entry_mode": "off"},
            {"label": "extreme_both", "quality_profile": "off",
             "min_quality_score": 0.0, "extreme_entry_mode": "both"},
        ]
    # full_june / validate_top: a bounded research grid (still NOT the full
    # aggressive cross-product — that is deferred per the task contract).
    grid = [base]
    for prof in QUALITY_PROFILES[1:]:
        for score in (0.0, 0.4, 0.6):
            grid.append({"label": f"{prof}_s{score}", "quality_profile": prof,
                         "min_quality_score": score, "extreme_entry_mode": "off"})
    for em in EXTREME_MODES:
        grid.append({"label": f"extreme_{em}", "quality_profile": "off",
                     "min_quality_score": 0.0, "extreme_entry_mode": em})
    return grid


def _quality_flags(cand: dict) -> list[str]:
    """Render a candidate's quality-layer generator flags (empty for base)."""
    flags: list[str] = []
    if cand["quality_profile"] != "off":
        flags += ["--quality-profile", str(cand["quality_profile"])]
        if float(cand.get("min_quality_score", 0.0)) > 0.0:
            flags += ["--min-quality-score", str(cand["min_quality_score"])]
    if cand["extreme_entry_mode"] != "off":
        flags += ["--extreme-entry-mode", str(cand["extreme_entry_mode"])]
    return flags


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)


def _gen_feed(out_txt: Path, charts: list[str], start: str, cand: dict) -> None:
    cmd = [sys.executable, "tools/generate_scalper_signals.py",
           "--charts", *charts, "--output", str(out_txt),
           "--start", start, "--progress-interval-seconds", "0",
           *FEED_FILTER, *_quality_flags(cand)]
    r = _run(cmd)
    if r.returncode != 0:
        raise SystemExit(f"feed generation failed for {cand['label']}:\n{r.stderr[-1500:]}")


def _run_backtest(feed: Path, charts: list[str], ticks: list[str],
                  start: str, end: str, out_dir: Path, score_json: Path) -> Path:
    cmd = [sys.executable, "tools/backtest_hybrid.py", "--signals", str(feed),
           "--charts", *charts, "--ticks", *ticks,
           "--sync-ticks", "false", "--sync-charts", "false",
           "--max-drawdown-limit-pct", "9999", "--progress-interval-seconds", "0",
           *TSL18_GEOMETRY, *ERA_SLIP,
           "--output-dir", str(out_dir), "--initial-capital", "50000",
           "--score-json", str(score_json),
           "--start-date", start, "--end-date", end]
    r = _run(cmd)
    if r.returncode != 0:
        raise SystemExit(f"backtest failed for {feed.name}:\n{r.stderr[-1500:]}")
    xlsx = out_dir.with_suffix(".xlsx")
    if not xlsx.exists():
        raise SystemExit(f"no workbook at {xlsx}")
    return xlsx


def _read_summary_pnl(xlsx: Path) -> tuple[float, float]:
    """(pure_trading_pnl, rebate_pnl) from the workbook Summary key/value cells."""
    from openpyxl import load_workbook

    ws = load_workbook(xlsx, data_only=True)["Summary"]
    pure = rebate = 0.0
    for r in range(1, ws.max_row + 1):
        label = ws.cell(row=r, column=1).value
        if not label:
            continue
        val = ws.cell(row=r, column=2).value
        text = str(label).strip()
        if text == "Trading P&L":
            pure = _money(val)
        elif text == "Closed-lot bonus":
            rebate = _money(val)
    return pure, rebate


def _count_open_pending(xlsx: Path) -> int:
    """Count entries still OPEN/PENDING at window end (incomplete P&L)."""
    from openpyxl import load_workbook

    ws = load_workbook(xlsx, data_only=True)["Per-Entry Detail"]
    hdr = {str(ws.cell(row=2, column=c).value).strip(): c
           for c in range(1, ws.max_column + 1)}
    status_col = hdr.get("Status")
    if not status_col:
        return 0
    n = 0
    for r in range(3, ws.max_row + 1):
        status = ws.cell(row=r, column=status_col).value
        if status and str(status).upper() in ("OPEN", "PENDING"):
            n += 1
    return n


def _money(val) -> float:
    if val is None:
        return 0.0
    if isinstance(val, (int, float)):
        return float(val)
    s = str(val).replace("$", "").replace(",", "").strip()
    try:
        return float(s)
    except ValueError:
        return 0.0


def _blank_row(cand: dict, window: str, span: tuple[str, str], objective: str) -> dict:
    """A schema-complete placeholder row (skeleton / no-backtest mode)."""
    return {
        "label": cand["label"], "quality_profile": cand["quality_profile"],
        "min_quality_score": cand["min_quality_score"],
        "extreme_entry_mode": cand["extreme_entry_mode"],
        "window": window, "window_start": span[0], "window_end": span[1],
        "pure_trading_pnl": "", "rebate_pnl": "", "net_pnl": "", "closed_lots": "",
        "pure_pnl_per_lot": "", "net_pnl_per_lot": "", "rebate_share_of_profit": "",
        "score_objective": objective, "score": "",
        "passes_rebate_guards": "", "rebate_guard_reason": "skeleton",
        "max_drawdown_pct": "", "win_rate_pct": "", "signals_included": "",
        "tick_signals": "", "m1_signals": "", "data_source_mixed": "",
        "partial_tick_lifecycle_excluded": "", "open_or_pending_left": "",
        "included_in_ranking": "",
        "collision_policy": "", "collisions_detected": "", "collision_pnl_delta": "",
    }


def evaluate_candidate(cand: dict, window: str, span: tuple[str, str], *,
                       charts: list[str], ticks: list[str], gen_start: str,
                       out_root: Path, args: argparse.Namespace) -> dict:
    """Run one candidate (or emit a placeholder row in --skeleton mode)."""
    if args.skeleton:
        return _blank_row(cand, window, span, args.score_objective)

    start, end = span
    feed = out_root / f"feed_{cand['label']}.txt"
    score_json = out_root / f"score_{cand['label']}.json"
    _gen_feed(feed, charts, gen_start, cand)
    xlsx = _run_backtest(feed, charts, ticks, start, end, out_root / f"bt_{cand['label']}", score_json)

    score = json.loads(score_json.read_text(encoding="utf-8"))
    pure, rebate = _read_summary_pnl(xlsx)
    closed_lots = rebate / args.rebate_per_lot if args.rebate_per_lot > 0 else 0.0
    m = compute_rebate_metrics(pure, closed_lots, rebate_per_lot=args.rebate_per_lot,
                               rebate_pnl=rebate)
    dd = float(score.get("max_drawdown_pct", 0.0))
    guard_ok, guard_reason = passes_rebate_guards(
        m, min_pure_trading_pnl=args.min_pure_trading_pnl,
        max_rebate_share_of_profit=args.max_rebate_share_of_profit)
    sc = score_candidate(m, args.score_objective, max_drawdown_pct=dd,
                         min_pure_trading_pnl=args.min_pure_trading_pnl,
                         max_rebate_share_of_profit=args.max_rebate_share_of_profit)
    tick_n = int(score.get("tick_signals", 0))
    m1_n = int(score.get("m1_signals", 0))
    mixed = tick_n > 0 and m1_n > 0
    partial_excluded = bool(args.require_full_tick_lifecycle and mixed)
    open_left = _count_open_pending(xlsx) > 0
    included = (guard_ok and not partial_excluded
                and not (args.exclude_open_or_pending and open_left))

    row = _blank_row(cand, window, span, args.score_objective)
    row.update({
        "pure_trading_pnl": m.pure_trading_pnl, "rebate_pnl": m.rebate_pnl,
        "net_pnl": m.net_pnl, "closed_lots": m.closed_lots,
        "pure_pnl_per_lot": m.pure_pnl_per_lot, "net_pnl_per_lot": m.net_pnl_per_lot,
        "rebate_share_of_profit": m.rebate_share_of_profit,
        "score": round(sc, 2), "passes_rebate_guards": guard_ok,
        "rebate_guard_reason": guard_reason,
        "max_drawdown_pct": dd, "win_rate_pct": float(score.get("win_rate_pct", 0.0)),
        "signals_included": int(score.get("signals_included", 0)),
        "tick_signals": tick_n, "m1_signals": m1_n, "data_source_mixed": mixed,
        "partial_tick_lifecycle_excluded": partial_excluded,
        "open_or_pending_left": open_left, "included_in_ranking": included,
    })
    return row


def write_results_csv(rows: list[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=RESULTS_COLUMNS)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in RESULTS_COLUMNS})


def write_top_candidates_json(rows: list[dict], path: Path) -> list[dict]:
    """Rank ranking-included rows by score desc; write + return them."""
    ranked = [r for r in rows if r.get("included_in_ranking") is True]
    ranked.sort(key=lambda r: (r.get("score") if isinstance(r.get("score"), (int, float))
                               else float("-inf")), reverse=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(ranked, indent=2), encoding="utf-8")
    return ranked


def write_summary_md(rows: list[dict], ranked: list[dict], path: Path,
                     mode: str, span: tuple[str, str], objective: str) -> None:
    lines = [
        f"# TSL18 quality-entry sweep — {mode} ({span[0]}..{span[1]})",
        "",
        f"Score objective: **{objective}**. Base TSL18 feed vs quality-entry variants, "
        "same TSL18 geometry, TICK where covered. **Rebate-aware**: a candidate green "
        "only on the $3/closed-lot rebate (bad pure P&L) is guarded out, never promoted.",
        "",
        "| label | profile | minScore | extreme | pure $ | rebate $ | net $ | "
        "rebate share | score | guards | DD% | mixed | open | ranked |",
        "|---|---|--:|---|--:|--:|--:|--:|--:|:--:|--:|:--:|:--:|:--:|",
    ]
    for r in rows:
        lines.append(
            f"| {r['label']} | {r['quality_profile']} | {r['min_quality_score']} | "
            f"{r['extreme_entry_mode']} | {r['pure_trading_pnl']} | {r['rebate_pnl']} | "
            f"{r['net_pnl']} | {r['rebate_share_of_profit']} | {r['score']} | "
            f"{r['rebate_guard_reason']} | {r['max_drawdown_pct']} | "
            f"{r['data_source_mixed']} | {r['open_or_pending_left']} | "
            f"{r['included_in_ranking']} |")
    lines += [
        "",
        "## Ranked survivors (gates passed)",
        "",
    ]
    if ranked:
        for i, r in enumerate(ranked, start=1):
            lines.append(f"{i}. **{r['label']}** — score {r['score']} "
                         f"(net ${r['net_pnl']}, pure ${r['pure_trading_pnl']})")
    else:
        lines.append("_No survivors (skeleton run, or all candidates failed the gates)._")
    lines += [
        "",
        "Gates: **rebate guards** (pure-P&L floor + max rebate share), "
        "**partial-tick-lifecycle exclusion** (mixed TICK/M1 windows when "
        "`--require-full-tick-lifecycle`), and **open/pending-left** "
        "(`--exclude-open-or-pending`). The `collision_*` columns are placeholders — "
        "collision policy is a SEPARATE branch and is not implemented here.",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mode", choices=list(MODE_WINDOW), default="smoke")
    ap.add_argument("--window", choices=list(WINDOWS), default=None,
                    help="override the mode's default window.")
    ap.add_argument("--skeleton", action="store_true",
                    help="emit the results schema with placeholder rows; run NO backtests.")
    ap.add_argument("--charts", nargs="+",
                    default=["data/XAUUSD_M1_202606_ELEV8.csv"])
    ap.add_argument("--ticks", nargs="+", default=["data/ticks/XAUUSD_TICK_*_ELEV8.csv"])
    ap.add_argument("--gen-start", default="2026-06-01",
                    help="feed-generation start (indicator warmup precedes the window).")
    ap.add_argument("--top-json", default=None,
                    help="validate_top: a prior run's top_candidates.json to re-score.")
    ap.add_argument("--out-root", default="reports")
    # rebate-aware scoring knobs (see tools/rebate_scoring.py)
    ap.add_argument("--rebate-per-lot", type=float, default=DEFAULT_REBATE_PER_LOT)
    ap.add_argument("--min-pure-trading-pnl", type=float, default=0.0,
                    help="reject candidates whose pure trading P&L < X.")
    ap.add_argument("--max-rebate-share-of-profit", type=float, default=0.50,
                    help="reject candidates whose net profit is >X rebate.")
    ap.add_argument("--score-objective", choices=list(SCORE_OBJECTIVES),
                    default="edge_plus_rebate_guarded")
    # exclusion gates
    ap.add_argument("--require-full-tick-lifecycle", action="store_true",
                    help="exclude candidates whose window mixes TICK and M1 (partial coverage).")
    ap.add_argument("--exclude-open-or-pending", action="store_true",
                    help="exclude candidates that left positions OPEN/PENDING at window end.")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    window = args.window or MODE_WINDOW[args.mode]
    span = WINDOWS[window]
    out_root = Path(args.out_root) / f"TSL18_QUALITY_{args.mode}"
    out_root.mkdir(parents=True, exist_ok=True)

    if args.mode == "validate_top" and args.top_json:
        prior = json.loads(Path(args.top_json).read_text(encoding="utf-8"))
        candidates = [{"label": c["label"], "quality_profile": c["quality_profile"],
                       "min_quality_score": c["min_quality_score"],
                       "extreme_entry_mode": c["extreme_entry_mode"]} for c in prior]
    else:
        candidates = build_candidate_grid(args.mode)

    rows: list[dict] = []
    for cand in candidates:
        print(f"[quality-sweep] {cand['label']}: profile={cand['quality_profile']} "
              f"score>={cand['min_quality_score']} extreme={cand['extreme_entry_mode']}"
              f"{' [skeleton]' if args.skeleton else ''}", flush=True)
        rows.append(evaluate_candidate(cand, window, span, charts=args.charts,
                                       ticks=args.ticks, gen_start=args.gen_start,
                                       out_root=out_root, args=args))

    results_csv = out_root / "results.csv"
    top_json = out_root / "top_candidates.json"
    summary = out_root / "summary.md"
    write_results_csv(rows, results_csv)
    ranked = write_top_candidates_json(rows, top_json)
    write_summary_md(rows, ranked, summary, args.mode, span, args.score_objective)
    print(f"[quality-sweep] results: {results_csv}")
    print(f"[quality-sweep] top:     {top_json} ({len(ranked)} survivors)")
    print(f"[quality-sweep] summary: {summary}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
