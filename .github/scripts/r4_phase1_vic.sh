#!/usr/bin/env bash
# Phase-1 VIC cell (M1-only, Jan-Jun). Env: JAN OOS ENTRIES to tc stage slm ft ad mh
set -o pipefail
mkdir -p out raw
CELL="vcal_e${ENTRIES}_to${to//./}_tc${tc//./}_s${stage}_slm${slm//./}_${ft}_ad${ad}_mh${mh}"
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
  --progress-interval-seconds 0 --minimum-lot 0.01 --lot-step 0.01"
CJAN="data/XAUUSD_M1_202601_ELEV8.csv data/XAUUSD_M1_202602_ELEV8.csv data/XAUUSD_M1_202603_ELEV8.csv data/XAUUSD_M1_202604_ELEV8.csv data/XAUUSD_M1_202605_ELEV8.csv data/XAUUSD_M1_202606_ELEV8.csv"
bt () { # start sizing bonus out
  python tools/backtest_hybrid.py --signals "$SIGNALS" --charts $CJAN $STRAT $COMMON --m1-only \
    --sizing-mode $2 --risk 0.01 --lot 0.01 --initial-capital 50000 --bonus-per-closed-lot $3 \
    --start-date "$1" --output-dir "reports/${CELL}_$4" --score-json "raw/$4.json"
}
bt "$JAN" risk  3.0 jan_risk
bt "$JAN" fixed 0   jan_edge
bt "$OOS" fixed 0   jan_oos
python - "$CELL" "$ENTRIES" "$to" "$tc" "$stage" "$slm" "$ft" "$ad" "$mh" <<'PY'
import json, sys
cell,entries,to,tc,stage,slm,ft,ad,mh = sys.argv[1:10]
def g(f,k): return round(float(json.load(open(f"raw/{f}.json"))[k]),2)
d={"cell":cell,"strategy":"VIC","entries":int(entries),
   "trailing_open":float(to),"trailing_close":float(tc),"trail_after_stage":int(stage),
   "sl_multiplier":float(slm),"final_target":ft,"activation_delay":int(ad),"max_hold":int(mh),
   "jan_net":g("jan_risk","net_profit"),"jan_dd":g("jan_risk","max_drawdown_pct"),
   "jan_edge":g("jan_edge","net_profit"),"jan_oos":g("jan_oos","net_profit"),
   "jan_signals":int(json.load(open("raw/jan_risk.json"))["signals_included"])}
open(f"out/{cell}.json","w").write(json.dumps(d)); print(d)
PY
