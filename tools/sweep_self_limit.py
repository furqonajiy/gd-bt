#!/usr/bin/env python3
"""Self-archive LIMIT-only sweep with honest, parity-aware gates.

Why this exists alongside tools/sweep_limit_entry.py:
  sweep_limit_entry pins LIMIT entries but is wired to the *provider* feed and
  ranks largely on risk-sized net profit. On a dense self-signal archive that is
  misleading on three counts the project already documents:
    1. Full-period risk-sized equity is a sequential-compounding artifact (it
       grows to millions purely because % risk compounds into a late strong
       regime), so it does not measure the per-trade edge.
    2. The $3/lot bonus inflates backtest P&L vs live.
    3. Sequential DD understates the concurrent DD that actually governs a live
       account running many correlated same-direction positions.

So this tool:
  * consumes a SELF-generated archive directly (no provider filtering);
  * forces trailing_open=0 (plain LIMIT/BUY-LIMIT/SELL-LIMIT entries);
  * ranks the EDGE on FIXED-LOT, no-bonus net profit;
  * GATES on the CONCURRENT risk-sized drawdown (default <= 30%, derated from 40
    because the backtest understates live concurrent DD);
  * GATES on monthly consistency (fixed-lot no-bonus): a minimum fraction of
    months profitable, with an optional worst-month floor;
  * validates OOS on a held-out tail, also fixed-lot no-bonus.

It reuses sweep.py's validated concurrent engine, candidate draw, checkpointing
and leaderboard writer verbatim; only candidate pinning and the per-candidate
evaluation differ, so results stay comparable and parity-safe.

CAVEATS (do not skip before any live use):
  * LIMIT entries ride the latent ORDER_FILLING_RETURN fill bug -- fix first.
  * trailing_close is M1-vs-live-tick parity-fragile (exit-side trailing).
  * Self-signals are research, NOT cleared. A sweep winner is in-sample on one
    ~18-month path with a non-stationary edge; forward/demo validation is
    required before it means anything.
"""
from __future__ import annotations

import argparse
import importlib
import json
import random
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Reuse the validated sweep machinery (concurrent engine, draw, checkpoint, board).
sweep = importlib.import_module("tools.sweep")

from trading.engine import CsvChartSource, parse_signals_file  # noqa: E402

FILTER_LABEL = "self_limit"  # stored in the row; not a provider filter


def make_limit_candidates(seed: int, max_candidates: int,
                          *, signal_policy: bool = False,
                          bep_policy: bool = False,
                          pin_trailing_open: float | None = None,
                          pin_trailing_close: float | None = None) -> list[dict]:
    """sweep.candidate_config draws with trailing-open pinned to 0 (LIMIT).

    Pinning collapses configs that differed only in trailing_open, so we draw
    with headroom to still fill the cap.

    With ``bep_policy`` the cell is the BEP early-floor variant: it seeds the
    explicit "SC24 champion + early floor" grid (not the bep-OFF base/neighborhood)
    and draws bep_plus_half_tp1 candidates, so its leaderboard contains only
    early-floor configs and the per-variant aggregate compares it cleanly against
    the bep-OFF base cell. Without it the candidate space is byte-unchanged.

    ``pin_trailing_open`` / ``pin_trailing_close`` force a FIXED trailing combo on
    every candidate (a trailing-sweep cell): the whole SC24-seeded strategy grid
    is then scored at that one (open, close) so the per-regime matrix enumerates
    all trailing combinations. Both default ``None`` -> the legacy behaviour:
    trailing_open pinned 0 (LIMIT entries) and trailing_close left to the draw, so
    the existing no-trailing sweeps are byte-identical.
    """
    seen: set[str] = set()
    out: list[dict] = []

    def _add(cfg: dict) -> None:
        cfg = dict(cfg)
        # trailing-open: legacy pins to 0 (plain LIMIT). A trailing sweep pins it
        # to the cell's value so the strategy grid is scored at that trailing-open.
        cfg["trailing_open_distance"] = (0.0 if pin_trailing_open is None
                                         else pin_trailing_open)
        # trailing-close: legacy leaves the candidate's drawn value; a trailing
        # sweep pins it to the cell's value.
        if pin_trailing_close is not None:
            cfg["trailing_close_distance"] = pin_trailing_close
        h = sweep._json_hash(cfg)
        if h not in seen:
            seen.add(h)
            out.append(cfg)

    # STAGE 1 (shard 0 / seed 42 only, so the other shards keep their full random
    # budget for breadth): the guaranteed-evaluated seeds. --resume skips these
    # once scored, so it costs the cell once. The widened random draw below cannot
    # otherwise reach entry_count=6 / max_hold=240 / tp1_delay=24 / risk=0.01.
    if bep_policy:
        # BEP cell: ONLY early-floor configs (no bep-OFF base), so "champion + floor"
        # is what this leaderboard is measuring against the separate base cell.
        if seed == 42:
            for cfg in sweep.sc24_bep_seed_grid():
                _add(cfg)
    else:
        _add(sweep.base_config_dict())  # DD40 base is itself a LIMIT config
        if seed == 42:
            for cfg in sweep.sc24_neighborhood_grid():
                _add(cfg)
    rng = random.Random(seed)
    attempts = 0
    while len(out) < max_candidates and attempts < max_candidates * 30:
        _add(sweep.candidate_config(rng, include_trend_runner=False,
                                    signal_policy=signal_policy, bep_policy=bep_policy))
        attempts += 1
    return out[:max_candidates]


def _monthly_stats(monthly: list[dict]) -> dict:
    """Consistency stats over fixed-lot no-bonus monthly trading P&L."""
    pnls = [float(m.get("trading_pnl") or 0.0) for m in monthly]
    total = len(pnls)
    stable = sum(1 for p in pnls if p > 0)
    worst = min(pnls) if pnls else 0.0
    return {
        "total_months": total,
        "stable_months": stable,
        "stable_fraction": (stable / total) if total else 0.0,
        "worst_month": worst,
    }


def evaluate_self_limit(cfg: dict, *, signals, chart, validate_signals, args) -> dict:
    """Evaluate one LIMIT candidate with fixed-lot edge + concurrent-DD gates."""
    candidate_id = sweep._json_hash({"preset": FILTER_LABEL, "config": cfg})

    # Edge + consistency on FIXED LOT (sizing-independent; no compounding mirage).
    fixed_cfg = dict(cfg)
    fixed_cfg["sizing_mode"] = "fixed"
    fixed_cfg["lot_per_entry"] = args.fixed_lot

    fixed_nb = sweep.run_concurrent_backtest(
        signals, chart, sweep.config_from_dict(fixed_cfg, bonus=0.0),
        exclude_structural_anomalies=args.exclude_structural_anomalies, label="fixed_nb_full")
    fixed_b = sweep.run_concurrent_backtest(
        signals, chart, sweep.config_from_dict(fixed_cfg),
        exclude_structural_anomalies=args.exclude_structural_anomalies, label="fixed_bonus_full")

    # Concurrent DD is measured under the candidate's own risk sizing -- that is
    # the live-relevant account drawdown the <=30% budget must hold.
    risk_full = sweep.run_concurrent_backtest(
        signals, chart, sweep.config_from_dict(cfg),
        exclude_structural_anomalies=args.exclude_structural_anomalies, label="risk_full")

    fixed_nb_val = (sweep.run_concurrent_backtest(
        validate_signals, chart, sweep.config_from_dict(fixed_cfg, bonus=0.0),
        exclude_structural_anomalies=args.exclude_structural_anomalies, label="fixed_nb_validate")
                    if validate_signals else None)

    edge = float(fixed_nb.get("net_profit") or 0.0)
    edge_bonus = float(fixed_b.get("net_profit") or 0.0)
    dd_concurrent = abs(float(risk_full.get("max_drawdown_pct") or 0.0))
    monthly = fixed_nb.get("monthly", [])
    ms = _monthly_stats(monthly)
    oos = float(fixed_nb_val.get("net_profit") or 0.0) if fixed_nb_val else None

    passes_dd = dd_concurrent <= args.max_concurrent_dd_pct
    passes_edge = edge > args.min_fixed_no_bonus_profit
    passes_consistency = (ms["total_months"] > 0
                          and ms["stable_fraction"] >= args.min_stable_month_fraction
                          and ms["worst_month"] >= args.worst_month_floor)
    passes_oos = (not validate_signals) or (oos is not None and oos > 0.0)
    passes = bool(passes_dd and passes_edge and passes_consistency and passes_oos)

    # Among survivors, reward both edge and consistency; add a small OOS term.
    score = edge * max(ms["stable_fraction"], 0.0) + 0.25 * (oos or 0.0)

    return {
        "candidate_id": candidate_id,
        "filter_preset": FILTER_LABEL,
        "passes_recommendation_gate": passes,
        "passes_dd": passes_dd,
        "passes_edge": passes_edge,
        "passes_consistency": passes_consistency,
        "passes_oos": passes_oos,
        "score": score,
        "fixed_no_bonus_profit": edge,
        "fixed_with_bonus_profit": edge_bonus,
        "bonus_contribution": edge_bonus - edge,
        "fixed_closed_lots": float(fixed_b.get("closed_lots") or 0.0),
        "concurrent_risk_max_dd_pct": dd_concurrent,
        "risk_net_profit_with_bonus": float(risk_full.get("net_profit") or 0.0),
        "stable_months": ms["stable_months"],
        "total_months": ms["total_months"],
        "stable_fraction": ms["stable_fraction"],
        "worst_month_pnl": ms["worst_month"],
        "oos_fixed_no_bonus_profit": oos,
        "config": cfg,
        "config_json": json.dumps(cfg, sort_keys=True),
        "monthly_json": json.dumps(monthly, default=str),
    }


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Self-archive LIMIT-only sweep: fixed-lot edge ranking, "
                    "concurrent-DD gate, monthly-consistency gate, fixed-lot OOS.")
    p.add_argument("--signals", default="signals/self_m15_archive.txt",
                   help="Self-generated signal archive (parsed directly; no provider filter).")
    p.add_argument("--charts", nargs="+", default=["data/XAUUSD_M1_*.csv"])
    p.add_argument("--output-dir", required=True)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--max-candidates", type=int, default=200)
    p.add_argument("--validate-months", type=int, default=6,
                   help="Held-out tail months for fixed-lot OOS.")
    p.add_argument("--max-concurrent-dd-pct", type=float, default=30.0)
    p.add_argument("--min-fixed-no-bonus-profit", type=float, default=0.0)
    p.add_argument("--min-stable-month-fraction", type=float, default=0.60,
                   help="Min fraction of months with positive fixed-lot no-bonus P&L.")
    p.add_argument("--worst-month-floor", type=float, default=-1e18,
                   help="Reject if any month's fixed-lot no-bonus P&L is below this (default off).")
    p.add_argument("--fixed-lot", type=float, default=0.01)
    p.add_argument("--exclude-structural-anomalies", action="store_true")
    p.add_argument("--top-n", type=int, default=20)
    p.add_argument("--resume", action="store_true")
    p.add_argument("--signal-policy", action="store_true",
                   help="Add provider-feed (Victor) signal R:R / SL-source "
                        "dimensions to the grid: ATR-vs-posted SL, TP rewrite, "
                        "R:R filter, nominal/effective reference. Off => the "
                        "scalper sweep's candidate space is unchanged.")
    p.add_argument("--bep-policy", action="store_true",
                   help="BEP early-floor variant: force the bep_plus_half_tp1 lock "
                        "model and draw bep_trigger_distance / bep_buffer ('+ small "
                        "points') / tp1_lock_fraction, seeded with the SC24 champion "
                        "+ early floor. Off => the scalper sweep's space is unchanged. "
                        "Run a base cell (no flag) and a bep cell (this flag) and "
                        "compare their winners on edge AND OOS.")
    p.add_argument("--progress-every", type=int, default=10)
    # Per-run locked-exit slippage override (backtest-realism, scoring only). The
    # sweep otherwise bakes the measured R4 value (2.0/1.0) into every candidate via
    # sweep.base_config_dict(); for a per-regime sweep, pass that regime's realistic
    # value (R3 0.9/0.45, R2 0.5/0.25, R1 0.4/0.2 from the volatility-scaled model).
    # < 0 means "leave the baked-in default unchanged". Never affects live orders.
    p.add_argument("--lock-tp1-slippage", type=float, default=-1.0,
                   help="override LOCK_TP1 exit slippage (pt) for ALL candidates; <0 keeps the default 2.0")
    p.add_argument("--lock-tp2-slippage", type=float, default=-1.0,
                   help="override LOCK_TP2 exit slippage (pt) for ALL candidates; <0 keeps the default 1.0")
    # Trailing-sweep cell: pin a fixed (open, close) trailing combo on every
    # candidate so the per-regime matrix enumerates all trailing combinations.
    # Both <0 => legacy no-trailing sweep (open pinned 0, close from the draw).
    p.add_argument("--trailing-open", type=float, default=-1.0,
                   help="pin trailing_open_distance on ALL candidates (a trailing-sweep cell); "
                        "<0 keeps the legacy LIMIT pin (0).")
    p.add_argument("--trailing-close", type=float, default=-1.0,
                   help="pin trailing_close_distance on ALL candidates; <0 keeps the candidate draw.")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    # Apply the per-regime locked-exit slippage override BEFORE candidates are
    # built: sweep.base_config_dict() reads these module globals at call time, so
    # every candidate (base, neighborhood, random) + the incumbent inherits it.
    if args.lock_tp1_slippage >= 0:
        sweep.SWEEP_LOCK_TP1_SLIPPAGE = args.lock_tp1_slippage
    if args.lock_tp2_slippage >= 0:
        sweep.SWEEP_LOCK_TP2_SLIPPAGE = args.lock_tp2_slippage
    print(f"[self-limit] locked-exit slippage scored at "
          f"TP1={sweep.SWEEP_LOCK_TP1_SLIPPAGE}/TP2={sweep.SWEEP_LOCK_TP2_SLIPPAGE} pt", flush=True)
    signals_path = Path(args.signals)
    if not signals_path.exists():
        raise SystemExit(f"signals file not found: {signals_path}")
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    checkpoint = output_dir / "results.jsonl"

    pin_to = args.trailing_open if args.trailing_open >= 0 else None
    pin_tc = args.trailing_close if args.trailing_close >= 0 else None
    candidates = make_limit_candidates(args.seed, args.max_candidates,
                                       signal_policy=args.signal_policy,
                                       bep_policy=args.bep_policy,
                                       pin_trailing_open=pin_to,
                                       pin_trailing_close=pin_tc)
    chart = CsvChartSource(sweep._expand_chart_paths(args.charts))
    signals = parse_signals_file(signals_path)
    train, validate = sweep.split_train_validate(signals, args.validate_months)
    trail_label = (f"trailing_open={pin_to if pin_to is not None else 0} pinned"
                   + (f", trailing_close={pin_tc} pinned" if pin_tc is not None
                      else ", trailing_close from draw"))
    print(f"[self-limit] candidates={len(candidates)} signals={len(signals)} "
          f"train={len(train)} validate={len(validate)} "
          f"({trail_label}; DD<= {args.max_concurrent_dd_pct}% concurrent; "
          f"consistency>= {args.min_stable_month_fraction})", flush=True)

    eval_args = SimpleNamespace(
        exclude_structural_anomalies=args.exclude_structural_anomalies,
        max_concurrent_dd_pct=args.max_concurrent_dd_pct,
        min_fixed_no_bonus_profit=args.min_fixed_no_bonus_profit,
        min_stable_month_fraction=args.min_stable_month_fraction,
        worst_month_floor=args.worst_month_floor,
        fixed_lot=args.fixed_lot,
    )

    existing = sweep.read_existing(checkpoint) if args.resume else {}
    all_rows = list(existing.values())

    for idx, cfg in enumerate(candidates, start=1):
        cid = sweep._json_hash({"preset": FILTER_LABEL, "config": cfg})
        if cid in existing:
            continue
        try:
            row = evaluate_self_limit(cfg, signals=signals, chart=chart,
                                      validate_signals=validate, args=eval_args)
        except Exception as exc:  # one bad candidate must not kill the sweep
            row = {"candidate_id": cid, "filter_preset": FILTER_LABEL,
                   "error": repr(exc), "passes_recommendation_gate": False,
                   "score": -1e18, "config": cfg,
                   "config_json": json.dumps(cfg, sort_keys=True)}
        sweep.write_jsonl(checkpoint, row)
        all_rows.append(row)
        if idx % max(1, args.progress_every) == 0:
            print(f"[self-limit] {idx}/{len(candidates)}", flush=True)

    csv_path, xlsx_path = sweep.write_leaderboards(all_rows, output_dir, args.top_n)
    print(f"[self-limit] checkpoint={checkpoint}")
    print(f"[self-limit] leaderboard_csv={csv_path}")
    print(f"[self-limit] leaderboard_xlsx={xlsx_path}")
    # top_configs/*.json carry the full config; their emitted live_command uses a
    # provider placeholder path -- deploy with --signals pointed at the archive.
    print(f"[self-limit] NOTE: deploy a winning config with "
          f"`--signals {signals_path}` (the config dict in top_configs is authoritative).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
