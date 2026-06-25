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
| `victor`, `vic` | `champion_victor.txt` | Victor feed (tag VIC) — deployed |
| `sqz6` | `champion_R4_SQZ6_no_trailing.txt` | R4 champion rsi75_sqz6_rr40 (tag SQZ6) — deployed |
| `e640` | `E640.txt` | cross-regime candidate (tag E640) |
| `rr08` | `rr08x15x30.txt` | R:R research candidate (tag RR08) |
| `sl19` | `candidate_R4_SL19_tick.txt` | R4 June-tick candidate: SQZ6 w/ slm1.9 (tag SL19) — **research** |
| `c160` | `candidate_R4_C160_tick.txt` | R4 May+June-tick sweep winner (tag C160) — **research** |
| `tr40` | `trailing_open_R4.txt` | best trailing-open cell, R4 (tag TR40) — **research, parity-fragile** |
| `tr30` | `trailing_open_R3.txt` | best trailing-open cell, R3 (tag TR30) — **research, parity-fragile** |
| `tr20` | `trailing_open_R2.txt` | best trailing-open cell, R2 (tag TR20) — **research, parity-fragile** |
| `tr10` | `trailing_open_R1.txt` | best trailing-open cell, R1 (tag TR10) — **research, parity-fragile** |
| `ts01` | `trailing_small_0101.txt` | `to0p1_tc0p1` (0.1/0.1) — #1 cell in ALL regimes (tag TS01) — **research, strongest fill-artifact suspicion** |
| `resync` | `resync_m1_from_2020.txt` | M1-archive resync utility |

The `trailing_open_R*` snapshots are the best trailing-open cells from the
in-progress trailing sweep (run 28009972567). They are **backtest research
only** — trailing-open is live-parity-fragile (a 0.1–0.2 trailing-open fills
entries on a pullback the live executor can't reproduce on M1), so their large
edge is likely optimistic. Reproduce/study them; do **not** deploy without
forward/demo validation. They follow the full champion 9-section format
(listener → live loop → auto executor → signal generator → 5 era backtests), but
section 3 (live executor) is **demo/forward-validation only**, and each carries a
high swept risk%/signal — size down before any real-money trial.

The launcher reconstructs each command byte-for-byte from the `.txt` (it only
joins the PowerShell `` ` `` line-continuations), so it never diverges from the
snapshot — the `.txt` files stay the single source of truth.
