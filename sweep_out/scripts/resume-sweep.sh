#!/bin/bash
# SessionStart hook: auto-resume the sweep PIPELINE after a container reset.
# Never re-runs finished work: pipeline.sh skips DONE archives and sweeps
# --resume from committed checkpoints. No-ops forever once PIPELINE_COMPLETE
# is committed on the branch.
REPO=/home/user/xauusd-backtest
BRANCH=research/self-signal-sweep

cd "$REPO" 2>/dev/null || exit 0
exec 9>/tmp/resume-sweep.lock
flock -n 9 || exit 0
pgrep -f "sweep_out/scripts/pipeline.sh" >/dev/null 2>&1 && exit 0

git fetch origin "$BRANCH" >/dev/null 2>&1 || exit 0
git rev-parse --verify "origin/$BRANCH" >/dev/null 2>&1 || exit 0
git cat-file -e "origin/$BRANCH:sweep_out/PIPELINE_COMPLETE" 2>/dev/null && exit 0

cur=$(git branch --show-current)
if [ "$cur" != "$BRANCH" ]; then
  [ -n "$(git status --porcelain)" ] && exit 0
  git checkout "$BRANCH" >/dev/null 2>&1 || exit 0
fi
git pull -q origin "$BRANCH" >/dev/null 2>&1

[ -f sweep_out/scripts/pipeline.sh ] || exit 0
nohup bash sweep_out/scripts/pipeline.sh >> /tmp/pipeline.log 2>&1 &
echo "[resume-sweep] pipeline auto-restarted from last pushed checkpoint (no work re-run)"
exit 0
