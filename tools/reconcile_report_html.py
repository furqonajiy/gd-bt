#!/usr/bin/env python3
"""Reconcile an MT5 *Trade History Report* (HTML) against a backtest workbook.

This is the method used repeatedly in the 2026-06-16 live<->backtest sessions,
captured as a tool so it is one command next time (the slippage calibration loop
in docs/VICTOR_SWEEP_RUNBOOK.md step 3 depends on it). Unlike
``tools/parity_reconcile.py`` (which needs the executor's forensic/notification
JSONL), this reads the **HTML report you export from the MT5 terminal**
(History tab -> right-click -> Report -> HTML) -- the thing that's easy to get.

It matches each live closed position to its backtest per-entry leg by the order
**comment** ``[TAG-]MMDD#DD.N`` <-> backtest key ``YYYY-MM-DD#DD.N``, then reports,
per exit type (SL / TP3 / LOCK_TP1 / LOCK_TP2 / TIME_EXIT): how live P&L and exit
PRICE compare to the backtest. Locked exits are where live gives back slippage;
SL/TP3 should match to the cent. It also flags **churn** (one key opened more
than once -> reopen duplicates) and **likely manual closes** (live close not at
the backtest exit, the SL, or a TP). Read-only; never touches MT5 or the registry.

    python tools/reconcile_report_html.py --report ReportHistory.html \
        --backtest reports/VIC_202601.xlsx --tag VIC
"""
from __future__ import annotations

import argparse
import re
from collections import defaultdict

import openpyxl

_COMMENT_RE = re.compile(r"([A-Za-z0-9]+)-(\d{4})#(\d+)\.(\d+)")  # TAG-MMDD#DD.N
_KEY_RE = re.compile(r"\d{4}-(\d{2})-(\d{2})#(\d+)\.(\d+)")        # YYYY-MM-DD#DD.N


def _cells(row_html: str) -> list[str]:
    return [re.sub(r"<[^>]+>", "", c).replace("&nbsp;", " ").strip()
            for c in re.findall(r"<td[^>]*>(.*?)</td>", row_html, re.S)]


def parse_report(path: str) -> list[dict]:
    """Live closed positions from an MT5 Trade History Report (HTML, UTF-16).

    The Positions table row is 14 cells: open_time, position_id, symbol, type,
    COMMENT, volume, open_price, S/L, T/P, close_time, close_price, commission,
    swap, profit.
    """
    html = open(path, encoding="utf-16").read()
    out = []
    for rh in re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S):
        c = _cells(rh)
        if len(c) != 14 or c[2] != "XAUUSD" or c[3] not in ("buy", "sell"):
            continue
        m = _COMMENT_RE.match(c[4])
        if not m:
            continue
        key = f"{m.group(2)}#{int(m.group(3)):02d}.{m.group(4)}"  # MMDD#DD.N
        out.append(dict(tag=m.group(1), key=key, entry=int(m.group(4)),
                        side=c[3], comment=c[4],
                        open_p=_f(c[6]), close_p=_f(c[10]), profit=_f(c[13]),
                        sl=_f(c[7]), tp=_f(c[8])))
    return out


def parse_backtest(path: str) -> dict[str, dict]:
    """Backtest Per-Entry Detail keyed by MMDD#DD.N."""
    ws = openpyxl.load_workbook(path, read_only=True)["Per-Entry Detail"]
    out: dict[str, dict] = {}
    for r in ws.iter_rows(min_row=2, values_only=True):
        if not r or r[0] in ("Entry Key", None):
            continue
        m = _KEY_RE.match(str(r[0]))
        if not m:
            continue
        key = f"{m.group(1)}{m.group(2)}#{int(m.group(3)):02d}.{m.group(4)}"
        out[key] = dict(side=r[4], status=r[15], exit_p=_f(r[18]),
                        pnl=_f(r[19]) if r[19] is not None else None,
                        sl=_f(r[7]), tp1=_f(r[8]), tp2=_f(r[9]), tp3=_f(r[10]))
    return out


def _f(v):
    try:
        return float(str(v).replace(",", ""))
    except (TypeError, ValueError):
        return None


def reconcile(live: list[dict], bt: dict[str, dict], tag: str | None) -> None:
    rows = [p for p in live if tag is None or p["tag"] == tag]
    if not rows:
        print(f"no live positions for tag={tag!r} (tags present: "
              f"{sorted({p['tag'] for p in live})})")
        return

    # churn: one key opened more than once
    by_key: dict[str, list[dict]] = defaultdict(list)
    for p in rows:
        by_key[p["key"]].append(p)
    churn = {k: v for k, v in by_key.items() if len(v) > 1}

    per_status = defaultdict(lambda: {"n": 0, "live": 0.0, "bt": 0.0, "slip_pts": []})
    matched = price_agree = manual = 0
    for key, plist in by_key.items():
        b = bt.get(key)
        if not b or b["exit_p"] is None or b["pnl"] is None:
            continue
        matched += 1
        best = min(plist, key=lambda x: abs((x["close_p"] or 0) - b["exit_p"]))
        live_pnl = sum(x["profit"] or 0 for x in plist)
        st = per_status[b["status"]]
        st["n"] += 1
        st["live"] += live_pnl
        st["bt"] += b["pnl"]
        dp = (best["close_p"] - b["exit_p"]) if best["close_p"] else 0.0
        signed = dp if best["side"] == "sell" else -dp  # +ve = live exited worse
        if str(b["status"]).startswith("LOCK_"):
            st["slip_pts"].append(signed)
        if abs(dp) <= 0.30:
            price_agree += 1
        else:
            near = lambda a: a is not None and abs((best["close_p"] or 0) - a) <= 0.30
            if not (near(b["sl"]) or near(b["tp1"]) or near(b["tp2"]) or near(b["tp3"])):
                manual += 1

    print(f"=== reconcile  tag={tag or 'ALL'}  live={len(rows)} positions "
          f"({len(by_key)} unique keys) ===")
    print(f"matched to backtest: {matched} | exit-price agree (<=0.30): "
          f"{price_agree}/{matched} | likely manual closes: {manual}")
    if churn:
        extra = sum(len(v) - 1 for v in churn.values())
        print(f"CHURN: {len(churn)} keys reopened -> {extra} extra fills "
              f"(e.g. {', '.join(list(churn)[:4])})")
    print(f"\n{'exit type':12}{'n':>5}{'live $':>12}{'backtest $':>12}"
          f"{'live-bt':>10}{'avg slip pt':>12}")
    tl = tb = 0.0
    for stt in sorted(per_status, key=lambda s: per_status[s]["live"] - per_status[s]["bt"]):
        d = per_status[stt]
        tl += d["live"]; tb += d["bt"]
        slip = (sum(d["slip_pts"]) / len(d["slip_pts"])) if d["slip_pts"] else 0.0
        print(f"{stt:12}{d['n']:>5}{d['live']:>12,.2f}{d['bt']:>12,.2f}"
              f"{d['live'] - d['bt']:>+10,.2f}"
              f"{(f'{slip:+.2f}' if d['slip_pts'] else '-'):>12}")
    print(f"{'TOTAL':12}{matched:>5}{tl:>12,.2f}{tb:>12,.2f}{tl - tb:>+10,.2f}")
    print("\nLOCK_* avg slip = the live give-back to calibrate "
          "SWEEP_LOCK_TP1/TP2_SLIPPAGE; SL/TP3 should be ~0.")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--report", required=True, help="MT5 Trade History Report (.html)")
    p.add_argument("--backtest", required=True, help="backtest workbook (.xlsx)")
    p.add_argument("--tag", default=None, help="filter to one strategy tag (e.g. VIC, SC24)")
    args = p.parse_args()
    live = parse_report(args.report)
    bt = parse_backtest(args.backtest)
    if args.tag is None:
        for t in sorted({x["tag"] for x in live}):
            reconcile(live, bt, t)
            print()
    else:
        reconcile(live, bt, args.tag)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
