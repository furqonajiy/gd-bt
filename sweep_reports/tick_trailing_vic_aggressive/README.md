# Aggressive trailing TICK sweep (VICTOR feed) -- beat V116

96 trailing cells on committed May+June ELEV8 ticks. Metrics per cell: **net compound + DD** (risk 1% / $50k base, $3/lot bonus), **edge** (fixed 0.01 lot, no bonus, full window), **OOS** (fixed-lot, no bonus, held-out tail). Incumbent **V116** (trailing OFF, scored the SAME way): net $11,111, DD 5.61%, edge $1,776, OOS $526.

**Best net@DD<=40%:** `vic_to05_tc05_s1_slm15_TP2_ad1` -- net $21,721, DD 9.59%, edge $3,087, OOS $57.

### No cell beat V116 on ALL FOUR. V116 stays the Victor champion.

Dimensions: trailing-open {0.5,1.0}, trailing-close {0.5,1.5}, trail-after {1=TP1,2=TP2}, sl-mult {1.5,1.7}, final {TP2,TP3}. ELEV8 != your broker -- forward-validate before deploying.
