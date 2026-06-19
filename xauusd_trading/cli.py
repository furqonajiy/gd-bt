"""Command-line interface wrapper.

The full historical CLI implementation is preserved in ``cli_orig``.  This module
keeps every non-auto path delegated to that implementation and overrides only the
``auto`` console presentation so live auto mode is an append-only event log.
"""
from __future__ import annotations

import argparse
import sys
import time
import traceback
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from . import cli_orig as _orig
from .cli_orig import *  # noqa: F401,F403 - preserve the original public CLI surface
from xauusd_trading.core import chart_tz
from xauusd_trading import ManualPositionSource, StrategyConfig
from xauusd_trading import parse_signals_file as _default_parse_signals_file
from xauusd_trading import decide as _default_decide


AUTO_HEARTBEAT_SECONDS = 3600.0

# Module-level aliases make pytest monkeypatching straightforward while defaulting
# to the exact objects used by the original CLI.
parse_signals_file = _default_parse_signals_file
decide = _default_decide
ARCHIVE_DIR = _orig.ARCHIVE_DIR
ARCHIVE_MONTHS = _orig.ARCHIVE_MONTHS
_config_from_args = _orig._config_from_args
_make_notifier = _orig._make_notifier
_make_forensic = _orig._make_forensic
_print_reconcile_log = _orig._print_reconcile_log
_emit_per_signal_snapshots = _orig._emit_per_signal_snapshots
_handle_closures = _orig._handle_closures
_replay_tracked_signal = _orig._replay_tracked_signal
_is_partial_placement = _orig._is_partial_placement
_chart_now = _orig._chart_now

from .closure_report import report_entry_closures


def _execution_log_has_output(log: Any) -> bool:
    return bool(
        getattr(log, "actions", None)
        or getattr(log, "warnings", None)
        or getattr(log, "placed", 0) > 0
        or getattr(log, "modified", 0) > 0
        or getattr(log, "cancelled", 0) > 0
        or getattr(log, "closed", 0) > 0
        or getattr(log, "errors", None)
    )


def _remember_auto_status(
        state: dict[str, str], signal_key: str, text: str,
        dedup_text: str | None = None,
) -> bool:
    """Return True when this candidate status should be printed this cycle.

    ``dedup_text`` lets a status dedupe on a stable key while still displaying a
    line that contains volatile detail (e.g. a played-out signal whose replay
    realized P&L re-computes every cycle). When omitted, the display text is the
    key, preserving the prior behaviour.
    """
    key = dedup_text if dedup_text is not None else text
    if state.get(signal_key) == key:
        return False
    state[signal_key] = key
    return True


def _auto_skip_invalidated_status_line(signal_key: str, rec: Any) -> str:
    rp = rec.new_signal.replay_position
    if rp is None:
        return (
            f"Signal {signal_key}: every entry has already played out in "
            f"backtest replay -- no orders placed."
        )
    total = len(getattr(rp, "entries", []) or [])
    realized = rp.realized_pnl() if hasattr(rp, "realized_pnl") else None
    suffix = f" Backtest realized so far: ${realized:+.2f}." if realized is not None else ""
    return (
        f"Signal {signal_key}: every entry has already played out in "
        f"backtest replay -- no orders placed"
        f" ({total} entr{'y' if total == 1 else 'ies'} resolved).{suffix}"
    )


def _auto_skip_invalidated_detail_lines(signal_key: str, rec: Any) -> list[str]:
    """Per-entry breakdown for a played-out signal: size, fill, close, move, $.

    Values come straight off the replay Position's entries; getattr keeps it
    robust to an entry that filled but has not closed (shown as 'still open').
    Console-only -- the notifier still gets the single-line header.
    """
    rp = getattr(rec.new_signal, "replay_position", None)
    if rp is None:
        return []
    side = getattr(getattr(rp, "signal", None), "side", "?")
    # day_id from the signal_key ('...#02' -> '02') tags each line to the signal
    # and matches the .N suffix shown once a STOP is placed: #02.1, #02.2.
    day_tag = signal_key.rsplit("#", 1)[-1] if "#" in signal_key else None
    lines: list[str] = []
    for e in getattr(rp, "entries", []) or []:
        label = int(getattr(e, "entry_index", 0)) + 1
        entry_tag = f"#{day_tag}.{label}" if day_tag else f"#{label}"
        lot = float(getattr(e, "lot", 0.0) or 0.0)
        head = f"  {entry_tag} {side} {lot:g} lot"
        fill_time = getattr(e, "fill_time", None)
        if fill_time is None:
            lines.append(f"{head}  no fill | move -- | $0.00")
            continue
        entry_price = float(getattr(e, "entry_price", 0.0) or 0.0)
        status = getattr(e, "status", "?")
        pnl = getattr(e, "pnl", None)
        pnl_str = f"${(pnl if pnl is not None else 0.0):+.2f}"
        exit_time = getattr(e, "exit_time", None)
        exit_price = getattr(e, "exit_price", None)
        if exit_time is None or exit_price is None:
            lines.append(
                f"{head}  filled {fill_time:%H:%M:%S} @{entry_price:.2f} "
                f"-> still open ({status}) | move -- | {pnl_str}"
            )
            continue
        exit_price = float(exit_price)
        move = (exit_price - entry_price) if side == "BUY" else (entry_price - exit_price)
        lines.append(
            f"{head}  filled {fill_time:%H:%M:%S} @{entry_price:.2f} "
            f"-> closed {exit_time:%H:%M:%S} @{exit_price:.2f} {status} "
            f"| move {move:+.2f} | {pnl_str}"
        )
    return lines


def _auto_partial_placement_status_line(signal_key: str, rec: Any) -> str:
    rp = rec.new_signal.replay_position
    placed = len(rec.new_signal.orders)
    total = len(getattr(rp, "entries", []) or [])
    skipped = total - placed
    placed_ids = ", ".join(f"#{o.entry_index}" for o in rec.new_signal.orders)
    return (
        f"Signal {signal_key}: partial placement -- {placed} of {total} "
        f"entries placeable ({placed_ids}); {skipped} "
        f"entr{'y' if skipped == 1 else 'ies'} already played out in "
        f"backtest replay."
    )


def _auto_record_candidate_action(
        log: Any,
        state: dict[str, str],
        signal_key: str,
        text: str,
        dedup_text: str | None = None,
) -> None:
    if _remember_auto_status(state, signal_key, text, dedup_text):
        log.actions.append(text)


def _print_auto_startup_banner(args: argparse.Namespace,
                               config: StrategyConfig,
                               signals_path: Path) -> None:
    print("=" * 70)
    print("XAUUSD AUTO MODE  (event-only output)")
    print(f"Symbol:          {args.mt5_symbol}")
    print(f"Watch interval:  {float(args.watch_interval):g}s")
    print(f"Signals file:    {signals_path}")
    print("Strategy:")
    print(f"  initial_capital=${config.initial_capital:g}  sizing={config.sizing_mode}")
    print(f"  risk_per_signal={config.risk_per_signal:g}  entries={config.entry_count}")
    print(f"  entry_ladder={config.entry_ladder}  entry_sl_gap={config.entry_sl_gap:g}")
    print(f"  activation_delay={config.activation_delay_minutes}m  pending_expiry={config.pending_expiry_minutes}m")
    print(f"  max_hold={config.max_hold_minutes}m  sl_multiplier={config.sl_multiplier:g}")
    print(f"  final_target={config.final_target}  lock_after_tp1={config.lock_after_tp1}  lock_after_tp2={config.lock_after_tp2}")
    print(f"  trailing_open={getattr(config, 'trailing_open_distance', 0):g}  trailing_close={getattr(config, 'trailing_close_distance', 0):g}")
    print(f"  trend_runner={getattr(config, 'trend_runner_enabled', False)}")
    print("=" * 70)
    print()


def _print_auto_watch_heartbeat(iteration: int) -> None:
    print(f"[auto heartbeat #{iteration} -- {datetime.now():%Y-%m-%d %H:%M:%S} local]")


def cmd_auto(args: argparse.Namespace) -> int:
    """Continuous live trading with quiet, event-only console output."""
    config = _config_from_args(args)

    interval = float(args.watch_interval)
    if interval < 1.0:
        print(
            f"--watch-interval must be >= 1.0 (got {interval}). "
            f"5.0 is the recommended default. Aborting.",
            file=sys.stderr,
        )
        return 2
    if interval < 2.0:
        print(
            f"WARNING: --watch-interval {interval}s is aggressive. The "
            f"strategy is M1 so the worst-case reversal window is 60s; "
            f"5s gives a 12x safety margin and is the recommended default."
        )
        print()

    signals_path = Path(args.signals)
    if not signals_path.exists():
        print(f"signals file not found: {signals_path}", file=sys.stderr)
        return 2
    try:
        parse_signals_file(signals_path)
    except Exception as e:
        print(f"signals file failed to parse: {e}", file=sys.stderr)
        return 2

    from xauusd_trading import (
        Mt5ChartSource, Mt5Connection,
        archive_m1_by_month, render_archive_summary,
    )

    conn = Mt5Connection(
        path=args.mt5_path, login=args.mt5_login,
        password=args.mt5_password, server=args.mt5_server,
    )
    conn.initialize()

    try:
        try:
            summary = archive_m1_by_month(
                conn, args.mt5_symbol, ARCHIVE_DIR,
                months_back=ARCHIVE_MONTHS,
                server_offset_hours=args.mt5_server_offset,
                overwrite=False,
            )
            print(render_archive_summary(summary))
            print()
        except Exception as e:
            print(f"[mt5] archive failed (continuing): {e}", file=sys.stderr)

        chart = Mt5ChartSource(
            conn, symbol=args.mt5_symbol,
            server_offset_hours=args.mt5_server_offset,
            history_bars=args.mt5_history_bars,
        )

        _print_auto_startup_banner(args, config, signals_path)
        return _run_auto_watch(args, config, conn, chart, signals_path)
    finally:
        conn.shutdown()


def _run_auto_watch(args: argparse.Namespace, config: StrategyConfig,
                    conn, chart, signals_path: Path) -> int:
    interval = float(args.watch_interval)
    iteration = 0
    candidate_console_state: dict[str, str] = {}
    notified_keys: dict[str, set] = {"detected": set(), "skipped": set()}
    last_heartbeat = time.monotonic()
    _tag = getattr(args, "strategy_tag", "") or ""
    _adaptive = " | ADAPTIVE (regime auto-switch)" if _adaptive_enabled(args) else ""
    print(f"[auto] strategy_tag={_tag or '(none)'} | "
          f"positions={getattr(args, 'positions_json', '?')} | signals: {signals_path}{_adaptive}")
    try:
        while True:
            iteration += 1
            exit_code = _auto_pass(
                args, config, conn, chart, signals_path,
                iteration=iteration,
                candidate_console_state=candidate_console_state,
                notified_keys=notified_keys,
            )
            if exit_code != 0:
                return exit_code
            now_monotonic = time.monotonic()
            if now_monotonic - last_heartbeat >= AUTO_HEARTBEAT_SECONDS:
                _print_auto_watch_heartbeat(iteration)
                last_heartbeat = now_monotonic
            time.sleep(interval)
    except KeyboardInterrupt:
        print()
        print("Interrupted; exiting auto mode.")
        return 0


def _adaptive_enabled(args: argparse.Namespace) -> bool:
    """--adaptive accepted as a bool (main CLI) or 'true'/'false' (auto_explicit)."""
    v = getattr(args, "adaptive", False)
    return v is True or str(v).lower() == "true"


def _boolish(value) -> bool:
    return value is True or str(value).lower() == "true"


def _regime_thresholds_from_args(args: argparse.Namespace):
    """Optional custom regime thresholds; None means keep built-in defaults."""
    from xauusd_trading import RegimeThresholds, regime_thresholds_from_json_file

    path = getattr(args, "regime_thresholds_json", None)
    use_learned = _boolish(getattr(args, "regime_use_learned_boundaries", False))
    thresholds = (
        regime_thresholds_from_json_file(path, use_learned_boundaries=use_learned)
        if path else None
    )
    low = getattr(args, "regime_vol_tier_low_max", None)
    mid = getattr(args, "regime_vol_tier_mid_max", None)
    bull = getattr(args, "regime_bull_trend_min", None)
    if thresholds is None and low is None and mid is None and bull is None:
        return None
    base = thresholds or RegimeThresholds()
    return RegimeThresholds(
        vol_tier_low_max=base.vol_tier_low_max if low is None else float(low),
        vol_tier_mid_max=base.vol_tier_mid_max if mid is None else float(mid),
        bull_trend_min=base.bull_trend_min if bull is None else float(bull),
    )


def _maybe_adaptive_config(args: argparse.Namespace, base_config: StrategyConfig,
                           chart, console_state: dict) -> StrategyConfig:
    """In --adaptive mode, classify the current volatility regime from recent
    chart M1 and run that regime's published champion config
    (CHAMPION_<regime>.json under --champions-dir); fall back to ``base_config``
    (the CLI/incumbent config) when no champion exists or anything fails. Logs
    once per regime/source change so a switch is visible in the event log. Never
    raises -- a detection failure keeps the incumbent and the cycle continues."""
    import pandas as pd

    from xauusd_trading import champion_config, read_current_regime

    try:
        m1 = chart.dataframe[["time", "open", "high", "low", "close"]].set_index("time")
        window = int(getattr(args, "adaptive_window_days", 20) or 20)
        thresholds = _regime_thresholds_from_args(args)
        reading = read_current_regime(
            m1[m1.index >= m1.index.max() - pd.Timedelta(days=window)],
            thresholds=thresholds,
        )
    except Exception as e:  # pragma: no cover - defensive, live data shape varies
        if console_state.get("__regime__") != "ERR":
            print(f"[adaptive] regime detection failed ({e}); using incumbent config.")
            console_state["__regime__"] = "ERR"
        return base_config

    regime = reading.regime
    champ_dir = getattr(args, "champions_dir", "champions") or "champions"
    config = champion_config(regime, champ_dir, base_config)
    source = (f"champion {champ_dir}/CHAMPION_{regime}.json"
              if config is not base_config else "no champion yet; using incumbent")

    note = f"{regime}|{source}"
    if console_state.get("__regime__") != note:
        threshold_note = ""
        if thresholds is not None:
            threshold_note = (
                f", thresholds low<{thresholds.vol_tier_low_max:.2f} "
                f"mid<{thresholds.vol_tier_mid_max:.2f} "
                f"bull>={thresholds.bull_trend_min:.3f}"
            )
        print(f"[adaptive] regime={regime} (M15 ATR ${reading.m15_atr:.2f}, "
              f"trend {reading.trend:+.3f}{threshold_note}) -> {source}")
        console_state["__regime__"] = note
    return config


def _stamped(text: str) -> str:
    """Prefix every non-blank line with a local wall-clock stamp.

    Anchors each auto-cycle event block (EXECUTION / RECONCILIATION / sanity) in
    time so a live log -- e.g. the VIC and SQZ6 executors writing to the same
    console -- can be correlated. Matches the live feed loop's stamp format.
    """
    stamp = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] "
    return "\n".join(stamp + ln if ln.strip() else ln for ln in text.splitlines())


def _render_reconcile_log(reconcile_log) -> str:
    lines = ["RECONCILIATION:"]
    lines.extend(reconcile_log.actions)
    lines.extend(f"  ! {w}" for w in reconcile_log.warnings)
    return "\n".join(lines)


# Sanity-check failures (e.g. broker maintenance disabling trading) recur every
# watch interval -- a few seconds apart -- so an hours-long outage would print
# thousands of identical banners. Print at most once per this window, and again
# immediately if the set of failures changes so a NEW problem is never hidden.
_SANITY_REPRINT_SECONDS = 30 * 60


def _maybe_print_sanity_errors(state: dict, errors: list[str]) -> None:
    """Print the sanity-check banner, throttled to once per reprint window."""
    signature = " | ".join(errors)
    now = time.monotonic()
    last_at = state.get("__sanity_at__")
    changed = signature != state.get("__sanity_sig__")
    due = last_at is None or (now - float(last_at)) >= _SANITY_REPRINT_SECONDS
    if not (changed or due):
        return
    block = ["SANITY CHECKS FAILED -- skipping MT5 actions this iteration:"]
    block += [f"  ! {e}" for e in errors]
    block.append(f"  (identical warnings suppressed; reprints in "
                 f"{_SANITY_REPRINT_SECONDS // 60} min or when the reason changes)")
    print(_stamped("\n".join(block)))
    state["__sanity_at__"] = now
    state["__sanity_sig__"] = signature


def _auto_pass(args: argparse.Namespace, config: StrategyConfig,
               conn, chart, signals_path: Path, iteration: int = 1,
               candidate_console_state: dict[str, str] | None = None,
               notified_keys: dict[str, set] | None = None) -> int:
    from xauusd_trading import (
        Mt5Executor, SignalRegistry, signal_to_magic,
        render_execution_log, ExecutionLog, mt5_equity,
    )

    if candidate_console_state is None:
        candidate_console_state = {}
    if notified_keys is None:
        notified_keys = {"detected": set(), "skipped": set()}

    notifier = _make_notifier(args)
    forensic = _make_forensic(args)

    try:
        equity = mt5_equity(conn)
    except Exception as e:
        print(f"[mt5] account_info() failed: {e}", file=sys.stderr)
        forensic.error("auto_pass.mt5_equity", str(e), traceback.format_exc())
        return 2

    registry_path = Path(args.positions_json or "positions.json")
    registry = SignalRegistry(registry_path)
    prior_entries = registry.load()

    replay_end = chart.last_time()
    if replay_end is None:
        print("[mt5] no chart data available; skipping iteration")
        return 0

    # Regime auto-switch: classify the current market and swap in that regime's
    # published champion config before any placement/management this cycle.
    if _adaptive_enabled(args):
        config = _maybe_adaptive_config(args, config, chart, candidate_console_state)

    tracked = []
    for item in prior_entries:
        tracked.append(_replay_tracked_signal(item, chart, replay_end, config))

    bid, ask = 0.0, 0.0
    tick = conn.mt5.symbol_info_tick(args.mt5_symbol)
    if tick is not None and tick.bid > 0:
        bid = tick.bid
        ask = tick.ask if tick.ask > 0 else tick.bid
    else:
        last_bar = chart.latest()
        if last_bar is not None:
            bid = last_bar.close
            ask = last_bar.close + last_bar.spread_price

    executor = Mt5Executor(
        conn, args.mt5_symbol,
        min_lot=config.minimum_lot or 0.01,
        lot_step=config.lot_step or 0.01,
        server_offset_hours=args.mt5_server_offset,
        notifier=notifier,
        forensic=forensic,
    )

    forensic.start_cycle(
        subcommand="auto", iteration=iteration,
        chart_time=replay_end, equity=equity,
        bid=bid, ask=ask, tracked_count=len(tracked),
    )

    reconcile_log = ExecutionLog()
    for _ideal, actual, _exec_at in tracked:
        rlog = executor.reconcile_with_mt5(actual, config, chart, replay_end)
        reconcile_log.merge(rlog)
    if _execution_log_has_output(reconcile_log):
        print(_stamped(_render_reconcile_log(reconcile_log)))
        print()

    _emit_per_signal_snapshots(forensic, executor, tracked)

    errors = executor.sanity_checks(expected_equity=equity)
    if errors:
        _maybe_print_sanity_errors(candidate_console_state, errors)
        forensic.end_cycle(errors=1)
        return 0
    if candidate_console_state.pop("__sanity_sig__", None) is not None:
        candidate_console_state.pop("__sanity_at__", None)
        print(_stamped("[sanity] checks passing again -- resuming MT5 actions."))

    log = ExecutionLog()

    for _ideal, actual, _exec_at in tracked:
        mlog = executor.manage_position(actual, config, replay_end)
        log.merge(mlog)

    # Optional self-heal: re-place pending entries whose LIMITs vanished from MT5
    # (e.g. cancelled by hand) while the signal is still live. Accepts the flag as
    # a bool (main CLI store_true) or a "true"/"false" string (auto_explicit).
    _rme = getattr(args, "replace_missing_entries", False)
    if _rme is True or str(_rme).lower() == "true":
        for _ideal, actual, _exec_at in tracked:
            log.merge(executor.replace_missing_pending_entries(actual, config, replay_end))

    # Optional mirror-the-replay: re-open positions for entries the replay still
    # holds OPEN but that are missing from MT5 (typically closed by hand). Same
    # bool / "true"/"false" flag handling as above.
    _rmp = getattr(args, "reopen_missing_positions", False)
    reopen_enabled = _rmp is True or str(_rmp).lower() == "true"
    # In reopen/mirror mode, partially played-out signals are placed per entry
    # (fresh LIMITs now; already-OPEN legs re-opened by the reopen pass) instead
    # of being skipped wholesale. place_signal reads this flag.
    executor._allow_partial_placement = reopen_enabled
    if reopen_enabled:
        for _ideal, actual, _exec_at in tracked:
            log.merge(executor.reopen_missing_open_positions(actual, config))

    # Optional provider edit/delete bridge: consume the listener's
    # signal_overrides journal so live MT5 follows the corrected feed. revoke =
    # flatten; amend = flatten + re-place corrected (close-and-reopen). Run before
    # the candidate pass so an amended signal (now untracked) is re-placed below
    # and a revoked one is held out of placement. Bool / "true"/"false" tolerant.
    _ase = getattr(args, "apply_signal_edits", False)
    revoked_keys: set[str] = set()
    if _ase is True or str(_ase).lower() == "true":
        revoked_keys = _orig._consume_signal_overrides(args, executor, registry, log=log)

    try:
        all_signals = parse_signals_file(signals_path, tag=getattr(args, "strategy_tag", "") or "")
    except Exception as e:
        print(f"[signals] failed to parse {signals_path}: {e}")
        forensic.error("auto_pass.parse_signals", str(e), traceback.format_exc())
        all_signals = []

    existing_keys = {item.get("signal_key") for item in registry.load()}
    age_cutoff = replay_end - timedelta(minutes=config.pending_expiry_minutes + 5)

    candidates = [
        s for s in all_signals
        if s.signal_time_chart > age_cutoff
           and s.signal_key not in existing_keys
           and s.signal_key not in revoked_keys
    ]
    candidates.sort(key=lambda s: s.signal_time_chart)

    for signal in candidates:
        positions_source = ManualPositionSource(
            equity=equity,
            positions=[t[1] for t in tracked],
        )
        rec = decide(signal, chart, positions_source, config)

        forensic.decision(
            signal_key=signal.signal_key,
            action=rec.new_signal.action,
            rationale=getattr(rec.new_signal, "rationale", "") or "",
            is_partial=_is_partial_placement(rec),
        )

        if rec.new_signal.action == "SKIP_EXPIRED":
            status = (
                f"Signal {signal.signal_key}: pending window already closed at "
                f"{chart_tz.to_log_tz(rec.new_signal.pending_expires_at):%Y-%m-%d %H:%M} GMT+7 "
                f"(now {chart_tz.to_log_tz(replay_end):%H:%M} GMT+7). Skipped."
            )
            # The 'now HH:MM' clock changes every minute, so dedupe on a stable
            # per-signal key; expiry is terminal and only needs announcing once.
            _auto_record_candidate_action(
                log, candidate_console_state, signal.signal_key, status,
                dedup_text=f"SKIP_EXPIRED:{signal.signal_key}",
            )
            if signal.signal_key not in notified_keys["skipped"]:
                notifier.signal_skipped(signal_key=signal.signal_key, side=signal.side, reason=status)
                notified_keys["skipped"].add(signal.signal_key)
            continue

        if rec.new_signal.action == "SKIP_INVALIDATED":
            header = _auto_skip_invalidated_status_line(signal.signal_key, rec)
            detail = _auto_skip_invalidated_detail_lines(signal.signal_key, rec)
            console_status = "\n".join([header, *detail]) if detail else header
            # Played-out is terminal per signal; its line carries a replay
            # realized P&L that re-computes every cycle, so dedupe on a stable
            # key to announce once instead of re-printing the flapping number.
            _auto_record_candidate_action(
                log, candidate_console_state, signal.signal_key, console_status,
                dedup_text=f"SKIP_INVALIDATED:{signal.signal_key}",
            )
            if signal.signal_key not in notified_keys["skipped"]:
                # Notify with the single-line header only; the per-entry detail
                # is console-only and would bloat the Telegram message.
                notifier.signal_skipped(signal_key=signal.signal_key, side=signal.side, reason=header)
                notified_keys["skipped"].add(signal.signal_key)
            continue

        if _is_partial_placement(rec):
            status = _auto_partial_placement_status_line(signal.signal_key, rec)
            _auto_record_candidate_action(log, candidate_console_state, signal.signal_key, status)

        if signal.signal_key not in notified_keys["detected"]:
            entry_type = "STOP" if float(getattr(config, "trailing_open_distance", 0.0) or 0.0) > 0 else "LIMIT"
            entries = [{
                "entry_index": o.entry_index, "entry_type": entry_type,
                "entry_price": o.entry_price, "lot": o.lot, "sl": o.initial_sl,
                "tp1": signal.tp1, "tp2": signal.tp2, "tp3": signal.tp3,
            } for o in rec.new_signal.orders]
            notifier.signal_detected(
                signal_key=signal.signal_key, side=signal.side, entries=entries,
                activation_at=getattr(rec.new_signal, "pending_activates_at", None),
                expiry_at=getattr(rec.new_signal, "pending_expires_at", None),
                trailing={
                    "trailing_open_distance": getattr(config, "trailing_open_distance", 0.0),
                    "trailing_close_distance": getattr(config, "trailing_close_distance", 0.0),
                    "trend_runner_enabled": getattr(config, "trend_runner_enabled", False),
                },
            )
            notified_keys["detected"].add(signal.signal_key)

        plog = executor.place_signal(signal, rec.new_signal)
        if (
                getattr(plog, "placed", 0) > 0
                or getattr(plog, "modified", 0) > 0
                or getattr(plog, "cancelled", 0) > 0
                or getattr(plog, "closed", 0) > 0
        ):
            log.merge(plog)
        else:
            for action in getattr(plog, "actions", []):
                _auto_record_candidate_action(log, candidate_console_state, signal.signal_key, action)
            log.warnings.extend(getattr(plog, "warnings", []))

        # Track the signal when something was placed OR (in reopen/mirror mode)
        # when the replay still holds OPEN legs to restore -- so a partial signal
        # whose only live legs are price-passed still gets registered and the
        # reopen pass mirrors it next cycle.
        _replay_pos = getattr(rec.new_signal, "replay_position", None)
        _track_for_reopen = (
            reopen_enabled and _replay_pos is not None
            and any(e.status == "OPEN" for e in _replay_pos.entries)
        )
        if getattr(plog, "placed", 0) > 0 or _track_for_reopen:
            executed_at = _chart_now()
            registry.add(signal, equity, executed_at=executed_at)
            candidate_console_state[signal.signal_key] = "PLACED"

    known_magics = {
        signal_to_magic(item.get("signal_key", "?"))
        for item in registry.load()
    }
    log.warnings.extend(executor.warn_on_unknown(known_magics))

    alive = executor.all_alive_magics()
    if reopen_enabled:
        # Mirror-the-replay mode: a signal whose replay still holds OPEN legs
        # must survive the prune even with zero MT5 footprint (e.g. every leg
        # closed by hand and this cycle's re-open failed on a missing tick),
        # otherwise it disappears from the registry before it can be restored.
        alive = alive | {
            signal_to_magic(actual.signal.signal_key)
            for _ideal, actual, _exec_at in tracked
            if any(e.status == "OPEN" for e in actual.entries)
        }
    report_entry_closures(
        executor, notifier, tracked,
        ledger_path=registry_path.with_name("closed_deals.json"),
        server_offset_hours=args.mt5_server_offset,
    )
    _handle_closures(notifier, forensic, tracked, alive)
    removed = registry.prune(alive)
    if removed:
        log.actions.append(
            f"Pruned {removed} closed signal(s) from {registry_path.name}"
        )

    if _execution_log_has_output(log):
        print(_stamped(render_execution_log(log)))

    forensic.end_cycle(placed=log.placed, modified=log.modified,
                       cancelled=log.cancelled, closed=log.closed)
    return 0


def _patch_original_auto_entrypoints() -> None:
    _orig.cmd_auto = cmd_auto
    _orig._run_auto_watch = _run_auto_watch
    _orig._auto_pass = _auto_pass


def build_parser() -> argparse.ArgumentParser:
    _patch_original_auto_entrypoints()
    return _orig.build_parser()


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


def __getattr__(name: str) -> Any:
    return getattr(_orig, name)


if __name__ == "__main__":
    raise SystemExit(main())
