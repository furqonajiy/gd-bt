#!/usr/bin/env python3
"""Aggregate Victor per-regime sweep shards and pick the most-profitable winner.

The CI sweep fans out, per regime, many ``tools/sweep_self_limit.py`` shards
(different seeds) that each append candidate rows to ``results.jsonl``. This
script merges every shard's rows for one regime, dedupes by candidate, applies
the hard gates, and ranks the survivors on the deploy OBJECTIVE:

  * OBJECTIVE = ``fixed_with_bonus_profit`` = fixed-lot edge + the $3/closed-lot
    bonus -- so a config that closes MORE signals scores higher (the bonus is
    real cash), on the reliable fixed-lot basis (no compounding mirage).
  * GATES = DD<=gate (concurrent risk DD) AND OOS>0 (held-out tail, overfit
    guard) -- both hard. Tiebreak: OOS then raw edge.

The compounded ``risk_net_profit_with_bonus`` is reported but does NOT decide (it
is a hypersensitive upper bound). Every scored config carries the locked-exit
slippage overlay (2.0/1.0 via ``sweep.base_config_dict``) AND, with
``--signal-policy``, the R:R / ATR-SL dimensions -- so the winner is chosen on
REAL fills across the full take-all-vs-selective + Victor-TP-vs-our-geometry space.
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


def _objective(r: dict) -> float:
    """Most-profitable = fixed-lot edge + the $3/closed-lot bonus (so more closed
    signals lifts the score), on the reliable fixed-lot basis (no compounding
    mirage). Falls back to edge if the bonus metric is absent."""
    v = r.get("fixed_with_bonus_profit")
    return _f(v) if v is not None else _f(r.get("fixed_no_bonus_profit"))


# Headline metrics to surface the per-objective best of, so the winner under
# EACH lens can be compared ("net profit's not bad -> execute"). All share the
# same hard gates (DD<=gate & OOS>0); they only differ in what they MAXIMIZE.
OBJECTIVES = [
    ("net+bonus (compounded)", lambda r: _f(r.get("risk_net_profit_with_bonus"))),
    ("edge+bonus (fixed)", _objective),
    ("edge (fixed)", lambda r: _f(r.get("fixed_no_bonus_profit"))),
    ("OOS (held-out)", lambda r: _f(r.get("oos_fixed_no_bonus_profit"))),
]


def best_per_objective(surv: list[dict]) -> list[tuple[str, dict]]:
    return [(name, max(surv, key=fn)) for name, fn in OBJECTIVES] if surv else []


def survivors(rows: list[dict], dd_gate: float) -> list[dict]:
    out = []
    for r in rows:
        if r.get("error"):
            continue
        dd = r.get("concurrent_risk_max_dd_pct")
        oos = r.get("oos_fixed_no_bonus_profit")
        if dd is None or _f(dd) > dd_gate:
            continue
        if oos is None or _f(oos) <= 0.0:   # OOS>0 overfit guard (hard gate)
            continue
        out.append(r)
    # Rank on edge+bonus (the deploy objective), tiebreak OOS then raw edge.
    out.sort(key=lambda r: (_objective(r), _f(r.get("oos_fixed_no_bonus_profit")),
                            _f(r.get("fixed_no_bonus_profit"))), reverse=True)
    return out


def _cfg_brief(c: dict) -> str:
    base = (f"e{c.get('entry_count')} slm{c.get('sl_multiplier')} "
            f"gap{c.get('entry_sl_gap')} d{c.get('tp1_lock_delay_minutes')} "
            f"hold{c.get('max_hold_minutes')} tgt{c.get('final_target')}")
    pol = []
    if c.get("sl_source") == "atr":
        pol.append(f"ATRsl{c.get('atr_sl_mult')}p{c.get('atr_period')}")
    if _f(c.get("signal_min_rr")) > 0:
        pol.append(f"minRR{c.get('signal_min_rr')}({c.get('signal_rr_reference','nominal')[:3]})")
    if _f(c.get("rewrite_tp3_rr")) > 0:
        pol.append(f"rwRR{c.get('rewrite_tp1_rr')}/{c.get('rewrite_tp2_rr')}/{c.get('rewrite_tp3_rr')}")
    return base + ("  [" + " ".join(pol) + "]" if pol else "  [Victor TP/SL as-is]")


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
    lines.append("- ranked by **edge + $3/lot bonus** (fixed-lot, slippage-aware; more "
                 "closed signals lifts it), guarded by OOS>0; compounded shown for reference only.")
    lines.append("")
    if not surv:
        lines.append("**No DD-passing, OOS>0 config found.**")
        summary.write_text("\n".join(lines) + "\n")
        print(f"[aggregate {args.regime}] no survivors ({len(rows)} scored)")
        return 0

    best = surv[0]
    bc = best.get("config") or {}
    json.dump(bc, open(out / f"BEST_{args.regime}.json", "w"), indent=2, sort_keys=True)

    # Best config under EACH objective, side by side, so the user can compare
    # (e.g. accept a slightly-lower-edge config because its net profit is great).
    lines.append("## Best config per objective (all gated DD<=%.0f%% & OOS>0)" % args.dd_gate)
    lines.append("")
    lines.append("| MAX of | edge $ | edge+bonus $ | OOS $ | net+bonus $ | DD % | config |")
    lines.append("|---|---:|---:|---:|---:|---:|---|")
    seen_obj = {}
    for name, r in best_per_objective(surv):
        seen_obj[name] = r
        lines.append(
            f"| **{name}** | {_f(r.get('fixed_no_bonus_profit')):,.0f} | "
            f"{_objective(r):,.0f} | {_f(r.get('oos_fixed_no_bonus_profit')):,.0f} | "
            f"{_f(r.get('risk_net_profit_with_bonus')):,.0f} | "
            f"{_f(r.get('concurrent_risk_max_dd_pct')):.1f} | "
            f"`{_cfg_brief(r.get('config') or {})}` |")
        # dump each objective's deployable config
        safe = name.split()[0].replace("+", "_")
        json.dump(r.get("config") or {},
                  open(out / f"BEST_{args.regime}_{safe}.json", "w"),
                  indent=2, sort_keys=True)
    lines.append("")
    lines.append("## Full leaderboard (ranked by edge + $3/lot bonus)")
    lines.append("")
    lines.append("| # | edge+bonus $ | edge $ | bonus $ | OOS $ | DD % | compounded $ | config |")
    lines.append("|---|---:|---:|---:|---:|---:|---:|---|")
    for i, r in enumerate(surv[:args.top_n], 1):
        lines.append(
            f"| {i} | {_objective(r):,.0f} | "
            f"{_f(r.get('fixed_no_bonus_profit')):,.0f} | "
            f"{_f(r.get('bonus_contribution')):,.0f} | "
            f"{_f(r.get('oos_fixed_no_bonus_profit')):,.0f} | "
            f"{_f(r.get('concurrent_risk_max_dd_pct')):.1f} | "
            f"{_f(r.get('risk_net_profit_with_bonus')):,.0f} | "
            f"`{_cfg_brief(r.get('config') or {})}` |")
    lines.append("")
    lines.append(f"**WINNER:** `{_cfg_brief(bc)}` — edge+bonus "
                 f"${_objective(best):,.0f} (edge ${_f(best.get('fixed_no_bonus_profit')):,.0f} "
                 f"+ bonus ${_f(best.get('bonus_contribution')):,.0f}), OOS "
                 f"${_f(best.get('oos_fixed_no_bonus_profit')):,.0f}, DD "
                 f"{_f(best.get('concurrent_risk_max_dd_pct')):.1f}%. "
                 f"Full config: `BEST_{args.regime}.json`.")
    summary.write_text("\n".join(lines) + "\n")
    print(f"[aggregate {args.regime}] winner {_cfg_brief(bc)} | "
          f"edge+bonus ${_objective(best):,.0f} "
          f"OOS ${_f(best.get('oos_fixed_no_bonus_profit')):,.0f} "
          f"DD {_f(best.get('concurrent_risk_max_dd_pct')):.1f}% | "
          f"survivors {len(surv)}/{len(rows)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
