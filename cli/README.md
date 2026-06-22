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
| `resync` | `resync_m1_from_2020.txt` | M1-archive resync utility |

The launcher reconstructs each command byte-for-byte from the `.txt` (it only
joins the PowerShell `` ` `` line-continuations), so it never diverges from the
snapshot — the `.txt` files stay the single source of truth.
