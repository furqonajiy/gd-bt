#!/usr/bin/env bash
# Phase-2 self-scalper cell: TICK-calibrate ONE Phase-1 top-100 cell.
#   Geometry IDENTICAL to r4_phase1_tc18.sh (same STRAT). feed.txt must exist
#   (generated Jan-Jun in the workflow step with the SAME generator params as Phase 1).
#   Same 5 runs as the VIC Phase-2 cell (see r4_phase2_vic.sh).
# Env: MAY OOS_TICK ENTRIES slm to tc ad mh CELL
set -o pipefail
mkdir -p out raw
SIGNALS="feed.txt"
STRAT="--entries ${ENTRIES} --entry-ladder range_to_sl --entry-sl-gap 0.7 --shared-sl false \
  --activation-delay ${ad} --pending-expiry 180 --max-hold ${mh} \
  --sl-multiplier ${slm} --final-target TP3 --runner-after-tp3 false \
  --lock-after-tp1 true --lock-after-tp2 true \
  --tp1-lock-delay-minutes 24 --tp2-lock-delay-minutes 24 \
  --profit-lock-mode tp_levels --bep-trigger-distance 3.0 --tp1-lock-fraction 0.75 \
  --tp2-lock-target TP1 --tp3-lock-target TP2 \
  --trailing-open-distance ${to} --trailing-close-distance ${tc} --trailing-close-after-stage 2 \
  --lock-tp1-exit-slippage 2.0 --lock-tp2-exit-slippage 1.0"
COMMON="--sync-ticks false --sync-charts false --max-drawdown-limit-pct 500 \
  --progress-interval-seconds 0 --minimum-lot 0.01 --lot-step 0.01"
CMAY="data/XAUUSD_M1_202605_ELEV8.csv data/XAUUSD_M1_202606_ELEV8.csv"
TICKS="data/ticks/XAUUSD_TICK_*_ELEV8.csv"
bt () { # mode start sizing bonus out
  local src="--m1-only"; [ "$1" = "tick" ] && src="--ticks $TICKS"
  python tools/backtest_hybrid.py --signals "$SIGNALS" --charts $CMAY $STRAT $COMMON $src \
    --sizing-mode $3 --risk 0.01 --lot 0.01 --initial-capital 50000 --bonus-per-closed-lot $4 \
    --start-date "$2" --output-dir "reports/${CELL}_$5" --score-json "raw/$5.json"
}
bt tick "$MAY"      fixed 0   tickmay_edge
bt tick "$MAY"      risk  3.0 tickmay_risk
bt m1   "$MAY"      fixed 0   m1may_edge
bt m1   "$MAY"      risk  3.0 m1may_risk
bt tick "$OOS_TICK" fixed 0   tick_oos
python - "$CELL" "$ENTRIES" "$slm" "$to" "$tc" "$mh" "$ad" <<'PY'
import json, sys
cell,entries,slm,to,tc,mh,ad = sys.argv[1:8]
def g(f,k): return round(float(json.load(open(f"raw/{f}.json"))[k]),2)
d={"cell":cell,"strategy":"TC18","entries":int(entries),
   "sl_multiplier":float(slm),"trailing_open":float(to),"trailing_close":float(tc),
   "max_hold":int(mh),"activation_delay":int(ad),
   "tickmay_edge":g("tickmay_edge","net_profit"),
   "tickmay_net":g("tickmay_risk","net_profit"),"tickmay_dd":g("tickmay_risk","max_drawdown_pct"),
   "m1may_edge":g("m1may_edge","net_profit"),
   "m1may_net":g("m1may_risk","net_profit"),"m1may_dd":g("m1may_risk","max_drawdown_pct"),
   "tick_oos":g("tick_oos","net_profit")}
open(f"out/{cell}.json","w").write(json.dumps(d)); print(d)
PY
