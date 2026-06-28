#!/usr/bin/env bash
# Phase-2 VIC cell: TICK-calibrate ONE Phase-1 top-100 cell.
#   Geometry is IDENTICAL to r4_phase1_vic.sh (same STRAT) so the factor is honest.
#   Runs (May-Jun overlap is the only real-tick window):
#     1. TICK May-Jun  fixed  -> tickmay_edge   (real fills, slippage inert on tick)
#     2. TICK May-Jun  risk   -> tickmay_dd/net
#     3. M1   May-Jun  fixed  -> m1may_edge     (slippage 2.0/1.0 applied)
#     4. M1   May-Jun  risk   -> m1may_dd
#     5. TICK Jun16-26 fixed  -> tick_oos       (REAL tick OOS, separate from M1 OOS)
#   factor_edge = tickmay_edge / m1may_edge ; the leaderboard joins Phase-1's M1
#   jan_edge/jan_oos/jan_dd by cell and computes est_jan_* = jan_* * factor.
# Env: MAY OOS_TICK ENTRIES to tc stage slm ft ad mh CELL
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
python - "$CELL" "$ENTRIES" "$to" "$tc" "$stage" "$slm" "$ft" "$ad" "$mh" <<'PY'
import json, sys
cell,entries,to,tc,stage,slm,ft,ad,mh = sys.argv[1:10]
def g(f,k): return round(float(json.load(open(f"raw/{f}.json"))[k]),2)
d={"cell":cell,"strategy":"VIC","entries":int(entries),
   "trailing_open":float(to),"trailing_close":float(tc),"trail_after_stage":int(stage),
   "sl_multiplier":float(slm),"final_target":ft,"activation_delay":int(ad),"max_hold":int(mh),
   "tickmay_edge":g("tickmay_edge","net_profit"),
   "tickmay_net":g("tickmay_risk","net_profit"),"tickmay_dd":g("tickmay_risk","max_drawdown_pct"),
   "m1may_edge":g("m1may_edge","net_profit"),
   "m1may_net":g("m1may_risk","net_profit"),"m1may_dd":g("m1may_risk","max_drawdown_pct"),
   "tick_oos":g("tick_oos","net_profit")}
open(f"out/{cell}.json","w").write(json.dumps(d)); print(d)
PY
