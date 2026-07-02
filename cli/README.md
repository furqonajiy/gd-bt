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
python cli/run.py ts3k             # menu: just the TS3K sections
python cli/run.py ts3k 3           # run section 3 (live auto executor) HERE
python cli/run.py v072 listener    # match a section by name, too
python cli/run.py ts3k 3 --print   # show the exact command without running it
```

On Windows you can type `cli\run ts3k 3` (the `run.bat` / `run.ps1` shims call
`run.py` in the same terminal — they do not open a new window).

## Live layout (live set: TS3K + V073A; step up to TSL18 at ~$10K)

Click into each terminal and launch one long-running section there (each blocks
until Ctrl+C, which is what you want). The deployed live set is the $3K book
**TS3K** and the Victor champion **V073A** running beside it; step up to **TSL18**
around ~$10K. Swap V073A → **V072** (the as-posted fallback) only during an R2
bull-grind, when the corrected-R:R ladder inverts:

| Terminal | Command |
|---|---|
| 1 | `python cli/run.py ts3k 2`   — TS3K live feed loop |
| 2 | `python cli/run.py ts3k 3`   — TS3K auto executor (the live $3K book) |
| 3 | `python cli/run.py v073a 1`  — Telegram listener (Victor feed) |
| 4 | `python cli/run.py v073a 2`  — Victor corrected-R:R live feed (→ `signals/victor_v073a_live.txt`) |
| 5 | `python cli/run.py v073a 3`  — V073A auto executor (Victor champion; STOP and revert to `v072 3` if the regime flips to an R2 grind) |
| 6 | free — backtests / `mt5-info` / ad-hoc |

(The `cd` / `conda activate` / `git pull` preamble at the top of a snapshot is
**not** a runnable section: those change shell state a subprocess can't hand
back, so do them once yourself in the terminal before launching.)

## Strategies (the 2026-07-02 deployed set)

| Alias | Snapshot | What |
|---|---|---|
| `v073a` | `candidate_V073A_victor_rr_trailing.txt` | **Victor champion** (tag V073A, deployed 2026-07-02) — V072 geometry (entries 8 / slm1.6 / mh180 / trailing-open+close 0.5 after TP1) fed a **corrected-R:R** Victor ladder (SL:TP1 1:1.5 / TP2 1:3 / TP3 1:5), risk 5%. **Uncapped runner** (`--runner-final-cap none`): TP3 is a reference level, the trailing-close SL owns the exit. **Regime-scoped** — run only in a trending regime (R3/R4); revert to `v072` in an R2 bull-grind (the ladder inverts there) |
| `v072` | `candidate_V072_victor_trailing_combo.txt` | **Victor R2-grind fallback** (tag V072) — the as-posted Victor feed on the combined trailing geometry (V017 base + slm1.6 / gap0.7 / mh180, trailing-open+close 0.5, TP3 a reference level under the uncapped-runner default). Run in place of V073A during an R2 bull-grind. May+June refreshed archive (316 TICK / 49 M1): pure **$80,962** at DD 19.02% |
| `tsl18` | `candidate_TSL18_trailing_tick.txt` | **Self-scalper champion** (tag TSL18) — C160 feed, e8 / slm1.8 / TP3(ref) / trailing 0.5-0.5 after TP2, locks 24/24, **uncapped runner** (`--runner-final-cap none`). The full book for **≥ ~$10K** accounts (8-entry floor is ~$4K–$8K) |
| `ts3k` | `candidate_TS3K_small_account_tick.txt` | **THE LIVE $3K BOOK** (tag TS3K) — TSL18's exact feed+geometry at **entries 1 / risk 1% / max-open 4 / daily-breaker 10%** (the measured best $3K variant; full-8-entry at $3K rides a 41%-of-account DD). Step up to `tsl18` around ~$10K |
| `resync` | `resync_m1_from_2020.txt` | M1-archive resync utility |
| `resync-ticks`, `ticks` | `resync_ticks.txt` | tick-archive export/resync utility (MT5 → data/ticks, day-window `_D<start>_pN` parts) |

(Pruned 2026-07-02 — everything else: SQZ6, VIC_C116/V116, C160, TOC5, TC18,
VT05, VCT5, VS17, V017, V817, T160, T18S, T818, TWL25, TS2K, the TSG18
structure-guard shadow, and the demo books DTR0/TR05. The operator consolidated
to champions **V073A + TSL18** (with **V072** kept as the R2-grind fallback for
V073A) and **TS3K** as the live $3K book; recover any pruned snapshot from git
history if needed. Earlier prune 2026-06-25:
`champion_victor` VIC, `E640`, `rr08x15x30`, `candidate_R4_SL19_tick`,
`trailing_open_R*` / `trailing_small_0101`.)

**Fast trailing live snapshots (`tsl18 3`, etc.) must run continuously.** Their
section-3 LIVE AUTO EXECUTOR carries live stale/terminal guards (terminal-SL
always on; `--max-live-signal-age-minutes` / `--min-live-entry-rr` /
`--min-live-entry-reward-distance` / `--max-live-spread-fraction-of-risk` set
conservatively): a signal whose original SL or final target was already touched
is **terminal** and is never opened or re-armed, and a late restart will **not**
back-fill resolved signals. The dangerous `--allow-live-replay-played-out-legs`
(which would revive played-out legs) is **default OFF and never set in a live
snapshot**. See `docs/OPERATIONS_PLAYBOOK.md` → *Live stale / terminal-signal
protection*.

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
