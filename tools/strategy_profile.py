#!/usr/bin/env python3
"""Extended win-rate / R:R / daily-stability profile of a backtest workbook.

The Phase leaderboards rank on edge / OOS / net+bonus / drawdown — none of which
tell you the *shape* of the return: win rate, where the wins come from (TP1/2/3
vs SL vs time-exit vs trailing-stop), the realized payoff (avg win : avg loss),
expectancy, and how many DAYS are green. This reads a `backtest_hybrid` /
`backtest_explicit` workbook's "Per-Entry Detail" sheet and prints exactly that.

It is reporting-only — it never runs a backtest, changes config, or touches a
strategy. Point it at any workbook (or a dir containing one .xlsx).

    python tools/strategy_profile.py reports/SQZ6_2026xx/<run>.xlsx
    python tools/strategy_profile.py reports/STRUCTURE_GUARD_june/bt_base.xlsx --label TSL18-June
"""
from __future__ import annotations

import argparse
import collections
import glob
import os

import openpyxl

# Per-Entry Detail column indices (row 2 sub-header is the contract; see
# reporting/excel_report.py). 0-based.
C_KEY, C_DATE, C_SIDE = 0, 1, 4
C_STATUS, C_PNL, C_RR = 16, 20, 21


def _resolve(path: str) -> str:
    if os.path.isdir(path):
        xs = sorted(glob.glob(os.path.join(path, "*.xlsx")))
        if not xs:
            raise SystemExit(f"no .xlsx in {path}")
        return xs[0]
    return path


def profile(path: str, label: str | None = None) -> dict:
    path = _resolve(path)
    label = label or os.path.basename(path)
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = wb["Per-Entry Detail"]
    rows = [r for r in list(ws.iter_rows(values_only=True))[2:]
            if r and r[C_KEY] and r[C_STATUS]]

    filled = [r for r in rows if r[C_STATUS] != "NO_FILL"]
    status = collections.Counter(r[C_STATUS] for r in filled)
    wins = [r[C_PNL] for r in filled if (r[C_PNL] or 0) > 0]
    losses = [r[C_PNL] for r in filled if (r[C_PNL] or 0) < 0]

    sig = collections.defaultdict(float)
    day = collections.defaultdict(float)
    for r in filled:
        sig[r[C_KEY].split(".")[0]] += (r[C_PNL] or 0.0)
        day[r[C_DATE]] += (r[C_PNL] or 0.0)

    n = len(filled) or 1
    avgw = sum(wins) / len(wins) if wins else 0.0
    avgl = sum(losses) / len(losses) if losses else 0.0
    net = sum(wins) + sum(losses)
    payoff = (avgw / abs(avgl)) if avgl else float("inf")
    # expectancy per unit risk: WR*payoff - (1-WR)  (R-multiple)
    wr = len(wins) / n
    exp_R = wr * payoff - (1 - wr) if avgl else float("inf")

    out = {
        "label": label, "filled": n, "signals": len(sig), "days": len(day),
        "entry_wr": 100 * len(wins) / n,
        "signal_wr": 100 * sum(1 for v in sig.values() if v > 0) / max(len(sig), 1),
        "daily_wr": 100 * sum(1 for v in day.values() if v > 0) / max(len(day), 1),
        "status": dict(status), "avg_win": avgw, "avg_loss": avgl,
        "payoff": payoff, "expectancy_R": exp_R,
        "net": net, "exp_entry": net / n, "exp_signal": net / max(len(sig), 1),
        "best_day": max(day.values()) if day else 0.0,
        "worst_day": min(day.values()) if day else 0.0,
    }
    return out


def _print(p: dict) -> None:
    n = p["filled"]
    print(f"\n===== {p['label']} =====")
    print(f"filled entries={n}  signals={p['signals']}  trading days={p['days']}")
    print(f"WIN RATE   entry {p['entry_wr']:.1f}%   signal {p['signal_wr']:.1f}%   "
          f"DAILY {p['daily_wr']:.1f}%")
    print("exit mix:  " + "  ".join(
        f"{k} {100*v/n:.1f}%" for k, v in
        sorted(p["status"].items(), key=lambda kv: -kv[1])))
    print(f"avg win ${p['avg_win']:.2f}   avg loss ${p['avg_loss']:.2f}   "
          f"payoff 1:{p['payoff']:.2f}   expectancy {p['expectancy_R']:+.2f}R")
    print(f"net ${p['net']:,.0f}   exp/entry ${p['exp_entry']:.2f}   "
          f"exp/signal ${p['exp_signal']:.2f}")
    print(f"best day ${p['best_day']:,.0f}   worst day ${p['worst_day']:,.0f}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("workbooks", nargs="+", help="workbook .xlsx or dir(s)")
    ap.add_argument("--label", default=None, help="label (single workbook only)")
    a = ap.parse_args()
    for w in a.workbooks:
        _print(profile(w, a.label if len(a.workbooks) == 1 else None))


if __name__ == "__main__":
    main()
