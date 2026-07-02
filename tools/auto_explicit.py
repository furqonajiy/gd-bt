#!/usr/bin/env python3
"""Live auto runner that requires every strategy parameter explicitly.

Use this instead of ``python -m trading.engine.cli auto`` when running live.
The goal is safety: live execution must not silently depend on StrategyConfig
or parser defaults that may change during research.

Every strategy-critical field that changes live order behavior is required by
argparse. Fields that do NOT affect live execution are optional: --initial-capital
and --bonus-per-closed-lot (live sizes off real MT5 equity and pays no bonus --
these only feed the startup banner / backtest reports) and --tp3-lock-target
(only TP2 is supported). MT5 credentials, notifications and forensic logging are
optional operational flags.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from trading.engine import DEFAULT_CONFIG, StrategyConfig, parse_signals_file  # noqa: E402
from trading.engine.cli import ARCHIVE_DIR, ARCHIVE_MONTHS, _run_auto_watch  # noqa: E402


def _positive_int(raw: str) -> int:
    value = int(raw)
    if value < 0:
        raise argparse.ArgumentTypeError("must be >= 0")
    return value


def _positive_float(raw: str) -> float:
    value = float(raw)
    if value < 0:
        raise argparse.ArgumentTypeError("must be >= 0")
    return value


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Run live auto mode with no hidden strategy defaults.",
    )

    runtime = p.add_argument_group("runtime")
    runtime.add_argument("--signals", required=True)
    runtime.add_argument("--positions-json", required=True)
    runtime.add_argument("--watch-interval", type=float, required=True)
    runtime.add_argument("--strategy-tag", default="",
                         help="Per-executor namespace prefix stamped onto each signal's magic + "
                              "MT5 comment. Set DISTINCT values when running two auto executors on "
                              "one account (e.g. VIC vs SC24) so they never manage each other's "
                              "orders. Capped at 5 chars (first 5 kept); the compact per-entry/close "
                              "MT5 comment is independently clamped to the broker's 16-char limit.")
    runtime.add_argument("--no-clear", action="store_true")
    runtime.add_argument("--replace-missing-entries", choices=["true", "false"], default="false",
                         help="Each cycle, re-place pending LIMIT entries that vanished from MT5 "
                              "(e.g. cancelled by hand) while the signal is still live. Only entries "
                              "still PENDING (price hasn't reached them, window open) are restored; "
                              "no chasing. Requires >=1 existing MT5 footprint for the signal.")
    runtime.add_argument("--reopen-missing-positions", choices=["true", "false"], default="false",
                         help="Each cycle, re-open at market any entry the replay still holds OPEN "
                              "but that is missing from MT5 (e.g. closed by hand), with the replay's "
                              "lot, its current effective stop, and the leg's target — live keeps "
                              "mirroring the backtest until the replay itself exits the leg.")
    runtime.add_argument("--trailing-live-entry", choices=["true", "false"], default="false",
                         help="Trailing-open only: place the entry off the LIVE price instead of "
                              "the M1 backtest replay. A signal the replay marks 'already played out' "
                              "is still placed if LIVE never traded it (no closed deals for its magic) "
                              "and the pending window is open — the broker then fills the STOP + exits "
                              "on the SL. Once it trades and closes live, the history gate blocks "
                              "re-entry. Lets fast-exit trailing signals trade live; those fills are "
                              "broker/tick-driven, so demo-validate + calibrate before trusting parity.")

    stale = p.add_argument_group("live stale-signal protection (terminal-SL always on; guards default off)")
    stale.add_argument("--allow-live-replay-played-out-legs", choices=["true", "false"], default="false",
                       help="DANGEROUS, default OFF. When true, --trailing-live-entry may RESTORE "
                            "replay-played-out legs / revive a SKIP_INVALIDATED signal as fresh live "
                            "trailing-open STOPs. OFF (the safe default) means a played-out signal is "
                            "NEVER restored live. Do NOT enable in a production/live snapshot — it is "
                            "what caused the 2026-07-01 stale-revival incident.")
    stale.add_argument("--max-live-signal-age-minutes", type=_positive_int, default=0,
                       help="Skip placing/arming a live trailing-open order for a signal older than "
                            "this many minutes (separate from --pending-expiry, which is too broad for "
                            "live startup replay). 0=off. TSL18 live uses 20.")
    stale.add_argument("--min-live-entry-rr", type=_positive_float, default=0.0,
                       help="Reject a live entry whose reward(final)/risk at the price it would fill is "
                            "below this. 0=off.")
    stale.add_argument("--min-live-entry-reward-distance", type=_positive_float, default=0.0,
                       help="Reject a live entry whose TP1 reward distance (price units) is below this "
                            "(too thin vs friction). 0=off.")
    stale.add_argument("--max-live-spread-fraction-of-risk", type=_positive_float, default=0.0,
                       help="Reject a live entry when the current spread exceeds this fraction of the "
                            "planned risk (and block trailing-close that sits inside spread+freeze, the "
                            "immediate-close micro-trade). 0=off.")
    runtime.add_argument("--apply-signal-edits", choices=["true", "false"], default="false",
                         help="Consume the listener's signal-overrides journal each cycle: on a "
                              "provider EDIT flatten the live order and re-place it at the corrected "
                              "levels (close-and-reopen); on a DELETE flatten and untrack it. Enable "
                              "only on the executor whose feed the Telegram listener writes (e.g. the "
                              "VIC executor); leave 'false' for a self-feed scalper with no listener.")
    runtime.add_argument("--signal-overrides-file", default="signal_overrides.jsonl",
                         help="Path to the listener's append-only edit/delete journal (default: "
                              "signal_overrides.jsonl). Only read when --apply-signal-edits true; a "
                              "per-executor byte-offset sidecar next to it tracks consumption.")
    runtime.add_argument("--adaptive", choices=["true", "false"], default="false",
                         help="Auto-switch by regime: each cycle classify the current volatility "
                              "regime from recent chart M1 and run that regime's published champion "
                              "(CHAMPION_<regime>.json under --champions-dir); falls back to these "
                              "explicit strategy flags (the incumbent) when no champion exists yet.")
    runtime.add_argument("--champions-dir", default="champions",
                         help="Directory holding CHAMPION_<regime>.json for --adaptive.")
    runtime.add_argument("--adaptive-window-days", type=int, default=20,
                         help="Trailing window (days) of M1 used to classify the regime.")

    mt5 = p.add_argument_group("MT5")
    mt5.add_argument("--mt5-symbol", required=True)
    mt5.add_argument("--mt5-server-offset", type=int, required=True)
    mt5.add_argument("--mt5-history-bars", type=int, required=True)
    mt5.add_argument("--mt5-path", default=None)
    mt5.add_argument("--mt5-login", type=int, default=None)
    mt5.add_argument("--mt5-password", default=None)
    mt5.add_argument("--mt5-server", default=None)

    # Not live-execution parameters: live sizes off the real MT5 equity and pays
    # no bonus, so these only feed the startup banner / backtest reports. They are
    # OPTIONAL here (backtest_explicit.py still requires them) -- pass them only if
    # you want the banner to show a specific DD base.
    noexec = p.add_argument_group("not used for live execution (optional)")
    noexec.add_argument("--initial-capital", type=_positive_float,
                        default=DEFAULT_CONFIG.initial_capital,
                        help="DD/report base only; NOT used for live sizing (live uses MT5 equity).")
    noexec.add_argument("--bonus-per-closed-lot", type=_positive_float, default=0.0,
                        help="Backtest scoring only ($/closed lot); no effect on live orders.")

    strategy = p.add_argument_group("required strategy contract")
    strategy.add_argument("--sizing-mode", choices=["fixed", "risk"], required=True)
    strategy.add_argument("--lot", type=_positive_float, required=True)
    strategy.add_argument("--risk", type=_positive_float, required=True)
    strategy.add_argument("--minimum-lot", type=_positive_float, required=True)
    strategy.add_argument("--maximum-lot", type=float, default=0.0, help="Per-entry lot cap (0=no cap)")
    strategy.add_argument("--lot-step", type=_positive_float, required=True)
    strategy.add_argument("--entries", type=int, required=True)
    strategy.add_argument("--entry-ladder", choices=["signal_range_3", "range_uniform", "range_to_sl"], required=True)
    strategy.add_argument("--entry-sl-gap", type=_positive_float, required=True)
    strategy.add_argument("--activation-delay", type=_positive_int, required=True)
    strategy.add_argument("--pending-expiry", type=_positive_int, required=True)
    strategy.add_argument("--max-hold", type=_positive_int, required=True)
    strategy.add_argument("--sl-multiplier", type=_positive_float, required=True)
    strategy.add_argument("--final-target", choices=["TP1", "TP2", "TP3"], required=True)
    strategy.add_argument("--lock-after-tp1", choices=["true", "false"], required=True)
    strategy.add_argument("--lock-after-tp2", choices=["true", "false"], required=True)
    strategy.add_argument("--tp1-lock-delay-minutes", type=_positive_int, required=True)
    strategy.add_argument("--tp2-lock-delay-minutes", type=_positive_int, required=True)
    strategy.add_argument("--profit-lock-mode", choices=["tp_levels", "bep_plus_half_tp1"], required=True)
    strategy.add_argument("--bep-trigger-distance", type=_positive_float, required=True)
    strategy.add_argument("--tp1-lock-fraction", type=float, required=True)
    strategy.add_argument("--tp2-lock-target", choices=["TP1", "TP2"], required=True)
    strategy.add_argument("--runner-after-tp3", choices=["true", "false"], required=True)
    strategy.add_argument("--tp3-lock-target", choices=["TP2"], default="TP2",
                          help="Where a TP3-locked leg parks its stop; only TP2 is "
                               "supported, so this is fixed and optional.")
    strategy.add_argument("--trailing-open-distance", type=_positive_float, required=True,
                          help="Virtual trailing-open entry distance in price units; 0 disables.")
    strategy.add_argument("--trailing-close-distance", type=_positive_float, required=True,
                          help="Trailing-close (ratcheting) stop distance in price units; 0 disables.")
    strategy.add_argument("--trailing-close-min-step", type=_positive_float, default=0.0,
                          help="Live-only broker-traffic throttle: send the trailing-close SL modify "
                               "to MT5 only when the stop improves by at least this many price units "
                               "(0 = send every improvement). The engine still trails continuously.")

    scale = p.add_argument_group("optional scale-out exit (default off)")
    scale.add_argument("--scale-out-at-tp1", choices=["true", "false"], default="false",
                       help="At TP1 touch, close the worst open leg (furthest from signal SL). Needs >=2 filled legs.")
    scale.add_argument("--scale-out-at-tp2", choices=["true", "false"], default="false",
                       help="At TP2 touch, close the worst remaining open leg.")
    scale.add_argument("--bep-after-tp1", choices=["true", "false"], default="false",
                       help="At TP1, move remaining legs' stop to entry +/- --bep-buffer.")
    scale.add_argument("--bep-buffer", type=_positive_float, default=0.0,
                       help="Profit locked beyond entry (price units) when --bep-after-tp1; use >=0.40 for live.")
    scale.add_argument("--trailing-close-after-stage", type=_positive_int, default=0,
                       help="Trailing-close engages only at/after this stage (0=from open, 1=after TP1, 2=after TP2).")
    scale.add_argument("--runner-final-cap", choices=["auto", "tp3", "none"], default="auto",
                       help="auto (default) = a trailing-close strategy runs past the final target "
                            "(no broker TP; the trailing-close SL owns the exit) -- because trailing-close "
                            "IS the exit; tp3 = force the trailing remainder to bank at the final target "
                            "(keeps the broker TP); none = pure trail (explicit).")
    scale.add_argument("--shared-sl", choices=["true", "false"], default="false",
                       help="All entries share ONE stop level (anchored on the first entry) instead "
                            "of per-entry stops; risk-sizing uses each leg's real distance to it.")
    scale.add_argument("--entry-targets", default=None, metavar="T1,T2,...",
                       help="Per-entry targets, one per entry from {TP1,TP2,TP3,RUN}; RUN holds at "
                            "TP3 then trails by --trailing-close-distance. Empty = single --final-target.")
    scale.add_argument("--bep-after-move", type=_positive_float, default=0.0,
                       help="Per-leg break-even+ once a leg is this many price units in favour "
                            "(per-entry-targets mode); moves SL to entry +/- --bep-buffer. 0=off.")
    scale.add_argument("--runner-trail-from", choices=["TP1", "TP2", "TP3"], default="TP3",
                       help="RUN legs engage their trailing stop when this TP is touched (never "
                            "from entry), then trail by --trailing-close-distance. Default TP3.")

    gate = p.add_argument_group("small-account deployment-safety gates (default off)")
    gate.add_argument("--risk-budget-gate", choices=["true", "false"], default="false",
                      help="Reject a signal whose worst-case MIN-LOT risk is too big for live "
                           "equity. Needs --max-single-entry-risk-pct and/or --max-zone-risk-pct.")
    gate.add_argument("--max-single-entry-risk-pct", type=_positive_float, default=0.0,
                      help="Risk-budget cap: reject if ONE min-lot leg, stopped out, loses > this "
                           "fraction of equity (0.04=4%%). 0=disabled.")
    gate.add_argument("--max-zone-risk-pct", type=_positive_float, default=0.0,
                      help="Risk-budget cap: reject if the WHOLE min-lot ladder, stopped out, loses "
                           "> this fraction of equity (0.06=6%%). 0=disabled.")
    gate.add_argument("--daily-loss-limit-pct", type=_positive_float, default=0.0,
                      help="Daily-loss circuit breaker: once today's realized P&L reaches this "
                           "fraction of start-of-day equity in the red (0.05=5%%), stop placing NEW "
                           "signals for the rest of the day (open positions keep being managed). 0=off.")
    gate.add_argument("--max-open-signals", type=_positive_int, default=0,
                      help="Cap concurrent OPEN signal GROUPS (not entries); a multi-entry signal "
                           "counts as one. New signals are skipped while >= this many are open. 0=unlimited.")
    gate.add_argument("--max-open-lots", type=_positive_float, default=0.0,
                      help="Cap TOTAL concurrent open lots across ALL positions (ELEV8 broker "
                           "ceiling =100). A new signal whose ladder would push total open lots "
                           "over this is skipped. 0=unlimited.")

    col = p.add_argument_group("TSL18 collision policies (BACKTEST/SWEEP ONLY; live refuses non-baseline)")
    col.add_argument("--opposite-signal-policy",
                     choices=["allow_hedge", "reject_opposite", "profit_bank_rearm",
                              "close_then_flip", "reduce_then_hedge"],
                     default="allow_hedge",
                     help="Resolve a NEW signal opposite to an active one (default allow_hedge = "
                          "keep both). BACKTEST/SWEEP-ONLY research layer: live execution does "
                          "NOT yet enforce collision policy, so live `auto` REFUSES any "
                          "non-baseline value (anything other than allow_hedge) to avoid false "
                          "protection -- a separate demo-validated live implementation is "
                          "required first. See docs/TSL18_COLLISION_POLICIES.md.")
    col.add_argument("--same-side-overlap-policy",
                     choices=["allow_all", "reject_overlap", "scale_in_better_entry_only",
                              "scale_in_fixed_risk"],
                     default="allow_all",
                     help="Resolve a NEW same-side signal overlapping an active cluster "
                          "(default allow_all). BACKTEST/SWEEP-ONLY: live `auto` REFUSES any "
                          "non-baseline value (anything other than allow_all) -- live does not "
                          "enforce collision policy yet. reject/scale-in apply only in "
                          "backtest_explicit / the quality-entry sweep.")
    col.add_argument("--same-side-cluster-window-minutes", type=_positive_int, default=30,
                     help="Same-side signals within this many minutes form one cluster (default 30).")
    col.add_argument("--same-side-cluster-entry-gap", type=_positive_float, default=5.0,
                     help="Min price improvement for scale_in_better_entry_only (default 5.0).")
    col.add_argument("--same-side-cluster-sl-gap", type=_positive_float, default=10.0,
                     help="Reserved: min SL separation within a cluster (default 10.0).")
    col.add_argument("--max-cluster-risk-multiple", type=_positive_float, default=1.0,
                     help="Cluster risk must stay <= the cluster anchor's risk x this (default 1.0).")
    col.add_argument("--opposite-profit-threshold-r", type=_positive_float, default=0.5,
                     help="profit_bank_rearm banks the old side only at >= this many R (default 0.5).")
    col.add_argument("--hedge-lot-fraction", type=_positive_float, default=0.5,
                     help="reduce_then_hedge keeps this fraction of the old side's exposure (default 0.5).")

    obs = p.add_argument_group("observability")
    obs.add_argument("--notifications", default=None)
    obs.add_argument("--no-notifications", action="store_true")
    obs.add_argument("--forensic-log", default=None)
    obs.add_argument("--no-forensic", action="store_true")
    obs.add_argument("--console-log", default="",
                     help="Tee the live console event stream (Signal/EXECUTION/RECONCILIATION/"
                          "heartbeat lines) to this .txt file so a terminal/process crash still "
                          "leaves the recent history on disk to analyze. Keeps only the last "
                          "--console-log-retain-hours. Off unless set; use a per-strategy name "
                          "(e.g. console_v017.txt) like --forensic-log/--notifications.")
    obs.add_argument("--console-log-retain-hours", type=_positive_float, default=24.0,
                     help="How many hours of console log to keep in --console-log (older lines are "
                          "pruned on a slow cadence via an atomic rewrite). 0=unbounded. Default 24.")
    return p


_TARGET_TOKENS = {"TP1", "TP2", "TP3", "RUN"}


def _parse_entry_targets(raw: str | None, entries: int) -> tuple[str, ...]:
    if not raw:
        return ()
    toks = tuple(t.strip().upper() for t in raw.split(",") if t.strip())
    bad = [t for t in toks if t not in _TARGET_TOKENS]
    if bad:
        raise SystemExit(f"--entry-targets tokens must be TP1/TP2/TP3/RUN (got: {','.join(bad)})")
    if len(toks) != entries:
        raise SystemExit(f"--entry-targets needs one token per entry (--entries {entries}); got {len(toks)}")
    return toks


def _bool_text(raw: str) -> bool:
    return str(raw).strip().lower() == "true"


def config_from_args(args: argparse.Namespace) -> StrategyConfig:
    if args.entries < 1:
        raise SystemExit("--entries must be >= 1")
    if args.tp1_lock_fraction < 0 or args.tp1_lock_fraction > 1:
        raise SystemExit("--tp1-lock-fraction must be between 0 and 1")
    if args.sizing_mode == "risk" and args.risk <= 0:
        raise SystemExit("--risk must be > 0 when --sizing-mode risk")
    if args.sizing_mode == "fixed" and args.lot <= 0:
        raise SystemExit("--lot must be > 0 when --sizing-mode fixed")
    if not 0 <= args.trailing_close_after_stage <= 3:
        raise SystemExit("--trailing-close-after-stage must be between 0 and 3")

    return StrategyConfig(
        initial_capital=args.initial_capital,
        sizing_mode=args.sizing_mode,
        lot_per_entry=args.lot,
        risk_per_signal=args.risk,
        minimum_lot=args.minimum_lot,
        maximum_lot=args.maximum_lot,
        lot_step=args.lot_step,
        bonus_per_closed_lot=args.bonus_per_closed_lot,
        entry_count=args.entries,
        entry_ladder=args.entry_ladder,
        entry_sl_gap=args.entry_sl_gap,
        activation_delay_minutes=args.activation_delay,
        pending_expiry_minutes=args.pending_expiry,
        max_hold_minutes=args.max_hold,
        sl_multiplier=args.sl_multiplier,
        final_target=args.final_target,
        lock_after_tp1=_bool_text(args.lock_after_tp1),
        lock_after_tp2=_bool_text(args.lock_after_tp2),
        tp1_lock_delay_minutes=args.tp1_lock_delay_minutes,
        tp2_lock_delay_minutes=args.tp2_lock_delay_minutes,
        profit_lock_mode=args.profit_lock_mode,
        bep_trigger_distance=args.bep_trigger_distance,
        tp1_lock_fraction=args.tp1_lock_fraction,
        tp2_lock_target=args.tp2_lock_target,
        runner_after_tp3=_bool_text(args.runner_after_tp3),
        tp3_lock_target=args.tp3_lock_target,
        trailing_open_distance=args.trailing_open_distance,
        trailing_close_distance=args.trailing_close_distance,
        trailing_close_min_step=args.trailing_close_min_step,
        scale_out_at_tp1=_bool_text(args.scale_out_at_tp1),
        scale_out_at_tp2=_bool_text(args.scale_out_at_tp2),
        bep_after_tp1=_bool_text(args.bep_after_tp1),
        bep_buffer=args.bep_buffer,
        trailing_close_after_stage=args.trailing_close_after_stage,
        runner_no_final_cap=(args.runner_final_cap == "none"
                             or (args.runner_final_cap == "auto"
                                 and float(args.trailing_close_distance or 0.0) > 0)),
        shared_sl=_bool_text(args.shared_sl),
        per_entry_targets=_parse_entry_targets(args.entry_targets, args.entries),
        bep_after_move=args.bep_after_move,
        runner_trail_from=args.runner_trail_from,
        risk_budget_gate=_bool_text(getattr(args, "risk_budget_gate", "false")),
        max_single_entry_risk_pct=getattr(args, "max_single_entry_risk_pct", 0.0),
        max_zone_risk_pct=getattr(args, "max_zone_risk_pct", 0.0),
        daily_loss_limit_pct=getattr(args, "daily_loss_limit_pct", 0.0),
        max_open_signals=getattr(args, "max_open_signals", 0),
        max_open_lots=getattr(args, "max_open_lots", 0.0),
        opposite_signal_policy=getattr(args, "opposite_signal_policy", "allow_hedge"),
        same_side_overlap_policy=getattr(args, "same_side_overlap_policy", "allow_all"),
        same_side_cluster_window_minutes=getattr(args, "same_side_cluster_window_minutes", 30),
        same_side_cluster_entry_gap=getattr(args, "same_side_cluster_entry_gap", 5.0),
        same_side_cluster_sl_gap=getattr(args, "same_side_cluster_sl_gap", 10.0),
        max_cluster_risk_multiple=getattr(args, "max_cluster_risk_multiple", 1.0),
        opposite_profit_threshold_r=getattr(args, "opposite_profit_threshold_r", 0.5),
        hedge_lot_fraction=getattr(args, "hedge_lot_fraction", 0.5),
    )


def _validate_live_collision_policy(args: argparse.Namespace) -> None:
    """Refuse non-baseline TSL18 collision flags in LIVE auto.

    The collision layer (PR #329) is backtest/sweep-only: live execution does NOT
    enforce reject/downsize/flip/bank outcomes yet. Accepting a non-baseline flag
    here would let an operator believe the account is protected when it is not, so
    we hard-stop before connecting to MT5. (Backtests/sweeps pass these via
    backtest_explicit / sweep_tsl18_quality_entry, which DO apply the policy.)"""
    if (
        getattr(args, "opposite_signal_policy", "allow_hedge") != "allow_hedge"
        or getattr(args, "same_side_overlap_policy", "allow_all") != "allow_all"
    ):
        raise SystemExit(
            "TSL18 collision policies are currently backtest/sweep only. "
            "Live auto does not enforce non-baseline collision policies yet, so "
            "non-baseline --opposite-signal-policy / --same-side-overlap-policy "
            "are refused to avoid false protection."
        )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = config_from_args(args)
    # Live safety: collision policies are backtest/sweep-only; refuse non-baseline
    # flags BEFORE we touch MT5 (see _validate_live_collision_policy).
    _validate_live_collision_policy(args)

    signals_path = Path(args.signals)
    if not signals_path.exists():
        raise SystemExit(f"signals file not found: {signals_path}")
    parse_signals_file(signals_path)

    if args.watch_interval < 1.0:
        raise SystemExit("--watch-interval must be >= 1.0")

    from trading.engine import Mt5ChartSource, Mt5Connection, archive_m1_by_month, render_archive_summary

    conn = Mt5Connection(
        path=args.mt5_path,
        login=args.mt5_login,
        password=args.mt5_password,
        server=args.mt5_server,
    )
    conn.initialize()
    try:
        try:
            summary = archive_m1_by_month(
                conn,
                args.mt5_symbol,
                ARCHIVE_DIR,
                months_back=ARCHIVE_MONTHS,
                server_offset_hours=args.mt5_server_offset,
                overwrite=False,
            )
            print(render_archive_summary(summary))
            print()
        except Exception as e:
            print(f"[mt5] archive failed (continuing): {e}", file=sys.stderr)

        chart = Mt5ChartSource(
            conn,
            symbol=args.mt5_symbol,
            server_offset_hours=args.mt5_server_offset,
            history_bars=args.mt5_history_bars,
        )
        return _run_auto_watch(args, config, conn, chart, signals_path)
    finally:
        conn.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())