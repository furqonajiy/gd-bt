#!/usr/bin/env bash
# Aggregate a self-scalper entry-feature sweep for one regime: run
# victor_sweep_aggregate per variant, then print the per-variant winners table.
# A variant beats the unfiltered 'base' feed only when it improves edge+bonus,
# raw edge, and OOS at DD<=40%.
#
# Usage: agg_entry_feature.sh <REGIME> [PREFIX]
#   PREFIX defaults to "selffeat" (the entry-feature sweep). The R:R sweep
#   passes "selfrr" so the two share this aggregator without colliding; both
#   key off _artifacts/<PREFIX>-<REGIME>-* and use the identical variant gate.
set -uo pipefail
REG="$1"
PREFIX="${2:-selffeat}"

for d in _artifacts/"${PREFIX}"-"${REG}"-*; do
  [ -d "$d" ] || continue
  v="${d#_artifacts/${PREFIX}-${REG}-}"
  python tools/victor_sweep_aggregate.py --root "$d" --regime "SCF_${REG}_${v}" \
    --out-dir "results/${v}" --dd-gate 40 --top-n 10 || true
done

python tools/entry_feature_variant_report.py \
  --results-dir results \
  --dd-gate 40 \
  --title "${REG} PER-VARIANT WINNERS"
