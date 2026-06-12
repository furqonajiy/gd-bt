#!/bin/bash
# SessionStart hook: relaunch the trailing re-sweep v2 after a container reset
# or 5-hour session limit. Idempotent: exits if complete or already running.
cd /home/user/xauusd-backtest 2>/dev/null || exit 0
CUR=$(git rev-parse --abbrev-ref HEAD 2>/dev/null)
if [ "$CUR" != "research/trailing-sweep-v2" ]; then
    git fetch origin research/trailing-sweep-v2 -q 2>/dev/null
    git checkout research/trailing-sweep-v2 -q 2>/dev/null || exit 0
fi
git pull -q origin research/trailing-sweep-v2 2>/dev/null
[ -f sweep_out/PIPELINE_COMPLETE ] && echo "[trail2] pipeline already complete" && exit 0
pgrep -f trail_orchestrate.py >/dev/null && echo "[trail2] orchestrator already running" && exit 0
git config user.name "C - Furqon Aji Yudhistira" 2>/dev/null
git config user.email "furqonajiy@gmail.com" 2>/dev/null
mkdir -p sweep_out
nohup python sweep_out/scripts/trail_orchestrate.py >> sweep_out/orchestrator.log 2>&1 &
echo "[trail2] RESUMED trailing sweep v2 (pid $!) — progress: sweep_out/BEST_TRAILING_V2.txt on branch research/trailing-sweep-v2"
