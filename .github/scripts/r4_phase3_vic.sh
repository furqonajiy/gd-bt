#!/usr/bin/env bash
# Phase-3 VIC cell: risk-ladder at 1/2/3/5% for ONE Phase-2 top-10 cell.
# Jan-Jun 2026 tick-hybrid (real ticks May-Jun, M1 Jan-Apr); OOS Jun16-26 tick.
# Emits out/{CELL}_r{01,02,03,05}.json (one per risk level; jan_edge + tick_oos shared).
# Env: JAN_START OOS_TICK CELL ENTRIES to tc stage slm ft ad mh
set -o pipefail
mkdir -p out raw
SIGNALS="victor_signals.txt"
STRAT="--entries ${ENTRIES} --entry-ladder range_to_sl --entry-sl-gap 0.5 \
  --activation-delay ${ad} --pending-expiry 180 --max-hold ${mh} \
  --sl-multiplier ${slm} --final-target ${ft} --runner-after-tp3 false \
  --lock-after-tp1 true --lock-after-tp2 true \
  --tp1-lock-delay-minutes 12 --tp2-lock-delay-minutes 2 \
  --profit-lock-mode tp_levels --bep-trigger-distance 3.0 --tp1-lock-fraction 0.5 \
  --tp2-lock-target TP1 --tp3-lock-target TP2 \
  --trailing-open-distance ${to} --trailing-close-distance ${tc} --trailing-close-after-stage ${stage} \
  --lock-tp1-exit-slippage 2.0 --lock-tp2-exit-slippage 1.0"
COMMON="--sync-ticks false --sync-charts false --max-drawdown-limit-pct 500 \
  --progress-interval-seconds 0 --minimum-lot 0.01 --maximum-lot 500 --lot-step 0.01"
CJAN="data/XAUUSD_M1_202601_ELEV8.csv data/XAUUSD_M1_202602_ELEV8.csv \
  data/XAUUSD_M1_202603_ELEV8.csv data/XAUUSD_M1_202604_ELEV8.csv \
  data/XAUUSD_M1_202605_ELEV8.csv data/XAUUSD_M1_202606_ELEV8.csv"
TICKS="data/ticks/XAUUSD_TICK_*_ELEV8.csv"

bt () { # label start sizing risk bonus
  python tools/backtest_hybrid.py --signals "$SIGNALS" --charts $CJAN $STRAT $COMMON \
    --ticks $TICKS --sizing-mode $3 --risk $4 --lot 0.01 --initial-capital 50000 \
    --bonus-per-closed-lot $5 --start-date "$2" \
    --output-dir "reports/${CELL}_$1" --score-json "raw/$1.json"
}

# Risk-invariant fixed-lot edge (run once)
bt jan_edge "$JAN_START" fixed 0.01 0
# Risk levels: 1%, 2%, 3%, 5%
bt jan_r01  "$JAN_START" risk  0.01 3.0
bt jan_r02  "$JAN_START" risk  0.02 3.0
bt jan_r03  "$JAN_START" risk  0.03 3.0
bt jan_r05  "$JAN_START" risk  0.05 3.0
# Real tick OOS (fixed lot, separate from M1 OOS)
bt tick_oos "$OOS_TICK"  fixed 0.01 0

python - "$CELL" "$ENTRIES" "$to" "$tc" "$stage" "$slm" "$ft" "$ad" "$mh" <<'PY'
import json, sys
cell, entries, to, tc, stage, slm, ft, ad, mh = sys.argv[1:10]
def g(f, k): return round(float(json.load(open(f"raw/{f}.json"))[k]), 2)
jan_edge = g("jan_edge", "net_profit")
tick_oos = g("tick_oos", "net_profit")
base = {
    "cell": cell, "strategy": "VIC", "entries": int(entries),
    "trailing_open": float(to), "trailing_close": float(tc), "trail_after_stage": int(stage),
    "sl_multiplier": float(slm), "final_target": ft,
    "activation_delay": int(ad), "max_hold": int(mh),
    "jan_edge": jan_edge, "tick_oos": tick_oos,
}
for r, rtag in [(0.01, "jan_r01"), (0.02, "jan_r02"), (0.03, "jan_r03"), (0.05, "jan_r05")]:
    d = dict(base)
    d["risk_pct"] = r
    d["jan_net"] = g(rtag, "net_profit")
    d["jan_dd"] = g(rtag, "max_drawdown_pct")
    fname = "out/{}_{}.json".format(cell, rtag.replace("jan_", ""))
    open(fname, "w").write(json.dumps(d))
    print(d)
PY
