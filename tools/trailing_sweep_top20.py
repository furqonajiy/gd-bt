#!/usr/bin/env python3
"""Build a per-regime TOP-N leaderboard from the trailing-sweep cell artifacts.

The trailing sweep (``self-scalper-trailing-sweep-r4r3r2r1.yml``) uploads one
artifact per (regime, trailing-open, trailing-close) cell named
``selftrail-<REG>-<cell>`` containing ``out/**/results.jsonl`` (the sweep's
per-candidate rows). This tool pools every completed cell of a regime, keeps the
deployable survivors (DD <= gate AND OOS > 0), ranks them by fixed-lot **edge**
(``fixed_no_bonus_profit`` -- the reliable forward-fit metric), and writes the
top-N per regime to CSV + a combined Excel workbook.

It works on a PARTIAL run (whatever cells have finished), so it can snapshot a
sweep that is still in progress. Coverage (cells found per regime) is reported in
the README so a partial snapshot is never mistaken for the final result.

    python tools/trailing_sweep_top20.py --artifacts-dir _artifacts \
        --out-dir sweep_reports/trailing_top20 --dd-gate 40 --top-n 20

IMPORTANT: trailing is live-parity-fragile (a small trailing-open can flatter the
backtest with better-than-real entry fills). These leaderboards RANK candidates;
they do NOT certify them for live. Forward/demo-validate before any deployment.
"""
from __future__ import annotations

import argparse
import csv
import glob
import json
import os
import re
from collections import defaultdict

REGIMES = ("R4", "R3", "R2", "R1")
REGIME_LABEL = {
    "R4": "R4 parabolic (2026)",
    "R3": "R3 strong (2025)",
    "R2": "R2 bull (2023-10..2024)",
    "R1": "R1 quiet (2021-11..2023-09)",
}
# columns pulled from each candidate's config for the leaderboard
CFG_COLS = ["trailing_open_distance", "trailing_close_distance", "entry_count",
            "sl_multiplier", "max_hold_minutes", "tp1_lock_delay_minutes",
            "tp1_lock_fraction", "lock_after_tp2"]


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def load_cells(artifacts_dir: str) -> dict[str, list[dict]]:
    """regime -> list of candidate rows (each tagged with its cell name)."""
    by_regime: dict[str, list[dict]] = defaultdict(list)
    for d in sorted(glob.glob(os.path.join(artifacts_dir, "selftrail-*"))):
        m = re.match(r"selftrail-(R[1-4])-(.+)$", os.path.basename(d))
        if not m:
            continue
        regime, cell = m.group(1), m.group(2)
        jsonls = glob.glob(os.path.join(d, "**", "results.jsonl"), recursive=True)
        if not jsonls:
            continue
        for line in open(jsonls[0]):
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            row["_cell"] = cell
            by_regime[regime].append(row)
    return by_regime


def top_n(rows: list[dict], dd_gate: float, n: int) -> list[dict]:
    elig = [r for r in rows
            if (_f(r.get("concurrent_risk_max_dd_pct")) is not None
                and abs(_f(r.get("concurrent_risk_max_dd_pct"))) <= dd_gate
                and (_f(r.get("oos_fixed_no_bonus_profit")) or -1) > 0)]
    elig.sort(key=lambda r: _f(r.get("fixed_no_bonus_profit")) or -1e18, reverse=True)
    return elig[:n]


def write_regime_csv(path: str, rows: list[dict]) -> None:
    cols = (["rank", "cell", "edge_fixed_no_bonus", "oos_fixed_no_bonus",
             "concurrent_risk_max_dd_pct", "fixed_with_bonus", "stable_fraction"]
            + CFG_COLS + ["candidate_id"])
    with open(path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(cols)
        for i, r in enumerate(rows, 1):
            cfg = r.get("config", {})
            w.writerow([i, r.get("_cell"),
                        f"{_f(r.get('fixed_no_bonus_profit')):.2f}",
                        f"{_f(r.get('oos_fixed_no_bonus_profit')):.2f}",
                        f"{_f(r.get('concurrent_risk_max_dd_pct')):.2f}",
                        f"{_f(r.get('fixed_with_bonus_profit')) or 0:.2f}",
                        f"{_f(r.get('stable_fraction')) or 0:.3f}"]
                       + [cfg.get(c) for c in CFG_COLS]
                       + [r.get("candidate_id")])


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--artifacts-dir", default="_artifacts")
    p.add_argument("--out-dir", default="sweep_reports/trailing_top20")
    p.add_argument("--dd-gate", type=float, default=40.0)
    p.add_argument("--top-n", type=int, default=20)
    p.add_argument("--run-id", default="", help="recorded in the README for traceability")
    args = p.parse_args(argv)

    os.makedirs(args.out_dir, exist_ok=True)
    by_regime = load_cells(args.artifacts_dir)
    coverage = {}
    tops = {}
    for reg in REGIMES:
        rows = by_regime.get(reg, [])
        cells = sorted({r["_cell"] for r in rows})
        coverage[reg] = cells
        tops[reg] = top_n(rows, args.dd_gate, args.top_n)
        write_regime_csv(os.path.join(args.out_dir, f"{reg}_top{args.top_n}.csv"), tops[reg])

    # Combined Excel: one sheet per regime.
    try:
        import openpyxl
        wb = openpyxl.Workbook()
        wb.remove(wb.active)
        for reg in REGIMES:
            ws = wb.create_sheet(reg)
            ws.append(["rank", "cell", "edge_fixed_no_bonus", "oos_fixed_no_bonus",
                       "DD%", "fixed_with_bonus", "stable_frac"] + CFG_COLS)
            for i, r in enumerate(tops[reg], 1):
                cfg = r.get("config", {})
                ws.append([i, r.get("_cell"),
                           round(_f(r.get("fixed_no_bonus_profit")), 2),
                           round(_f(r.get("oos_fixed_no_bonus_profit")), 2),
                           round(_f(r.get("concurrent_risk_max_dd_pct")), 2),
                           round(_f(r.get("fixed_with_bonus_profit")) or 0, 2),
                           round(_f(r.get("stable_fraction")) or 0, 3)]
                          + [cfg.get(c) for c in CFG_COLS])
        wb.save(os.path.join(args.out_dir, f"trailing_top{args.top_n}.xlsx"))
    except Exception as exc:  # openpyxl missing or write error -> CSVs still written
        print(f"[top20] Excel skipped: {exc!r}")

    # README with coverage + the parity caveat.
    grid_cells = 49
    with open(os.path.join(args.out_dir, "README.md"), "w") as fh:
        fh.write("# Trailing sweep — per-regime TOP-%d (PARTIAL SNAPSHOT)\n\n" % args.top_n)
        if args.run_id:
            fh.write(f"Source run: `{args.run_id}` "
                     f"(`self-scalper-trailing-sweep-r4r3r2r1.yml`).\n\n")
        fh.write(f"Gate: DD <= {args.dd_gate:.0f}%% AND OOS > 0, ranked by fixed-lot "
                 f"edge (`fixed_no_bonus_profit`).\n\n")
        fh.write("## Coverage (completed cells per regime, of %d in the full grid)\n\n" % grid_cells)
        for reg in REGIMES:
            cells = coverage[reg]
            fh.write(f"- **{reg}** ({REGIME_LABEL[reg]}): {len(cells)}/{grid_cells} cells, "
                     f"{len(tops[reg])} gate-passing in top-{args.top_n}.\n")
        fh.write("\n## ⚠️ Read before acting\n\n"
                 "This is a **partial, in-progress** snapshot, not the final per-regime "
                 "winner-vs-base verdict (that comes from the `r*_agg` leaderboards once "
                 "each regime's full grid finishes).\n\n"
                 "**Trailing is live-parity-fragile.** A small trailing-open (0.1-0.2) can "
                 "flatter the backtest with better-than-real entry fills, so the very large "
                 "edge figures here are suspected modeling artifacts. These tables RANK "
                 "candidates; they do NOT certify them for live. Forward/demo-validate the "
                 "top trailing-open cells (and check entry-fill realism) before any deploy.\n")

    # Console summary.
    for reg in REGIMES:
        cells = coverage[reg]
        best = tops[reg][0] if tops[reg] else None
        if best:
            print(f"{reg}: {len(cells)} cells, top-{args.top_n} written; #1 "
                  f"{best['_cell']} edge={_f(best.get('fixed_no_bonus_profit')):,.0f} "
                  f"OOS={_f(best.get('oos_fixed_no_bonus_profit')):,.0f} "
                  f"DD={_f(best.get('concurrent_risk_max_dd_pct')):.1f}%")
        else:
            print(f"{reg}: {len(cells)} cells, no gate-passing candidate yet")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
