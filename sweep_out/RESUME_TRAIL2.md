# Trailing re-sweep v2 — how it runs & resumes

**Branch:** `research/trailing-sweep-v2` (engine = main with the PR #77 shared-SL/trailing-open fix).
**Goal:** find a trailing-open/close config (any 24h feed, risk walked ≤5%, concurrent DD ≤50%)
whose deployable net beats the no-trailing reference (`scalper24` e6 / range_to_sl / gap 0.5 /
slm 2.1 / TP3 / d24-d2 @ risk 1%, `sweep_out/BASELINE.json`).

## Pipeline (all stages checkpointed)

1. **Feeds** — resample M15, generate all 16 24h archives → `generated/` (`FEEDS_DONE`).
2. **Baseline** — reference config through the same harness → `BASELINE.json`.
3. **16 trail sweeps** — ~112 candidates each (structured seeds + random draw, every candidate
   has trailing_open>0 or trailing_close>0), 2 in parallel, `--resume` per archive
   → `trail2_sweep_<archive>/results.jsonl` + `DONE`.
4. **Risk post-pass** — top 6 per archive walked 5%→1% until DD ≤50% → `trail2_postpass.jsonl`.
5. **Verdict** — `FINAL_VERDICT_TRAIL2.md`; `PIPELINE_COMPLETE` marker ends the pipeline.

## Live monitoring (pushed every 5 minutes)

- `sweep_out/BEST_TRAILING_V2.txt` — leaderboard + heartbeat timestamp.
- `self_cli_trailing.txt` — best-so-far full CLI (generator → auto → backtest), updated every snapshot.
- `sweep_out/orchestrator.log` — stage log.

## Resume after a reset / 5-hour limit

A SessionStart hook (`~/.claude/settings.json` → `sweep_out/scripts/resume-trail-sweep.sh`)
re-launches the orchestrator automatically when a new session starts. Manual resume:

```bash
bash sweep_out/scripts/resume-trail-sweep.sh
```

Honest limitation: the container only computes while a session is alive. If everything stops,
prompting any new message in the session resumes the sweep from the last committed checkpoint.
