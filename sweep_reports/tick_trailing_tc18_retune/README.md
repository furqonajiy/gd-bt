# TC18 re-tune trailing TICK sweep -- beat TC18

72 cells on committed May+June ELEV8 ticks. Metrics per cell: **net compound + DD** (risk 1% / $50k base, $3/lot bonus), **edge** (fixed 0.01 lot, no bonus, full window), **OOS** (fixed-lot, no bonus, held-out tail). Incumbent **TC18** (scored the SAME way): net $785,949, DD 28.93%, edge $19,150, OOS $-1,136.

**Best net@DD<=40%:** `tc18_slm16_to05_tc05_mh240_ad0` -- net $1,953,875, DD 34.76%, edge $25,318, OOS $797.

### No cell beat TC18 on ALL FOUR. TC18 stays the trailing candidate.

Fixed: trail_tp2 mode, tp1+tp2-lock-delay 24, final TP3. Swept: sl-mult {1.6,1.7,1.8}, trailing-open {0.5,0.7}, trailing-close {0.5,0.75}, max-hold {240,300}. ELEV8 != demo -- forward-validate before deploying.
