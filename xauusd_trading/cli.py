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


def _auto_skip_invalidated_detail_lines(rec: Any) -> list[str]:
    """Per-entry breakdown for a played-out signal: size, fill, close, move, $.

    Values come straight off the replay Position's entries; getattr keeps it
    robust to an entry that filled but has not closed (shown as 'still open').
    Console-only -- the notifier still gets the single-line header.
    """
    rp = getattr(rec.new_signal, "replay_position", None)
    if rp is None:
        return []
    side = getattr(getattr(rp, "signal", None), "side", "?")
    lines: list[str] = []
    for e in getattr(rp, "entries", []) or []:
        label = int(getattr(e, "entry_index", 0)) + 1
        lot = float(getattr(e, "lot", 0.0) or 0.0)
        head = f"  #{label} {side} {lot:g} lot"
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


def _auto_pass(args: argparse.Namespace, config: StrategyConfig,
               conn, chart, signals_path: Path, iteration: int = 1,
               candidate_console_state: dict[str, str] | None = None,
               notified_keys: dict[str, set] | None = None) -> int:
    from xauusd_trading import mt5_equity
    from xauusd_trading import (
        Mt5Executor, SignalRegistry, signal_to_magic,
        render_execution_log, ExecutionLog,
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
        _print_reconcile_log(reconcile_log)

    _emit_per_signal_snapshots(forensic, executor, tracked)

    errors = executor.sanity_checks(expected_equity=equity)
    if errors:
        print("SANITY CHECKS FAILED -- skipping MT5 actions this iteration:")
        for e in errors:
            print(f"  ! {e}")
        forensic.end_cycle(errors=1)
        return 0

    log = ExecutionLog()

    for _ideal, actual, _exec_at in tracked:
        mlog = executor.manage_position(actual, config, replay_end)
        log.merge(mlog)

    try:
        all_signals = parse_signals_file(signals_path)
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
                f"{rec.new_signal.pending_expires_at:%Y-%m-%d %H:%M} GMT+3 "
                f"(now {replay_end:%H:%M}). Skipped."
            )
            _auto_record_candidate_action(log, candidate_console_state, signal.signal_key, status)
            if signal.signal_key not in notified_keys["skipped"]:
                notifier.signal_skipped(signal_key=signal.signal_key, side=signal.side, reason=status)
                notified_keys["skipped"].add(signal.signal_key)
            continue

        if rec.new_signal.action == "SKIP_INVALIDATED":
            header = _auto_skip_invalidated_status_line(signal.signal_key, rec)
            detail = _auto_skip_invalidated_detail_lines(rec)
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

        if getattr(plog, "placed", 0) > 0:
            executed_at = _chart_now()
            registry.add(signal, equity, executed_at=executed_at)
            candidate_console_state[signal.signal_key] = "PLACED"

    known_magics = {
        signal_to_magic(item.get("signal_key", "?"))
        for item in registry.load()
    }
    log.warnings.extend(executor.warn_on_unknown(known_magics))

    alive = executor.all_alive_magics()
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
        print(render_execution_log(log))

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