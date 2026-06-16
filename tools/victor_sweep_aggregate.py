#!/usr/bin/env python3
"""Aggregate Victor per-regime sweep shards and pick the winner on edge + OOS.

The CI sweep fans out, per regime, several ``tools/sweep_self_limit.py`` shards
(different seeds) that each append candidate rows to ``results.jsonl``. This
script merges every shard's rows for one regime, dedupes by candidate, applies
the deploy gates, and ranks the survivors on the RELIABLE forward-fit metrics:

  * EDGE  = ``fixed_no_bonus_profit``       (fixed-lot, no-bonus, no-compounding)
  * OOS   = ``oos_fixed_no_bonus_profit``   (fixed-lot edge on the held-out tail)

per ``docs/SWEEP_RUNBOOK`` -- a config only wins if it leads on BOTH, so we rank
by OOS (the best live proxy, since live is always out-of-sample) then edge, among
configs that pass DD<=gate and OOS>0. The compounded ``risk_net_profit_with_bonus``
is reported alongside but does NOT decide (it is a hypersensitive upper bound).
Every scored config already carries the locked-exit slippage overlay (2.0/1.0 via
``sweep.base_config_dict``), so the winner is chosen on REAL fills.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import sweep  # noqa: E402


def _f(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def load_rows(root: Path) -> list[dict]:
    """Every results.jsonl under root, deduped by candidate_id (last wins)."""
    by_id: dict[str, dict] = {}
    for jf in sorted(root.rglob("results.jsonl")):
        for line in jf.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            cid = row.get("candidate_id") or sweep._json_hash(row.get("config") or {})
            by_id[cid] = row
    return list(by_id.values())


def survivors(rows: list[dict], dd_gate: float) -> list[dict]:
    out = []
    for r in rows:
        if r.get("error"):
            continue
        dd = r.get("concurrent_risk_max_dd_pct")
        oos = r.get("oos_fixed_no_bonus_profit")
        if dd is None or _f(dd) > dd_gate:
            continue
        if oos is None or _f(oos) <= 0.0:
            continue
        out.append(r)
    out.sort(key=lambda r: (_f(r.get("oos_fixed_no_bonus_profit")),
                            _f(r.get("fixed_no_bonus_profit"))), reverse=True)
    return out


def _cfg_brief(c: dict) -> str:
    return (f"e{c.get('entry_count')} slm{c.get('sl_multiplier')} "
            f"gap{c.get('entry_sl_gap')} d{c.get('tp1_lock_delay_minutes')} "
            f"hold{c.get('max_hold_minutes')} tgt{c.get('final_target')}")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--root", required=True, help="dir tree holding the shards' results.jsonl")
    p.add_argument("--regime", required=True)
    p.add_argument("--out-dir", required=True)
    p.add_argument("--dd-gate", type=float, default=40.0)
    p.add_argument("--top-n", type=int, default=25)
    args = p.parse_args(argv)

    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    rows = load_rows(Path(args.root))
    surv = survivors(rows, args.dd_gate)

    # Leaderboard workbook/csv over ALL rows (sweep's own writer), plus our
    # edge+OOS ranked summary + the winning config.
    sweep.write_leaderboards(rows, out, args.top_n)

    summary = out / f"WINNER_{args.regime}.md"
    lines = [f"# Victor sweep winner — {args.regime}", ""]
    lines.append(f"- candidates scored: **{len(rows)}** | "
                 f"DD<={args.dd_gate:.0f}% & OOS>0 survivors: **{len(surv)}**")
    lines.append("- ranked by **OOS** (held-out tail) then **edge** (fixed-lot, "
                 "slippage-aware); compounded shown for reference only.")
    lines.append("")
    if not surv:
        lines.append("**No DD-passing, OOS>0 config found.**")
        summary.write_text("\n".join(lines) + "\n")
        print(f"[aggregate {args.regime}] no survivors ({len(rows)} scored)")
        return 0

    best = surv[0]
    bc = best.get("config") or {}
    json.dump(bc, open(out / f"BEST_{args.regime}.json", "w"), indent=2, sort_keys=True)

    lines.append("| # | edge $ | OOS $ | DD % | compounded+bonus $ | config |")
    lines.append("|---|---:|---:|---:|---:|---|")
    for i, r in enumerate(surv[:args.top_n], 1):
        lines.append(
            f"| {i} | {_f(r.get('fixed_no_bonus_profit')):,.0f} | "
            f"{_f(r.get('oos_fixed_no_bonus_profit')):,.0f} | "
            f"{_f(r.get('concurrent_risk_max_dd_pct')):.1f} | "
            f"{_f(r.get('risk_net_profit_with_bonus')):,.0f} | "
            f"`{_cfg_brief(r.get('config') or {})}` |")
    lines.append("")
    lines.append(f"**WINNER:** `{_cfg_brief(bc)}` — edge "
                 f"${_f(best.get('fixed_no_bonus_profit')):,.0f}, OOS "
                 f"${_f(best.get('oos_fixed_no_bonus_profit')):,.0f}, DD "
                 f"{_f(best.get('concurrent_risk_max_dd_pct')):.1f}%. "
                 f"Full config: `BEST_{args.regime}.json`.")
    summary.write_text("\n".join(lines) + "\n")
    print(f"[aggregate {args.regime}] winner {_cfg_brief(bc)} | "
          f"edge ${_f(best.get('fixed_no_bonus_profit')):,.0f} "
          f"OOS ${_f(best.get('oos_fixed_no_bonus_profit')):,.0f} "
          f"DD {_f(best.get('concurrent_risk_max_dd_pct')):.1f}% | "
          f"survivors {len(surv)}/{len(rows)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
