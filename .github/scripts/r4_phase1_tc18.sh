#!/usr/bin/env bash
# Phase-1 self-scalper cell (M1-only, Jan-Jun). Feed.txt must exist. Env: JAN OOS ENTRIES slm to tc ad mh
set -o pipefail
mkdir -p out raw
CELL="tcal_e${ENTRIES}_slm${slm//./}_to${to//./}_tc${tc//./}_mh${mh}_ad${ad}"
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
CJAN="data/XAUUSD_M1_202601_ELEV8.csv data/XAUUSD_M1_202602_ELEV8.csv data/XAUUSD_M1_202603_ELEV8.csv data/XAUUSD_M1_202604_ELEV8.csv data/XAUUSD_M1_202605_ELEV8.csv data/XAUUSD_M1_202606_ELEV8.csv"
bt () {
  python tools/backtest_hybrid.py --signals "$SIGNALS" --charts $CJAN $STRAT $COMMON --m1-only \
    --sizing-mode $2 --risk 0.01 --lot 0.01 --initial-capital 50000 --bonus-per-closed-lot $3 \
    --start-date "$1" --output-dir "reports/${CELL}_$4" --score-json "raw/$4.json"
}
bt "$JAN" risk  3.0 jan_risk
bt "$JAN" fixed 0   jan_edge
bt "$OOS" fixed 0   jan_oos
python - "$CELL" "$ENTRIES" "$slm" "$to" "$tc" "$mh" "$ad" <<'PY'
import json, sys
cell,entries,slm,to,tc,mh,ad = sys.argv[1:8]
def g(f,k): return round(float(json.load(open(f"raw/{f}.json"))[k]),2)
d={"cell":cell,"strategy":"TC18","entries":int(entries),
   "sl_multiplier":float(slm),"trailing_open":float(to),"trailing_close":float(tc),
   "max_hold":int(mh),"activation_delay":int(ad),
   "jan_net":g("jan_risk","net_profit"),"jan_dd":g("jan_risk","max_drawdown_pct"),
   "jan_edge":g("jan_edge","net_profit"),"jan_oos":g("jan_oos","net_profit"),
   "jan_signals":int(json.load(open("raw/jan_risk.json"))["signals_included"])}
open(f"out/{cell}.json","w").write(json.dumps(d)); print(d)
PY
