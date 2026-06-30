# cli/ — clickable deployment commands

The `cli/*.txt` files are the canonical, byte-identical deployment snapshots
(one per strategy, the same numbered sections). Instead of copy-pasting their
multi-line PowerShell blocks, run **`cli/run.py`**: pick a strategy + a numbered
section and the command runs **in the current terminal** (foreground — it
streams output and Ctrl+C stops it, exactly as if you'd typed it).

## Use it

Open the terminal you want the process to live in, then:

```text
python cli/run.py                  # menu: pick a strategy, then a section
python cli/run.py sqz6             # menu: just the SQZ6 sections
python cli/run.py sqz6 3           # run section 3 (live auto executor) HERE
python cli/run.py victor listener  # match a section by name, too
python cli/run.py sqz6 3 --print   # show the exact command without running it
```

On Windows you can type `cli\run sqz6 3` (the `run.bat` / `run.ps1` shims call
`run.py` in the same terminal — they do not open a new window).

## Six-terminal live layout

Click into each terminal and launch one long-running section there (each blocks
until Ctrl+C, which is what you want):

| Terminal | Command |
|---|---|
| 1 | `python cli/run.py victor 1`   — Telegram listener |
| 2 | `python cli/run.py victor 2`   — Victor live filter feed |
| 3 | `python cli/run.py victor 3`   — Victor auto executor |
| 4 | `python cli/run.py sqz6 2`     — SQZ6 live feed loop |
| 5 | `python cli/run.py sqz6 3`     — SQZ6 auto executor |
| 6 | free — backtests / `mt5-info` / ad-hoc |

(The `cd` / `conda activate` / `git pull` preamble at the top of a snapshot is
**not** a runnable section: those change shell state a subprocess can't hand
back, so do them once yourself in the terminal before launching.)

## Strategies

| Alias | Snapshot | What |
|---|---|---|
| `sqz6` | `champion_R4_SQZ6_no_trailing.txt` | R4 champion rsi75_sqz6_rr40 (tag SQZ6) — **deployed** |
| `victor`, `vic`, `v116`, `vic116` | `candidate_VIC_C116_tick.txt` | Victor champion: tick-tuned TP2/mh180/slm1.7 (tag V116, M1 DD 11.3%) — **deployed** |
| `c160` | `candidate_R4_C160_tick.txt` | R4 tick winner (tag C160) — **deployed beside SQZ6** (research-grade: M1 DD 42.1% > gate; reduced risk/demo) |
| `toc5` | `candidate_TOC5_trailing_tick.txt` | C160 feed + trailing (tag TOC5) — real-DD tick sweep #1 (net $115.6k / DD 23.5%); **research — demo-validate** |
| `tc18` | `candidate_TC18_trailing_tick.txt` | TOC5 with the aggressive-sweep levers (slm1.8 / trail-after-TP2; tag TC18) — net $166.8k / DD 23.65% (+44% net at flat DD); **research — demo-validate vs TOC5** |
| `vt05` | `candidate_VT05_victor_trailing.txt` | V116 Victor feed + trailing (to0.5/tc0.5/trail-TP1, slm1.5, ad1; tag VT05) — VIC sweep #1 under DD≤40%: ~2× net/edge but ~2× DD and OOS collapses ($526→$57); **research — demo-validate vs V116** |
| `vct5` | `candidate_VCT5_victor_trailing.txt` | **Victor Trail 0.5** (tag VCT5) — VT05 geometry promoted to a **deployed** identity at **MAX risk 5%**; #1 VIC cell of the tick-calibrated Jan sweep (est edge $14k / OOS $977 / DD 23.5% **at 1%** → ~5× DD at 5%). Aggressive, NOT gate-compliant by design |
| `t160` | `candidate_T160_trailing_tick.txt` | **Trailing SLM 1.6** (tag T160) — C160 self-scalper feed + trailing (slm1.6/to0.5/tc0.5/mh240/ad0) at risk 1%; #1 cell of the tick-calibrated Jan sweep (est edge $93.9k / OOS $5.4k / DD 44.4%). **Deployed, high-DD** (beats TC18 on edge+OOS+DD) |
| `twl25` | `candidate_TWL25_loss_filtered_tick.txt` | **TWL25 loss-filter** (tag TWL25) — loss-FIRST research sibling of TSL18: harder feed filters (RSI/BB/ADX/HTF/VWAP) + faster TP2 geometry, swept loss-first on June then Jan-Jun ticks (`twl25-loss-tick-sweep.yml`). **RESEARCH / DRAFT — NOT deployed.** Stays research until the June sweep + Jan-Jun validation succeed, a cell passes the DD25/DD40 loss-first gates, and a demo-forward run confirms broker behaviour |
| `resync` | `resync_m1_from_2020.txt` | M1-archive resync utility |
| `resync-ticks`, `ticks` | `resync_ticks.txt` | tick-archive export/resync utility (MT5 → data/ticks, day-window `_D<start>_pN` parts) |

(Superseded snapshots — the old `champion_victor` VIC, `E640`, `rr08x15x30`,
`candidate_R4_SL19_tick`, and the `trailing_open_R*` / `trailing_small_0101`
research cells — were pruned 2026-06-25; recover from git history if needed.)

**Tick-aware backtests.** In the non-trailing snapshots, the **2026 backtest
sections (5 & 6) run `tools/backtest_hybrid.py --ticks data/ticks/...`** instead
of `backtest_explicit.py`: each signal is filled on the **real tick archive**
where it covers the signal (2026-05+, the closest-to-live fills) and on M1 OHLC
elsewhere — auto-routed, one combined report with a **Data Source** column. The
pre-tick eras (sections 7–9: 2025 / 2024 / 2021-2023) stay on `backtest_explicit`
(no tick overlap, so identical numbers without loading the tick archive). With no
ticks in range the hybrid output is byte-identical to `backtest_explicit`.
These sections run with **`--sync-ticks false`** — they read the committed tick
archive and do NOT auto-refresh it; sync ticks deliberately via the
`resync-ticks` snapshot when the market is open (the in-backtest sync could
collide with another process holding the parts on Windows).

The launcher reconstructs each command byte-for-byte from the `.txt` (it only
joins the PowerShell `` ` `` line-continuations), so it never diverges from the
snapshot — the `.txt` files stay the single source of truth.
