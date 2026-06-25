# Self-scalper TRAILING-open/close TICK sweep (committed June ELEV8 ticks)

18 cells, ranked by **tick P&L** (real Mt5Executor on June ticks).
Geometry pinned to C160 (e7 / slm2.1 / mh300 / TP3 / lock-after-tp1 / tp1-frac0.75).
Grid: trailing_open x trailing_close x after_stage. (0,0,*) = no-trail baseline.

Distances >= 0.5 are TRADEABLE (broker min-stop ~0.4); the tick mock does not
enforce that floor, so keep deployed distances >= 0.5. Tick fills are broker-
driven -- a winner is RESEARCH until forward/demo-validated.
