# Trailing sweep — per-regime TOP-20 (PARTIAL SNAPSHOT)

Source run: `28009972567` (`self-scalper-trailing-sweep-r4r3r2r1.yml`).

Gate: DD <= 40%% AND OOS > 0, ranked by fixed-lot edge (`fixed_no_bonus_profit`).

## Coverage (completed cells per regime, of 49 in the full grid)

- **R4** (R4 parabolic (2026)): 29/49 cells, 20 gate-passing in top-20.
- **R3** (R3 strong (2025)): 30/49 cells, 20 gate-passing in top-20.
- **R2** (R2 bull (2023-10..2024)): 23/49 cells, 20 gate-passing in top-20.
- **R1** (R1 quiet (2021-11..2023-09)): 18/49 cells, 20 gate-passing in top-20.

## ⚠️ Read before acting

This is a **partial, in-progress** snapshot, not the final per-regime winner-vs-base verdict (that comes from the `r*_agg` leaderboards once each regime's full grid finishes).

**Trailing is live-parity-fragile.** A small trailing-open (0.1-0.2) can flatter the backtest with better-than-real entry fills, so the very large edge figures here are suspected modeling artifacts. These tables RANK candidates; they do NOT certify them for live. Forward/demo-validate the top trailing-open cells (and check entry-fill realism) before any deploy.
