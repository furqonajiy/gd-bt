#!/usr/bin/env python3
"""Tick-decided loss-filter sweep for the self-scalper/TSL18 family.

The workflow is intentionally loss-first:
1. sweep June 2026 on real tick replay over stricter entry filters + defensive
   TSL18-like geometry variants;
2. replay the best June cells on Jan-Jun 2026 using every available tick file.

If Jan-Apr ticks are not present, the coverage columns expose that explicitly.
"""
from __future__ import annotations

import argparse
import csv
import glob
import json
import sys
import tempfile
from dataclasses import asdict
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]
for p in (ROOT, ROOT / "tools"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from trading.engine import CsvChartSource, DEFAULT_CONFIG, StrategyConfig, parse_signals_file  # noqa: E402
import generate_scalper_signals as gen  # noqa: E402
import tick_backtest as tk  # noqa: E402
from tools import sweep as sw  # noqa: E402

SESSIONS = (("all", 0, 0), ("london_ny", 7, 23), ("ny_core", 12, 22))
FILTERS: dict[str, dict[str, Any]] = {
    "base_c160": {"rsi_buy_max": 70, "rsi_sell_min": 30, "bb_bandwidth_min": 0.0006, "rr1": 1.2, "rr2": 2.5, "rr3": 5},
    "rsi65_bb08_adx18": {"rsi_buy_max": 65, "rsi_sell_min": 35, "bb_bandwidth_min": 0.0008, "bb_buy_pctb_max": 0.85, "bb_sell_pctb_min": 0.15, "adx_min": 18, "rr1": 1, "rr2": 2, "rr3": 3.5},
    "rsi60_htf_vwap": {"rsi_buy_max": 60, "rsi_sell_min": 40, "bb_bandwidth_min": 0.0008, "bb_buy_pctb_max": 0.8, "bb_sell_pctb_min": 0.2, "adx_min": 18, "htf_filter": True, "vwap_filter": True, "rr1": 1, "rr2": 1.8, "rr3": 3},
    "trend_pullback_quality": {"rsi_buy_max": 62, "rsi_sell_min": 38, "bb_bandwidth_min": 0.001, "adx_min": 22, "htf_filter": True, "min_slope": 0.05, "min_body_atr": 0.12, "pullback_atr": 0.18, "rr1": 1, "rr2": 2, "rr3": 4},
    "round_sr_quality": {"rsi_buy_max": 65, "rsi_sell_min": 35, "bb_bandwidth_min": 0.0008, "adx_min": 18, "sr_proximity_atr": 0.6, "sr_round_step": 10, "rr1": 1, "rr2": 2, "rr3": 3.5},
    "sd_return_quality": {"rsi_buy_max": 66, "rsi_sell_min": 34, "bb_bandwidth_min": 0.0008, "adx_min": 16, "sd_mode": "rbr_dbd", "sd_base_bars": 6, "sd_base_max_atr": 1.5, "sd_impulse_bars": 3, "sd_impulse_min_atr": 1.25, "sd_proximity_atr": 0.8, "sd_max_age_bars": 360, "rr1": 1, "rr2": 2, "rr3": 3.5},
}
BASE_TSL18: dict[str, Any] = {
    "initial_capital": 50000.0, "sizing_mode": "risk", "lot_per_entry": 0.01,
    "risk_per_signal": 0.01, "minimum_lot": 0.01, "maximum_lot": 500.0,
    "lot_step": 0.01, "bonus_per_closed_lot": 3.0, "entry_count": 8,
    "entry_ladder": "range_to_sl", "entry_sl_gap": 0.7, "shared_sl": False,
    "activation_delay_minutes": 0, "pending_expiry_minutes": 180,
    "max_hold_minutes": 150, "sl_multiplier": 1.8, "final_target": "TP3",
    "lock_after_tp1": True, "lock_after_tp2": True, "tp1_lock_delay_minutes": 24,
    "tp2_lock_delay_minutes": 24, "profit_lock_mode": "tp_levels",
    "bep_trigger_distance": 3.0, "tp1_lock_fraction": 0.75,
    "tp2_lock_target": "TP1", "runner_after_tp3": False, "tp3_lock_target": "TP2",
    "trailing_open_distance": 0.5, "trailing_close_distance": 0.5,
    "trailing_close_after_stage": 2,
}
STRATEGIES: dict[str, dict[str, Any]] = {
    "tsl18_base": {},
    "e6_s21_m120_tp2_fastlock": {"entry_count": 6, "sl_multiplier": 2.1, "max_hold_minutes": 120, "final_target": "TP2", "tp1_lock_delay_minutes": 10, "tp2_lock_delay_minutes": 2, "trailing_close_after_stage": 1},
    "e6_s21_m150_bep": {"entry_count": 6, "sl_multiplier": 2.1, "max_hold_minutes": 150, "profit_lock_mode": "bep_plus_half_tp1", "bep_trigger_distance": 2.0, "tp1_lock_fraction": 0.5, "tp1_lock_delay_minutes": 10, "tp2_lock_delay_minutes": 2, "trailing_close_after_stage": 1},
    "e5_s23_m90_tp2_defensive": {"entry_count": 5, "sl_multiplier": 2.3, "max_hold_minutes": 90, "final_target": "TP2", "tp1_lock_delay_minutes": 5, "tp2_lock_delay_minutes": 2, "trailing_close_distance": 0.75, "trailing_close_after_stage": 1},
    "e7_s20_m180_tp3_hold": {"entry_count": 7, "sl_multiplier": 2.0, "max_hold_minutes": 180, "tp1_lock_delay_minutes": 20, "tp2_lock_delay_minutes": 5, "trailing_close_distance": 1.0, "trailing_close_after_stage": 2},
    "e6_gap05_s18_tp2": {"entry_count": 6, "entry_sl_gap": 0.5, "sl_multiplier": 1.8, "max_hold_minutes": 120, "final_target": "TP2", "tp1_lock_delay_minutes": 10, "tp2_lock_delay_minutes": 2},
    "e6_scaleout_bep": {"entry_count": 6, "sl_multiplier": 2.1, "max_hold_minutes": 150, "scale_out_at_tp1": True, "bep_after_tp1": True, "bep_buffer": 0.5, "tp1_lock_delay_minutes": 5, "tp2_lock_delay_minutes": 2, "trailing_close_after_stage": 1},
    "e4_scaleout_low_dd": {"entry_count": 4, "sl_multiplier": 2.3, "max_hold_minutes": 90, "final_target": "TP2", "scale_out_at_tp1": True, "bep_after_tp1": True, "bep_buffer": 0.5, "tp1_lock_delay_minutes": 0, "tp2_lock_delay_minutes": 2, "trailing_close_distance": 0.75, "trailing_close_after_stage": 1},
}

# The full grid is one cell per (session, filter, strategy). A complete June
# aggregation must contain exactly this many candidate rows; the workflow passes
# it to `aggregate --expected-rows` so a missing shard can't publish a partial board.
EXPECTED_FULL_GRID = len(SESSIONS) * len(FILTERS) * len(STRATEGIES)  # 3 x 6 x 8 = 144


def expand(patterns: Iterable[str]) -> list[str]:
    out: list[str] = []
    for pat in patterns:
        matches = sorted(glob.glob(pat)) if any(c in pat for c in "*?[") else [pat]
        out.extend(m for m in matches if Path(m).exists())
    if not out:
        raise SystemExit(f"No files matched: {list(patterns)}")
    return out


def phase_defaults(phase: str) -> tuple[list[str], list[str], str, str | None]:
    # end is EXCLUSIVE (gen_args passes it to --end, and the generator excludes
    # rows at/after it). June must stop at 2026-07-01 so a June run cannot leak
    # July/future signals; Jan-Jun spans the whole half-year to the same bound.
    if phase == "june":
        return ["data/XAUUSD_M1_202605_ELEV8.csv", "data/XAUUSD_M1_202606_ELEV8.csv"], ["data/ticks/XAUUSD_TICK_202606*_ELEV8.csv"], "2026-06-01", "2026-07-01"
    if phase == "jan_jun":
        return ["data/XAUUSD_M1_2026*_ELEV8.csv"], ["data/ticks/XAUUSD_TICK_20260*_ELEV8.csv"], "2026-01-01", "2026-07-01"
    raise SystemExit(f"unknown phase: {phase}")


def candidates() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for session, start, end in SESSIONS:
        for filter_name, flags in FILTERS.items():
            sig_flags = dict(flags, session_start=start, session_end=end)
            sig_name = f"{filter_name}_{session}"
            for strat_name, overrides in STRATEGIES.items():
                c = {"signal_name": sig_name, "signal_flags": sig_flags,
                     "strategy_name": strat_name, "strategy_overrides": overrides}
                c["candidate_id"] = sw._json_hash(c)
                out.append(c)
    return out


def gen_args(flags: dict[str, Any], charts: list[str], output: Path, start: str, end: str | None) -> argparse.Namespace:
    argv = ["--charts", *charts, "--output", str(output), "--start", start, "--signal-tz", "7", "--progress-interval-seconds", "0", "--progress-every-rows", "0"]
    if end:
        argv += ["--end", end]
    for key, value in sorted(flags.items()):
        opt = "--" + key.replace("_", "-")
        if isinstance(value, bool):
            argv.append(opt if value else "--no-" + key.replace("_", "-"))
        else:
            argv += [opt, str(value)]
    return gen.build_parser().parse_args(argv)


def strategy_config(overrides: dict[str, Any]) -> StrategyConfig:
    payload = asdict(DEFAULT_CONFIG)
    payload.update(BASE_TSL18)
    payload.update(overrides)
    # R4 backtest/sweep scoring uses the measured locked-exit realism model
    # (docs/BACKTEST_REALISM.md): a LOCK_TP1/LOCK_TP2 protective stop fills at
    # market on the retrace, costing ~2.0/1.0 points. This is a BACKTEST-ONLY
    # realism knob -- DEFAULT_CONFIG and the live executor keep it at 0 (the
    # broker adds the real slippage on the live fill), so live order placement is
    # unchanged. Scoring TWL25 candidates without it would over-reward locked
    # exits and pick an over-optimistic champion. Kept explicit here (not via env
    # vars) so the sweep config is reproducible regardless of shell state.
    payload["lock_tp1_exit_slippage_points"] = 2.0
    payload["lock_tp2_exit_slippage_points"] = 1.0
    return StrategyConfig(**payload)


def curve_stats(pnls: list[float], initial_capital: float,
                days: list[Any] | None = None) -> dict[str, Any]:
    wins = sum(p > 0 for p in pnls)
    losses = sum(p < 0 for p in pnls)
    flats = sum(p == 0 for p in pnls)
    gross_win = sum(p for p in pnls if p > 0)
    gross_loss = -sum(p for p in pnls if p < 0)
    peak = trough = initial_capital
    equity = initial_capital
    max_dd = 0.0
    for pnl in pnls:
        equity += pnl
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
        trough = min(trough, equity)
    closed = wins + losses
    pf = gross_win / gross_loss if gross_loss else (99.0 if gross_win > 0 else 0.0)

    # --- Loss-quality metrics (TWL25 is loss-first: the remaining TSL18 problem
    #     is STRUCTURAL/SEQUENTIAL loss, not generic tuning -- see
    #     docs/TSL18_STRUCTURE_GUARD.md). Rank on these, not just total P&L/DD.
    max_consec = cur_consec = 0
    for p in pnls:
        if p < 0:
            cur_consec += 1
            max_consec = max(max_consec, cur_consec)
        else:
            cur_consec = 0
    # worst single losing signal (positive magnitude; 0 if no losing signal)
    worst_single = max(0.0, -min(pnls)) if pnls else 0.0
    # worst calendar (feed-zone) day's net P&L -> positive magnitude of the loss
    max_daily_loss = 0.0
    if days is not None and len(days) == len(pnls) and pnls:
        by_day: dict[Any, float] = {}
        for p, d in zip(pnls, days):
            by_day[d] = by_day.get(d, 0.0) + p
        worst_day_net = min(by_day.values()) if by_day else 0.0
        max_daily_loss = round(-worst_day_net, 2) if worst_day_net < 0 else 0.0

    return {
        "tick_pnl": round(sum(pnls), 2), "wins": wins, "losses": losses, "flats": flats,
        "win_rate_pct": round(wins / closed * 100.0, 2) if closed else 0.0,
        "profit_factor": round(min(pf, 99.0), 3), "max_drawdown": round(max_dd, 2),
        "max_drawdown_pct": round(max_dd / initial_capital * 100.0, 2),
        "max_loss_signal": round(min(pnls or [0.0]), 2), "min_equity": round(trough, 2),
        # loss-first metrics
        "loss_count": losses,
        "max_consecutive_losing_signals": max_consec,
        "max_daily_loss": max_daily_loss,
        "worst_single_signal_loss": round(worst_single, 2),
    }


def passes_gates(tick_pnl: float, dd: float, win_rate: float,
                 open_left: int) -> tuple[bool, bool]:
    """(passes_dd25, passes_dd40). STRICT: a candidate with ANY unresolved
    position/order (``open_left`` > 0) is NOT deployment-ready -- it leaves live
    exposure the model never closed -- so it fails BOTH gates regardless of how
    good its P&L / DD / win-rate look. Partial-coverage signals are already
    excluded from the scored curve upstream, so ``tick_pnl`` here is full-coverage
    only."""
    clean = open_left == 0
    dd25 = clean and tick_pnl > 0 and dd <= 25 and win_rate >= 45
    dd40 = clean and tick_pnl > 0 and dd <= 40 and win_rate >= 40
    return dd25, dd40


def evaluate(c: dict[str, Any], *, chart: CsvChartSource, ticks, charts: list[str], phase: str, start: str, end: str | None, tmp: Path, watch_seconds: int, min_signals: int, max_signals: int) -> dict[str, Any]:
    feed = tmp / f"{c['candidate_id']}.txt"
    args = gen_args(c["signal_flags"], charts, feed, start, end)
    signals_raw = gen.generate_signals(chart.dataframe, args)
    gen._write_signal_file(signals_raw, feed, signal_tz=7)
    signals = parse_signals_file(feed)
    if len(signals) < min_signals:
        return {**c, "phase": phase, "generated_signals": len(signals), "error": f"too few signals ({len(signals)} < {min_signals})", "score": -1e18, "config_json": json.dumps(c, sort_keys=True)}
    if max_signals > 0:
        signals = signals[:max_signals]
    cfg = strategy_config(c["strategy_overrides"])
    clock = tk._install_sim_clock()
    pnls: list[float] = []
    days: list[Any] = []
    no_ticks = no_fill = open_left = covered = partial = 0
    for sig in signals:
        res = tk.run_signal(sig, cfg, chart, ticks, "XAUUSD", watch_seconds, clock)
        if res.get("no_ticks"):
            no_ticks += 1
            continue
        # Partial tick coverage: the archive does NOT span the signal's full
        # lifecycle, so the replay modelled an incomplete window. Count it, surface
        # any unresolved exposure it left, but do NOT fold its P&L into the scored
        # curve -- a partially-covered signal must not masquerade as a clean tick
        # result (the sweep-integrity rule).
        if not res.get("covers_full_lifecycle"):
            partial += 1
            open_left += int(res.get("open_left") or 0) + int(res.get("pending_left") or 0)
            continue
        covered += 1
        total = float(res.get("total") or 0.0)
        if total == 0.0:
            no_fill += 1
        open_left += int(res.get("open_left") or 0) + int(res.get("pending_left") or 0)
        pnls.append(total)
        # Group by the signal's feed-zone (source) day -- the same day key the
        # report's Daily breakdown uses -- so max_daily_loss lines up with the codes.
        src = getattr(sig, "signal_time_source", None) or getattr(sig, "signal_time_chart", None)
        days.append(src.date() if src is not None else None)
    stats = curve_stats(pnls, cfg.initial_capital, days=days)
    # Coverage % = FULLY-covered signals only (partial/no-tick excluded), so the
    # column honestly reflects how much of the feed was scored on real ticks.
    coverage = covered / len(signals) * 100.0 if signals else 0.0
    dd = stats["max_drawdown_pct"]
    # Loss-FIRST scoring: heavily penalize SEQUENTIAL losses and the worst single
    # day (the structural TSL18 failure mode, docs/TSL18_STRUCTURE_GUARD.md), not
    # just total P&L / DD / win-rate. The DD25/DD40 gates remain the primary rank
    # key (see leaderboard()), but among gate-equal cells the one with fewer
    # consecutive losers and a smaller worst day wins. Unresolved exposure
    # (open_or_pending_left) and partial-coverage signals are severely penalized so
    # an incomplete/leaky cell can never out-rank a clean one.
    score = (
        stats["tick_pnl"]
        + stats["win_rate_pct"] * 20
        + min(stats["profit_factor"], 5) * 200
        - max(0.0, dd - 25) * 2000
        - stats["max_consecutive_losing_signals"] * 400
        - stats["max_daily_loss"] * 2.0
        - stats["loss_count"] * 15
        - stats["worst_single_signal_loss"] * 0.5
        - open_left * 5000
        - partial * 250
    )
    dd25, dd40 = passes_gates(stats["tick_pnl"], dd, stats["win_rate_pct"], open_left)
    return {**c, **stats, "phase": phase, "generated_signals": len(signals), "tick_covered_signals": covered, "tick_coverage_pct": round(coverage, 2), "no_tick_signals": no_ticks, "partial_tick_signals": partial, "no_fill_signals": no_fill, "open_or_pending_left": open_left, "passes_dd25_gate": dd25, "passes_dd40_gate": dd40, "score": round(score, 2), "config_json": json.dumps(c, sort_keys=True)}


def write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, default=str, sort_keys=True) + "\n")


def load_jsonl(patterns: Iterable[str]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for pat in patterns:
        for path in glob.glob(pat, recursive=True):
            with open(path, encoding="utf-8", errors="replace") as fh:
                rows.extend(json.loads(line) for line in fh if line.strip())
    return rows


def leaderboard(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(rows, key=lambda r: (bool(r.get("passes_dd25_gate")), bool(r.get("passes_dd40_gate")), float(r.get("score") or -1e18)), reverse=True)


def duplicate_keys(rows: list[dict[str, Any]]) -> dict[tuple, int]:
    """Count rows sharing the same (phase, candidate_id). A candidate is sweep-
    once per phase, so any count > 1 means the aggregate inputs double-counted
    (e.g. globbing all_results_*/top_candidates.jsonl in addition to the raw
    shard results) -- the 432-row vs 144-row bug."""
    from collections import Counter
    counts = Counter((r.get("phase"), r.get("candidate_id")) for r in rows)
    return {k: n for k, n in counts.items() if n > 1}


def write_board(rows: list[dict[str, Any]], out_dir: Path, phase: str, top_n: int) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    board = leaderboard(rows)
    cols = ["rank", "candidate_id", "phase", "signal_name", "strategy_name", "score", "tick_pnl", "max_drawdown_pct", "win_rate_pct", "profit_factor", "max_consecutive_losing_signals", "max_daily_loss", "loss_count", "worst_single_signal_loss", "open_or_pending_left", "wins", "losses", "flats", "generated_signals", "tick_covered_signals", "tick_coverage_pct", "no_tick_signals", "partial_tick_signals", "no_fill_signals", "max_loss_signal", "passes_dd25_gate", "passes_dd40_gate", "error"]
    with (out_dir / f"leaderboard_{phase}.csv").open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        writer.writeheader()
        for rank, row in enumerate(board, 1):
            writer.writerow({**row, "rank": rank})
    write_jsonl(out_dir / f"all_results_{phase}.jsonl", board)
    write_jsonl(out_dir / "top_candidates.jsonl", board[:top_n])
    with (out_dir / f"README_{phase}.md").open("w", encoding="utf-8") as fh:
        fh.write(f"# TWL25 loss-filter tick sweep: {phase}\n\nRows: {len(rows)}. Ranked by DD25 gate, DD40 gate, then score.\n")
        if board:
            b = board[0]
            fh.write(f"\nLeader: `{b.get('candidate_id')}` / `{b.get('signal_name')}` / `{b.get('strategy_name')}`\n")
            fh.write(f"\nTick P&L `{b.get('tick_pnl')}`, DD `{b.get('max_drawdown_pct')}%`, win rate `{b.get('win_rate_pct')}%`, tick coverage `{b.get('tick_coverage_pct')}%`, open/pending left `{b.get('open_or_pending_left')}`, partial-tick signals `{b.get('partial_tick_signals')}`.\n")


def run_shard(args: argparse.Namespace) -> int:
    dc, dt, start, end = phase_defaults(args.phase)
    charts = expand(args.charts or dc)
    ticks = tk.load_ticks(expand(args.ticks or dt))
    chart = CsvChartSource(charts)
    cells = [c for i, c in enumerate(candidates()) if i % args.shards == args.shard]
    if args.max_cells > 0:
        cells = cells[:args.max_cells]
    rows: list[dict[str, Any]] = []
    print(f"[twl25] phase={args.phase} shard={args.shard}/{args.shards} cells={len(cells)} charts={len(charts)}")
    with tempfile.TemporaryDirectory(prefix="twl25_") as td:
        for i, cell in enumerate(cells, 1):
            print(f"[twl25] {i}/{len(cells)} {cell['candidate_id']} {cell['signal_name']} + {cell['strategy_name']}", flush=True)
            try:
                row = evaluate(cell, chart=chart, ticks=ticks, charts=charts, phase=args.phase, start=start, end=end, tmp=Path(td), watch_seconds=args.watch_seconds, min_signals=args.min_signals, max_signals=args.max_signals)
            except Exception as exc:
                row = {**cell, "phase": args.phase, "error": repr(exc), "score": -1e18, "tick_pnl": -1e18, "config_json": json.dumps(cell, sort_keys=True)}
            print(json.dumps({k: row.get(k) for k in ("candidate_id", "tick_pnl", "max_drawdown_pct", "win_rate_pct", "score", "error")}, default=str), flush=True)
            rows.append(row)
    out = Path(args.output_dir)
    write_jsonl(out / f"results_{args.phase}_shard{args.shard}.jsonl", rows)
    write_board(rows, out, f"{args.phase}_shard{args.shard}", args.top_n)
    return 0


def aggregate(args: argparse.Namespace) -> int:
    rows = load_jsonl(args.inputs)
    if not rows:
        raise SystemExit("No JSONL rows found")
    # Fail LOUDLY on double-counting. Aggregate inputs must be ONLY the raw shard
    # result files (results_<phase>_shard*.jsonl); globbing the per-shard
    # all_results_*/top_candidates.jsonl too counted every candidate 3x (432 rows
    # instead of 3 sessions x 6 filters x 8 strategies = 144).
    dups = duplicate_keys(rows)
    if dups:
        sample = list(dups.items())[:5]
        raise SystemExit(
            f"Duplicate candidate rows detected: {len(dups)} (phase, candidate_id) keys "
            f"appear more than once (e.g. {sample}). Aggregate --inputs must match ONLY "
            f"the raw shard files (results_<phase>_shard*.jsonl), not all_results_* or "
            f"top_candidates.jsonl.")
    # Completeness gate: a FULL grid is 3 sessions x 6 filters x 8 strategies = 144
    # candidates. If --expected-rows is set (the workflow passes 144 on full runs),
    # refuse to publish a leaderboard that is missing or has extra rows -- a missing
    # shard would otherwise publish a silently-incomplete board. Smoke / --max-cells
    # runs pass 0 to skip this.
    if args.expected_rows > 0 and len(rows) != args.expected_rows:
        raise SystemExit(
            f"Incomplete aggregation: got {len(rows)} candidate rows, expected exactly "
            f"{args.expected_rows} (the full {EXPECTED_FULL_GRID}-cell grid). A shard is "
            f"missing or duplicated -- refusing to publish a partial leaderboard. Re-run "
            f"the failed June shard(s), or pass --expected-rows 0 for an intentional smoke.")
    errored = sum(1 for r in rows if r.get("error"))
    if errored == len(rows):
        raise SystemExit(f"All {len(rows)} aggregated candidates errored; refusing to "
                         f"publish a leaderboard of pure errors. See the JSONL 'error' field.")
    write_board(rows, Path(args.output_dir), args.phase, args.top_n)
    print(f"[twl25] aggregated {len(rows)} rows ({errored} errored) into {args.output_dir}")
    return 0


def validate_top(args: argparse.Namespace) -> int:
    top = load_jsonl(args.candidate_jsonl)[:args.top_n]
    dc, dt, start, end = phase_defaults(args.phase)
    charts = expand(args.charts or dc)
    ticks = tk.load_ticks(expand(args.ticks or dt))
    chart = CsvChartSource(charts)
    rows: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="twl25_val_") as td:
        for row in top:
            # Per-candidate try/except (mirrors run_shard): one bad candidate must
            # not crash the whole Jan-Jun validation job. Preserve identity +
            # config so the errored row is still traceable in the leaderboard.
            try:
                cell = json.loads(row["config_json"])
            except Exception:
                cell = {k: row.get(k) for k in ("signal_name", "signal_flags",
                                                "strategy_name", "strategy_overrides",
                                                "candidate_id")}
            cell["candidate_id"] = row.get("candidate_id", cell.get("candidate_id", sw._json_hash(cell)))
            try:
                rows.append(evaluate(cell, chart=chart, ticks=ticks, charts=charts, phase=args.phase, start=start, end=end, tmp=Path(td), watch_seconds=args.watch_seconds, min_signals=args.min_signals, max_signals=args.max_signals))
            except Exception as exc:
                rows.append({
                    "candidate_id": cell.get("candidate_id"),
                    "signal_name": cell.get("signal_name"),
                    "strategy_name": cell.get("strategy_name"),
                    "phase": args.phase,
                    "config_json": json.dumps(cell, sort_keys=True),
                    "error": repr(exc), "score": -1e18, "tick_pnl": -1e18,
                })
    write_jsonl(Path(args.output_dir) / f"results_{args.phase}_validation.jsonl", rows)
    write_board(rows, Path(args.output_dir), args.phase, args.top_n)
    if rows and all(r.get("error") for r in rows):
        raise SystemExit(f"All {len(rows)} validation candidates errored; see the "
                         f"'error' column in leaderboard_{args.phase}.csv.")
    if not any(r.get("passes_dd40_gate") for r in rows):
        print(f"[twl25] WARNING: no validation candidate passed the DD40 loss-first gate "
              f"({len(rows)} evaluated). TWL25 stays research.", flush=True)
    return 0


def add_common(sp: argparse.ArgumentParser) -> None:
    sp.add_argument("--phase", choices=["june", "jan_jun"], default="june")
    sp.add_argument("--charts", nargs="*", default=None)
    sp.add_argument("--ticks", nargs="*", default=None)
    sp.add_argument("--output-dir", required=True)
    sp.add_argument("--watch-seconds", type=int, default=3)
    sp.add_argument("--min-signals", type=int, default=10)
    sp.add_argument("--max-signals", type=int, default=0, help="0 = no max")
    sp.add_argument("--top-n", type=int, default=25)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Tick-decided loss-filter sweep for TWL25.")
    sub = p.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run-shard")
    add_common(run)
    run.add_argument("--shards", type=int, default=12)
    run.add_argument("--shard", type=int, required=True)
    run.add_argument("--max-cells", type=int, default=0)
    run.set_defaults(func=run_shard)
    agg = sub.add_parser("aggregate")
    agg.add_argument("--inputs", nargs="+", required=True)
    agg.add_argument("--output-dir", required=True)
    agg.add_argument("--phase", default="june")
    agg.add_argument("--top-n", type=int, default=25)
    agg.add_argument("--expected-rows", type=int, default=0,
                     help="For a FULL grid run, the exact candidate count expected "
                          "(3 sessions x 6 filters x 8 strategies = 144). Aggregate "
                          "REFUSES to publish if the row count differs (missing/extra "
                          "shards). 0 = no check (smoke / --max-cells runs).")
    agg.set_defaults(func=aggregate)
    val = sub.add_parser("validate-top")
    add_common(val)
    val.add_argument("--candidate-jsonl", nargs="+", required=True)
    val.set_defaults(func=validate_top)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
