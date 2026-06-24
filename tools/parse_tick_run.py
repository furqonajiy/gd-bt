#!/usr/bin/env python3
"""Parse a tick_backtest.py stdout dump into a machine-readable score.

tick_backtest prints, per signal, a block ending in:
    TICK  : trading=$<x> + bonus=$<b> => $<net>
    M1    : status=<S> realized=$<y>  (engine baseline; bonus excl.)
and per leg a line ``... pnl=$<p> reason=<R> (open->close)``.

This collapses one run into a JSON summary so a sweep can rank configs on the
*tick* result (the real-executor P&L) instead of the optimistic M1 baseline:

    {config, tick_pnl, m1_pnl, gap, n_signals, n_nofill, reasons, max_drawdown}

``max_drawdown`` is a proxy: per-signal tick P&L ordered by signal key (which
embeds YYYY-MM-DD#NN, so roughly chronological) accumulated into an equity curve,
max peak-to-trough as a % of the run's starting capital. Signals overlap in time,
so it is a *ranking* proxy, not the concurrent-risk DD the M1 sweep computes --
the primary rank key stays tick_pnl.
"""
from __future__ import annotations

import argparse
import json
import re
from collections import Counter

_SIG = re.compile(r"^=== (\S+) ===$", re.M)
_TICK = re.compile(r"TICK  : trading=\$(-?[\d.]+)")
_M1 = re.compile(r"M1\s+: status=\w+ realized=\$(-?[\d.]+)")
_REASON = re.compile(r"reason=(\w+)")


def parse(text: str) -> dict:
    # Split into per-signal blocks so each TICK/M1 pair is attributed to a key.
    parts = _SIG.split(text)
    per_signal: list[tuple[str, float]] = []
    tick_total = m1_total = 0.0
    n_nofill = 0
    reasons: Counter[str] = Counter()
    # parts[0] is the preamble; then (key, body) pairs.
    for i in range(1, len(parts), 2):
        key, body = parts[i], parts[i + 1]
        t = _TICK.search(body)
        m = _M1.search(body)
        tv = float(t.group(1)) if t else 0.0
        mv = float(m.group(1)) if m else 0.0
        tick_total += tv
        m1_total += mv
        if tv == 0.0:
            n_nofill += 1
        reasons.update(_REASON.findall(body))
        per_signal.append((key, tv))

    # Drawdown proxy on the key-ordered cumulative tick equity curve.
    peak = cum = 0.0
    max_dd = 0.0
    for _, pnl in sorted(per_signal, key=lambda kv: kv[0]):
        cum += pnl
        peak = max(peak, cum)
        max_dd = max(max_dd, peak - cum)

    return {
        "tick_pnl": round(tick_total, 2),
        "m1_pnl": round(m1_total, 2),
        "gap": round(m1_total - tick_total, 2),
        "n_signals": len(per_signal),
        "n_nofill": n_nofill,
        "reasons": dict(reasons),
        "max_drawdown": round(max_dd, 2),
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Summarize a tick_backtest stdout dump as JSON.")
    p.add_argument("input", help="Path to the tick_backtest stdout file.")
    p.add_argument("--config", default="", help="Config label/JSON to embed in the summary.")
    p.add_argument("--initial-capital", type=float, default=50000.0,
                   help="For the drawdown %% (default 50000).")
    args = p.parse_args(argv)

    summary = parse(open(args.input, encoding="utf-8", errors="replace").read())
    if args.config:
        summary["config"] = args.config
    cap = args.initial_capital if args.initial_capital > 0 else 50000.0
    summary["max_drawdown_pct"] = round(summary["max_drawdown"] / cap * 100, 2)
    print(json.dumps(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
