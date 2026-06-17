#!/usr/bin/env bash
# Aggregate a self-scalper entry-feature sweep for one regime: run
# victor_sweep_aggregate per variant, then print the per-variant winners table
# and the winner-vs-base verdict (a variant wins only if it beats the unfiltered
# 'base' feed on BOTH edge AND OOS at DD<=40%).
#
# Usage: agg_entry_feature.sh <REGIME> [PREFIX]
#   PREFIX defaults to "selffeat" (the entry-feature sweep). The R:R sweep
#   passes "selfrr" so the two share this aggregator without colliding; both
#   key off _artifacts/<PREFIX>-<REGIME>-* and use the identical edge+OOS gate.
set -uo pipefail
REG="$1"
PREFIX="${2:-selffeat}"

for d in _artifacts/"${PREFIX}"-"${REG}"-*; do
  [ -d "$d" ] || continue
  v="${d#_artifacts/${PREFIX}-${REG}-}"
  python tools/victor_sweep_aggregate.py --root "$d" --regime "SCF_${REG}_${v}" \
    --out-dir "results/${v}" --dd-gate 40 --top-n 10 || true
done

echo "==================== ${REG} PER-VARIANT WINNERS ===================="
python - << 'PY'
import glob, csv, os
rows = []
for csvp in glob.glob("results/*/leaderboard.csv"):
    variant = os.path.basename(os.path.dirname(csvp))
    best = None
    for r in csv.DictReader(open(csvp)):
        def f(x):
            try: return float(x)
            except: return None
        dd = f(r["concurrent_risk_max_dd_pct"]); oos = f(r["oos_fixed_no_bonus_profit"]); edge = f(r["fixed_no_bonus_profit"])
        if dd is None or oos is None or edge is None: continue
        if abs(dd) <= 40 and oos > 0:
            key = f(r["fixed_with_bonus_profit"]) or edge
            if best is None or key > best[0]:
                best = (key, edge, oos, dd, r["cfg_entry_count"], r["cfg_sl_multiplier"],
                        r["cfg_max_hold_minutes"], r["cfg_tp1_lock_delay_minutes"])
    if best: rows.append((variant, *best))
rows.sort(key=lambda x: -(x[2]))  # by edge
print(f"{'variant':12}{'edge$':>10}{'OOS$':>9}{'DD%':>7}  e/slm/hold/d")
for v, key, edge, oos, dd, e, slm, hold, d in rows:
    print(f"{v:12}{edge:>10,.0f}{oos:>9,.0f}{dd:>7.1f}  e{e}/slm{slm}/h{hold}/d{d}")
base = next((r for r in rows if r[0] == "base"), None)
if base and rows:
    be, bo = base[2], base[3]
    winners = [r for r in rows if r[2] > be and r[3] > bo]
    print(f"\nbase: edge=${be:,.0f} OOS=${bo:,.0f}")
    if winners:
        winners.sort(key=lambda x: (x[3], x[2]))  # OOS then edge
        g = winners[-1]
        print(f"BEATS BASE on edge AND OOS: {', '.join(w[0] for w in winners)}")
        print(f"GLOBAL WINNER: variant={g[0]} edge=${g[2]:,.0f} OOS=${g[3]:,.0f} DD={g[4]:.1f}%")
    else:
        print("NO entry-feature variant beats base on BOTH edge AND OOS -> keep base (unfiltered).")
else:
    print("(no base survivor to compare against)")
PY
