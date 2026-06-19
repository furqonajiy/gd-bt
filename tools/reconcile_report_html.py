#!/usr/bin/env python3
"""Reconcile an MT5 *Trade History Report* (HTML) against a backtest workbook.

This is the method used repeatedly in the live<->backtest sessions, captured as
one command. Unlike ``tools/parity_reconcile.py`` (which needs the executor's
forensic/notification JSONL), this reads the **HTML report you export from the
MT5 terminal** (History tab -> right-click -> Report -> HTML) -- the thing that's
easy to get -- and the per-strategy backtest workbook.

It matches each live closed position to its backtest per-entry leg by the order
**comment** ``[TAG-]MMDD#DD.N`` <-> backtest key ``YYYY-MM-DD#DD.N`` and reports,
per strategy tag:

  * the AGGREGATE bottom line -- live realized $ (all fills) vs the backtest at
    the SAME fixed lot, recomputed from entry/exit PRICES so a risk-sized /
    compounded workbook reconciles in dollars without re-running it at fixed lot;
  * REOPEN CHURN -- keys opened more than once (the reopen pass round-tripping a
    leg the backtest holds once) and what those extra fills cost;
  * per exit type (SL / TP3 / LOCK_TP1 / LOCK_TP2 / TIME_EXIT) on the CLEAN
    single-fill keys: live $ vs backtest @lot $, entry- and exit-price agreement,
    and the LOCK_* give-back in points (calibrates the lock slippage knobs);
  * the biggest divergence, so the leak is named not buried.

Backtest dollars are computed at the live lot from prices: a XAUUSD move of $1.00
is $1.00 per 0.01 lot, so ``--usd-per-point`` defaults to 1.0 (dollars per 1.00
price move at the live fixed lot). SL/TP3 prices should match live to the cent;
LOCK_* exits are where live gives back slippage.

For a clean reconcile the backtest workbook MUST cover the live window -- its
chart data has to extend PAST the last live trade, or the live signals won't
exist in the backtest and nothing matches. Regenerate the backtest right after
you export the HTML.

    python tools/reconcile_report_html.py --report ReportHistory.html \
        --backtest reports/SQZ6_202606.xlsx --tag SQZ6

Pair one --backtest per --tag (in order) to reconcile several strategies from a
single report in one run:

    python tools/reconcile_report_html.py --report ReportHistory.html \
        --tag SC24 --backtest SC24E5_202606.xlsx \
        --tag SQZ6 --backtest SQZ6_202606.xlsx \
        --tag VIC  --backtest VIC_202606.xlsx

Omit --tag to reconcile every tag found in the report against --backtest[0].
Read-only; never touches MT5.
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


def _f(v):
    try:
        return float(str(v).replace(",", ""))
    except (TypeError, ValueError):
        return None


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
                        side=c[3], comment=c[4], open_t=c[0], close_t=c[9],
                        open_p=_f(c[6]), close_p=_f(c[10]), profit=_f(c[13]),
                        sl=_f(c[7]), tp=_f(c[8])))
    return out


def parse_backtest(path: str) -> dict[str, dict]:
    """Backtest Per-Entry Detail keyed by MMDD#DD.N (one leg per key)."""
    ws = openpyxl.load_workbook(path, read_only=True)["Per-Entry Detail"]
    out: dict[str, dict] = {}
    for r in ws.iter_rows(min_row=2, values_only=True):
        if not r or r[0] in ("Entry Key", None):
            continue
        m = _KEY_RE.match(str(r[0]))
        if not m:
            continue
        key = f"{m.group(1)}{m.group(2)}#{int(m.group(3)):02d}.{m.group(4)}"
        out[key] = dict(side=r[4], status=r[15], entry_p=_f(r[12]),
                        exit_p=_f(r[18]),
                        pnl=_f(r[19]) if r[19] is not None else None,
                        sl=_f(r[7]), tp1=_f(r[8]), tp2=_f(r[9]), tp3=_f(r[10]))
    return out


def _bt_fixed(b: dict, usd_per_point: float) -> float | None:
    """Backtest leg P&L at the live fixed lot, from prices (lot-independent)."""
    if b["entry_p"] is None or b["exit_p"] is None:
        return None
    sign = 1.0 if b["side"] == "buy" else -1.0
    return (b["exit_p"] - b["entry_p"]) * sign * usd_per_point


def reconcile(live: list[dict], bt: dict[str, dict], tag: str | None,
              usd_per_point: float) -> None:
    rows = [p for p in live if tag is None or p["tag"] == tag]
    if not rows:
        print(f"no live positions for tag={tag!r} (tags present: "
              f"{sorted({p['tag'] for p in live})})")
        return

    by_key: dict[str, list[dict]] = defaultdict(list)
    for p in rows:
        by_key[p["key"]].append(p)
    for plist in by_key.values():
        plist.sort(key=lambda x: x["open_t"])

    matched = [k for k in by_key
               if k in bt and _bt_fixed(bt[k], usd_per_point) is not None]
    churn = {k: v for k, v in by_key.items() if len(v) > 1}
    extra_fills = sum(len(v) - 1 for v in churn.values())
    extra_pnl = sum(x["profit"] or 0 for v in churn.values() for x in v[1:])

    live_all = sum(x["profit"] or 0 for v in by_key.values() for x in v)
    bt_matched = sum(_bt_fixed(bt[k], usd_per_point) for k in matched)

    # Per-exit-type on CLEAN single-fill keys: one live fill <-> one bt leg, so
    # P&L, entry/exit agreement and slippage are apples-to-apples (churned keys
    # book several round-trips against the backtest's single leg).
    per = defaultdict(lambda: {"n": 0, "live": 0.0, "bt": 0.0,
                               "ent_ok": 0, "exit_ok": 0, "slip": []})
    clean_live = clean_bt = 0.0
    for k in matched:
        if len(by_key[k]) != 1:
            continue
        b, fl = bt[k], by_key[k][0]
        btfix = _bt_fixed(b, usd_per_point)
        d = per[b["status"]]
        d["n"] += 1
        d["live"] += fl["profit"] or 0
        d["bt"] += btfix
        clean_live += fl["profit"] or 0
        clean_bt += btfix
        if b["entry_p"] is not None and abs((fl["open_p"] or 0) - b["entry_p"]) <= 0.30:
            d["ent_ok"] += 1
        dp = (fl["close_p"] or 0) - b["exit_p"]
        if abs(dp) <= 0.30:
            d["exit_ok"] += 1
        if str(b["status"]).startswith("LOCK_"):
            d["slip"].append(dp if fl["side"] == "sell" else -dp)

    print(f"=== {tag or 'ALL'}  |  live {len(rows)} fills / {len(by_key)} keys "
          f"|  matched {len(matched)} | unmatched {len(by_key) - len(matched)} ===")
    print(f"AGGREGATE: live realized ${live_all:+,.2f} (all fills)  vs  "
          f"backtest ${bt_matched:+,.2f} (@live lot, matched legs)  ->  "
          f"gap ${live_all - bt_matched:+,.2f}")
    if churn:
        print(f"CHURN: {len(churn)} keys reopened -> {extra_fills} extra fills, "
              f"extra-fill P&L ${extra_pnl:+,.2f} "
              f"(e.g. {', '.join(list(churn)[:4])})")
    print("\nclean single-fill keys by exit type "
          "(live vs backtest @lot, price agreement, lock slip):")
    print(f"{'exit type':12}{'n':>5}{'live $':>11}{'bt @lot $':>11}"
          f"{'live-bt':>10}{'entry ok':>10}{'exit ok':>10}{'slip pt':>9}")
    worst = (None, 0.0)
    for stt in sorted(per, key=lambda s: per[s]["live"] - per[s]["bt"]):
        d = per[stt]
        diff = d["live"] - d["bt"]
        if abs(diff) > abs(worst[1]):
            worst = (stt, diff)
        slip = (sum(d["slip"]) / len(d["slip"])) if d["slip"] else None
        print(f"{str(stt):12}{d['n']:>5}{d['live']:>11,.2f}{d['bt']:>11,.2f}"
              f"{diff:>+10,.2f}{d['ent_ok']:>6}/{d['n']:<3}{d['exit_ok']:>6}/{d['n']:<3}"
              f"{(f'{slip:+.2f}' if slip is not None else '-'):>9}")
    print(f"{'TOTAL clean':12}{sum(d['n'] for d in per.values()):>5}"
          f"{clean_live:>11,.2f}{clean_bt:>11,.2f}{clean_live - clean_bt:>+10,.2f}")
    if worst[0] is not None:
        print(f"\nBIGGEST DIVERGENCE: {worst[0]} legs, live-vs-backtest "
              f"${worst[1]:+,.2f}. LOCK_* slip = live give-back to calibrate the "
              f"lock-slippage knobs; SL/TP3 should be ~0.")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--report", required=True, help="MT5 Trade History Report (.html)")
    p.add_argument("--backtest", required=True, action="append",
                   help="backtest workbook (.xlsx); repeat for multiple tags")
    p.add_argument("--tag", default=None, action="append",
                   help="strategy tag (e.g. SQZ6); repeat to pair with --backtest "
                        "in order. Omit to reconcile every tag against --backtest[0].")
    p.add_argument("--usd-per-point", type=float, default=1.0,
                   help="dollars per 1.00 price move at the live fixed lot "
                        "(XAUUSD 0.01 lot = 1.0; default 1.0)")
    args = p.parse_args()

    live = parse_report(args.report)
    tags = args.tag
    backtests = args.backtest

    if tags and len(tags) == len(backtests):
        for t, bpath in zip(tags, backtests):
            reconcile(live, parse_backtest(bpath), t, args.usd_per_point)
            print()
    elif tags and len(backtests) == 1:
        bt = parse_backtest(backtests[0])
        for t in tags:
            reconcile(live, bt, t, args.usd_per_point)
            print()
    else:
        bt = parse_backtest(backtests[0])
        for t in sorted({x["tag"] for x in live}):
            reconcile(live, bt, t, args.usd_per_point)
            print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
