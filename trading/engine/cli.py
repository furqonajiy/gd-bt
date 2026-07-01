"""Command-line interface wrapper.

The full historical CLI implementation is preserved in ``cli_impl``.  This module
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

from . import cli_impl as _impl
from .cli_impl import *  # noqa: F401,F403 - preserve the original public CLI surface
from trading.engine.core import chart_tz
from trading.engine import ManualPositionSource, StrategyConfig
from trading.engine import PlannedOrder, compute_entries
from trading.engine import parse_signals_file as _default_parse_signals_file
from trading.engine import decide as _default_decide


AUTO_HEARTBEAT_SECONDS = 3600.0

# Module-level aliases make pytest monkeypatching straightforward while defaulting
# to the exact objects used by the original CLI.
parse_signals_file = _default_parse_signals_file
decide = _default_decide
ARCHIVE_DIR = _impl.ARCHIVE_DIR
ARCHIVE_MONTHS = _impl.ARCHIVE_MONTHS
_config_from_args = _impl._config_from_args
_make_notifier = _impl._make_notifier
_make_forensic = _impl._make_forensic
_print_reconcile_log = _impl._print_reconcile_log
_emit_per_signal_snapshots = _impl._emit_per_signal_snapshots
_handle_closures = _impl._handle_closures
_replay_tracked_signal = _impl._replay_tracked_signal
_is_partial_placement = _impl._is_partial_placement
_chart_now = _impl._chart_now

from .closure_report import report_entry_closures


def _restore_trailing_ladder_orders(placed_orders, replay_entries, planned_entries, side):
    """Rebuild the FULL trailing-open ladder for --trailing-live-entry.

    Keep the placeable orders and add a PlannedOrder for every replay leg the
    replay filtered out (it considers them "already played out"), so LIVE places
    the whole ladder. The missing legs use the PLANNED entry price
    (``planned_entries``) -- a played-out replay leg's ``entry_price`` is
    overwritten with its modelled fill, whereas SL/lot are not, so those come
    from the replay entry. Returns the orders sorted by entry_index.
    """
    have = {o.entry_index for o in placed_orders}
    full = list(placed_orders)
    for e in replay_entries:
        if e.entry_index in have:
            continue
        ep = (planned_entries[e.entry_index]
              if e.entry_index < len(planned_entries) else e.entry_price)
        full.append(PlannedOrder(
            entry_index=e.entry_index, side=side, entry_price=float(ep),
            initial_sl=float(e.initial_sl), lot=float(e.lot), risk_dollars=0.0))
    full.sort(key=lambda o: o.entry_index)
    return full


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

    from trading.engine import (
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


class _TeeStdout:
    """Write to the real stdout AND mirror to a text sink (best-effort).

    Wraps ``sys.stdout`` for the duration of the auto watch loop so every console
    line the operator sees is also persisted to disk, without touching any of the
    individual ``print`` sites. A sink failure never breaks the console output --
    trading observability must never take down trading."""

    def __init__(self, wrapped, sink) -> None:
        self._wrapped = wrapped
        self._sink = sink

    def write(self, data):
        n = self._wrapped.write(data)
        try:
            self._sink.write(data)
        except Exception:
            pass
        return n

    def flush(self):
        return self._wrapped.flush()

    def __getattr__(self, name):
        return getattr(self._wrapped, name)


def _make_console_log_sink(args: argparse.Namespace):
    """Build the RotatingTextLog from --console-log / --console-log-retain-hours,
    or None when --console-log is unset (opt-in; default path unchanged)."""
    path = getattr(args, "console_log", "") or ""
    if not path:
        return None
    from trading.engine.core.rotating_text import RotatingTextLog
    hours = float(getattr(args, "console_log_retain_hours", 24.0) or 0.0)
    return RotatingTextLog(path, retain_hours=hours)


def _run_auto_watch(args: argparse.Namespace, config: StrategyConfig,
                    conn, chart, signals_path: Path) -> int:
    interval = float(args.watch_interval)
    iteration = 0
    candidate_console_state: dict[str, str] = {}
    notified_keys: dict[str, set] = {"detected": set(), "skipped": set()}
    last_heartbeat = time.monotonic()
    _tag = getattr(args, "strategy_tag", "") or ""
    _adaptive = " | ADAPTIVE (regime auto-switch)" if _adaptive_enabled(args) else ""

    # Persist the console event stream to a time-windowed .txt so a terminal /
    # process crash still leaves the last N hours on disk to analyze. Opt-in via
    # --console-log; stdout is restored on exit so nothing else is affected.
    sink = _make_console_log_sink(args)
    orig_stdout = sys.stdout
    if sink is not None:
        sys.stdout = _TeeStdout(orig_stdout, sink)
        print(f"[auto] console log -> {sink.path} (keeping last {sink.retain_hours:g}h)")

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
    finally:
        sys.stdout = orig_stdout


def _adaptive_enabled(args: argparse.Namespace) -> bool:
    """--adaptive accepted as a bool (main CLI) or 'true'/'false' (auto_explicit)."""
    v = getattr(args, "adaptive", False)
    return v is True or str(v).lower() == "true"


def _maybe_adaptive_config(args: argparse.Namespace, base_config: StrategyConfig,
                           chart, console_state: dict) -> StrategyConfig:
    """In --adaptive mode, classify the current volatility regime from recent
    chart M1 and run that regime's published champion config
    (CHAMPION_<regime>.json under --champions-dir); fall back to ``base_config``
    (the CLI/incumbent config) when no champion exists or anything fails. Logs
    once per regime/source change so a switch is visible in the event log. Never
    raises -- a detection failure keeps the incumbent and the cycle continues."""
    import pandas as pd

    from trading.engine import champion_config, read_current_regime

    try:
        m1 = chart.dataframe[["time", "open", "high", "low", "close"]].set_index("time")
        window = int(getattr(args, "adaptive_window_days", 20) or 20)
        reading = read_current_regime(m1[m1.index >= m1.index.max() - pd.Timedelta(days=window)])
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
        print(f"[adaptive] regime={regime} (M15 ATR ${reading.m15_atr:.2f}, "
              f"trend {reading.trend:+.3f}) -> {source}")
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


def _format_terminal_reason(*, fired_chart, expiry_chart, hit_chart,
                            label: str, level: float, tag: str) -> str:
    """Human-readable 'why this signal is no longer valid' line for the live log.

    Instead of a bare 'signal already resolved', it says WHEN the signal fired,
    HOW LONG its entry window ran, and WHEN its original SL / final target was
    reached -- all in GMT+7 (the live-log zone) so it lines up with the feed and
    the operator's clock. ``expiry_chart`` may be None (no window shown)."""
    fired = chart_tz.to_log_tz(fired_chart)
    hit = chart_tz.to_log_tz(hit_chart)
    window = ""
    if expiry_chart is not None:
        expiry = chart_tz.to_log_tz(expiry_chart)
        window = f", entry window until {expiry:%Y-%m-%d %H:%M} GMT+7"
    return (f"not opened/re-armed -- fired {fired:%Y-%m-%d %H:%M} GMT+7{window}; "
            f"{label} {level:g} already reached at {hit:%Y-%m-%d %H:%M} GMT+7 "
            f"-> no longer valid ({tag})")


def _auto_pass(args: argparse.Namespace, config: StrategyConfig,
               conn, chart, signals_path: Path, iteration: int = 1,
               candidate_console_state: dict[str, str] | None = None,
               notified_keys: dict[str, set] | None = None) -> int:
    from trading.engine import (
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

    # Optional trailing-open LIVE entry: place the entry off the live price
    # instead of trusting the M1 replay's "already played out" verdict. Only for
    # trailing-open configs; the live-history gate (inside place_signal) keeps it
    # to one live trade per signal. Bool / "true"/"false" tolerant.
    _tle = getattr(args, "trailing_live_entry", False)
    trailing_live_entry = (
        (_tle is True or str(_tle).lower() == "true")
        and float(getattr(config, "trailing_open_distance", 0.0) or 0.0) > 0
    )

    # --- Stale / terminal live-signal protection (LIVE-ONLY, default safe) ------
    # A fast trailing strategy restarted late must NOT revive a signal whose
    # original context is gone (the 2026-07-01 TSL18 incident: 39 BUY legs from
    # 00:59-02:40 signals re-armed at 03:31 when price had fallen ~4030 -> ~4012,
    # all stopped out in ~15s by the 0.5 trailing-close). Two layers:
    #   1) TERMINAL-SL (always on, can't be disabled): if the original SL (or the
    #      final target) was already touched between the signal time and now, the
    #      signal is terminal -- never open OR re-arm it, and pull its pendings.
    #   2) LiveEntryGuard (opt-in via flags): age, current-price-vs-SL context,
    #      live RR, spread friction, and immediate-close (trailing-close inside
    #      spread/freeze) checks before any live placement.
    # Reviving REPLAY-played-out legs is gated behind an explicit dangerous flag,
    # default OFF -- so by default a played-out signal is never restored live.
    from trading.engine import LiveEntryGuard
    _arp = getattr(args, "allow_live_replay_played_out_legs", False)
    allow_replay_played_out = (_arp is True or str(_arp).lower() == "true")
    _freeze_price = 0.0
    try:
        _si = conn.mt5.symbol_info(args.mt5_symbol)
        if _si is not None:
            _freeze_price = max(int(getattr(_si, "trade_stops_level", 0) or 0),
                                int(getattr(_si, "freeze_level", 0) or 0)) * float(getattr(_si, "point", 0.01) or 0.01)
    except Exception:
        _freeze_price = 0.0
    live_guard = LiveEntryGuard.maybe(
        max_age_minutes=int(getattr(args, "max_live_signal_age_minutes", 0) or 0),
        min_rr=float(getattr(args, "min_live_entry_rr", 0.0) or 0.0),
        min_reward_distance=float(getattr(args, "min_live_entry_reward_distance", 0.0) or 0.0),
        max_spread_fraction_of_risk=float(getattr(args, "max_live_spread_fraction_of_risk", 0.0) or 0.0),
        trailing_close_distance=float(getattr(config, "trailing_close_distance", 0.0) or 0.0),
        freeze_distance=_freeze_price,
    )

    def _final_target_price(sig) -> float:
        return {"TP1": sig.tp1, "TP2": sig.tp2, "TP3": sig.tp3}.get(
            getattr(config, "final_target", "TP3"), sig.tp3)

    def _historical_touch(sig) -> tuple[datetime | None, datetime | None]:
        """(sl_touch_time, target_touch_time): the FIRST bar time in
        (signal_time, now] where the original SL / final target was touched, or
        None. BUY: low<=SL is a stop-out, high>=final a target hit; SELL mirrored.
        Signal-level TERMINAL test -- it overrides the leg-level replay."""
        ft = _final_target_price(sig)
        sl_time = tgt_time = None
        try:
            bars = chart.bars_between(sig.signal_time_chart, replay_end)
        except Exception:
            return (None, None)
        buy = sig.side.upper() == "BUY"
        for b in bars:
            if b.time <= sig.signal_time_chart:
                continue
            if buy:
                if sl_time is None and b.low <= sig.sl:
                    sl_time = b.time
                if tgt_time is None and b.high >= ft:
                    tgt_time = b.time
            else:
                if sl_time is None and b.high >= sig.sl:
                    sl_time = b.time
                if tgt_time is None and b.low <= ft:
                    tgt_time = b.time
            if sl_time is not None and tgt_time is not None:
                break
        return (sl_time, tgt_time)

    def _terminal_reason(sig) -> str | None:
        """Signal-level terminal reason (or None). Always active for live opens and
        re-arms: a signal whose original SL was already touched -- even if the
        engine is late and a fresh trailing-open price now looks attractive -- must
        be treated as stopped out, not revived. The reason names WHEN it fired, its
        entry window, and WHEN the SL/target was reached (see _format_terminal_reason)."""
        sl_time, tgt_time = _historical_touch(sig)
        if sl_time is None and tgt_time is None:
            return None
        expiry_min = int(getattr(config, "pending_expiry_minutes", 0) or 0)
        delay_min = int(getattr(config, "activation_delay_minutes", 0) or 0)
        expiry_chart = (sig.signal_time_chart + timedelta(minutes=delay_min + expiry_min)
                        if expiry_min else None)
        # A stop-out is the more important flag, so it wins if both were touched.
        if sl_time is not None:
            return _format_terminal_reason(
                fired_chart=sig.signal_time_chart, expiry_chart=expiry_chart,
                hit_chart=sl_time, label="original SL", level=float(sig.sl),
                tag="terminal_sl")
        return _format_terminal_reason(
            fired_chart=sig.signal_time_chart, expiry_chart=expiry_chart,
            hit_chart=tgt_time, label="final target",
            level=float(_final_target_price(sig)), tag="resolved")

    def _live_entry_skip(signal, rec) -> str | None:
        """Combined terminal + LiveEntryGuard decision for a would-be live placement.
        Returns a skip reason (already loggable) or None to place. ``planned_entry``
        is the LIVE price the trailing-open STOP would force-fill at."""
        treason = _terminal_reason(signal)
        if treason is not None:
            return treason
        if live_guard is None:
            return None
        orders = getattr(rec.new_signal, "orders", []) or []
        if not orders:
            return None
        o0 = orders[0]
        buy = signal.side.upper() == "BUY"
        planned_entry = (ask if buy else bid) if trailing_live_entry else float(o0.entry_price)
        if planned_entry <= 0:
            planned_entry = float(o0.entry_price)
        sl_time, tgt_time = _historical_touch(signal)
        age_min = max(0.0, (replay_end - signal.signal_time_chart).total_seconds() / 60.0)
        return live_guard.check(
            side=signal.side, planned_entry=planned_entry,
            effective_sl=float(o0.initial_sl), original_sl=float(signal.sl),
            tp1=float(signal.tp1), final_target=float(_final_target_price(signal)),
            age_minutes=age_min, bid=bid, ask=ask,
            sl_hit_after=sl_time is not None, target_hit_after=tgt_time is not None)

    def _record_live_skip(signal, reason, *, dedup):
        forensic.decision(signal_key=signal.signal_key, action="SKIP_STALE", rationale=reason)
        _auto_record_candidate_action(
            log, candidate_console_state, signal.signal_key,
            f"Signal {signal.signal_key}: {reason}.", dedup_text=f"{dedup}:{signal.signal_key}")
        if signal.signal_key not in notified_keys["skipped"]:
            notifier.signal_skipped(signal_key=signal.signal_key, side=signal.side, reason=reason)
            notified_keys["skipped"].add(signal.signal_key)

    # Signal-level TERMINAL state for already-TRACKED signals: if a tracked
    # signal's original SL (or final target) was touched historically, it is
    # terminal -- pull its resting pendings and EXCLUDE it from every self-heal
    # path below (replace-missing / reopen-missing / re-arm). This overrides the
    # leg-level replay verdict, so a partially-closed signal can't have its missing
    # legs recreated after it should already be stopped out (the #04 re-arm bug).
    terminal_tracked_keys: set[str] = set()
    for _ideal, actual, _exec_at in tracked:
        _sig = getattr(actual, "signal", None)
        if _sig is None:
            continue
        _tr = _terminal_reason(_sig)
        if _tr is not None:
            terminal_tracked_keys.add(_sig.signal_key)
            log.merge(executor.cancel_signal_pendings(_sig.signal_key, reason="terminal_sl"))
            # _tr already reads "not opened/re-armed -- ..."; don't double-prefix.
            _record_live_skip(_sig, _tr, dedup="TERMINAL")

    # Optional self-heal: re-place pending entries whose LIMITs vanished from MT5
    # (e.g. cancelled by hand) while the signal is still live. Terminal signals are
    # skipped (a missing order is NOT a reason to recreate a stopped-out signal).
    _rme = getattr(args, "replace_missing_entries", False)
    if _rme is True or str(_rme).lower() == "true":
        for _ideal, actual, _exec_at in tracked:
            if getattr(getattr(actual, "signal", None), "signal_key", None) in terminal_tracked_keys:
                continue
            log.merge(executor.replace_missing_pending_entries(actual, config, replay_end))

    # Optional mirror-the-replay: re-open positions for entries the replay still
    # holds OPEN but missing from MT5. Terminal signals are excluded.
    _rmp = getattr(args, "reopen_missing_positions", False)
    reopen_enabled = _rmp is True or str(_rmp).lower() == "true"
    executor._allow_partial_placement = reopen_enabled
    if reopen_enabled:
        for _ideal, actual, _exec_at in tracked:
            if getattr(getattr(actual, "signal", None), "signal_key", None) in terminal_tracked_keys:
                continue
            log.merge(executor.reopen_missing_open_positions(actual, config))

    # Optional provider edit/delete bridge: consume the listener's
    # signal_overrides journal so live MT5 follows the corrected feed. revoke =
    # flatten; amend = flatten + re-place corrected (close-and-reopen). Run before
    # the candidate pass so an amended signal (now untracked) is re-placed below
    # and a revoked one is held out of placement. Bool / "true"/"false" tolerant.
    _ase = getattr(args, "apply_signal_edits", False)
    revoked_keys: set[str] = set()
    if _ase is True or str(_ase).lower() == "true":
        revoked_keys = _impl._consume_signal_overrides(args, executor, registry, log=log)

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

    # Magics of signals tracked for reopen DURING this candidate pass. The prune
    # exemption below is built from `tracked` (the pre-loop registry state), so a
    # signal added this cycle for reopen -- e.g. a trailing partial ladder the
    # executor skips (placed=0, no MT5 footprint) but whose replay holds OPEN legs
    # -- would otherwise be pruned the same cycle, re-detected next cycle, and
    # re-added: an add/prune churn that re-logs its status every interval. Keeping
    # its magic alive lets it survive to be mirrored, exactly like a pre-loop
    # replay-OPEN signal.
    reopen_added_magics: set[int] = set()

    # One-time startup feed scan (printed on the first cycle): how many recent,
    # not-yet-tracked feed signals are still actionable vs already resolved. Makes
    # a cold start read "0 placeable right now" instead of looking broken -- a
    # fast-exit (e.g. trailing) strategy often has every recent signal already
    # played out by launch, and a restart never back-fills resolved signals.
    _startup_placeable = _startup_played_out = _startup_expired = 0

    # Deployment-safety gate (None unless a small-account gate is enabled, so the
    # default-OFF live path is byte-identical). This is the ACTIVE auto path --
    # cli.py overrides cli_impl.cmd_auto / _auto_pass -- so the gate MUST live here
    # to actually fire live; the SAME DeploymentGate predicates the backtest/tick
    # loop uses, fed from live state, so a TS2K/V817 tick backtest predicts live
    # placement decisions (the live/backtest parity contract). Gate-only REJECTS a
    # placement -- it never adds orders or touches geometry/lot/SLTP -- so it can
    # only reduce exposure. Order matches the backtest loop: daily-loss breaker ->
    # concurrency -> open-lots -> risk-budget.
    from trading.engine import DeploymentGate
    gate = DeploymentGate.maybe(config)
    placed_this_cycle = 0
    # Account-level realized P&L today + start-of-day equity. TS2K/V817 run one
    # strategy per small account, so the breaker is intentionally account-wide;
    # best-effort 0.0 without MT5 history (documented in SMALL_ACCOUNT_DEPLOYMENT.md).
    if gate is not None:
        gate_day_start = replay_end.replace(hour=0, minute=0, second=0, microsecond=0)
        gate_day_pnl = executor.realized_pnl_since(gate_day_start)
        # start-of-day equity ~= now minus what was realized today (floating P&L
        # ignored -- a coarse circuit-breaker basis, same approximation as cli_impl).
        gate_day_start_equity = equity - gate_day_pnl
    else:
        gate_day_pnl = 0.0
        gate_day_start_equity = equity

    def _gate_rejects(signal, rec) -> str | None:
        """Run DeploymentGate.live_check for a FOLLOW candidate using live state.
        Returns a reject reason (already logged) or None to place. open_groups is
        the currently-open tracked groups plus anything placed earlier this cycle;
        open_lots is the executor's live total volume. No-op when the gate is OFF."""
        if gate is None:
            return None
        open_groups = len(tracked) + placed_this_cycle
        planned_legs = [{"entry_price": o.entry_price, "effective_SL": o.initial_sl,
                         "lot": getattr(o, "lot", 0.0)}
                        for o in getattr(rec.new_signal, "orders", []) or []]
        greason = gate.live_check(
            planned_legs=planned_legs, equity=equity, open_groups=open_groups,
            day_realized_pnl=gate_day_pnl, day_start_equity=gate_day_start_equity,
            open_lots=executor.open_lots())
        if greason is not None:
            forensic.decision(signal_key=signal.signal_key, action="SKIP_GATE",
                              rationale=greason)
            _auto_record_candidate_action(
                log, candidate_console_state, signal.signal_key,
                f"Signal {signal.signal_key}: SKIPPED by deployment gate "
                f"({greason}); open_groups={open_groups}, equity=${equity:,.0f}.",
                dedup_text=f"SKIP_GATE:{greason}:{signal.signal_key}",
            )
        return greason

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

        _act = rec.new_signal.action
        if _act == "SKIP_INVALIDATED":
            _startup_played_out += 1
        elif _act == "SKIP_EXPIRED":
            _startup_expired += 1
        elif _act == "FOLLOW":
            _startup_placeable += 1

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
            # Trailing-open LIVE entry: the replay says every leg already played
            # out, but if LIVE never traded this signal and its pending window is
            # still open, place the trailing-open ladder off the live price and
            # let the broker run it. The history gate inside place_signal keeps it
            # to one live trade; the trailing-open arm logic only fills when the
            # live price is actually in the entry's pullback zone (no chasing).
            # DANGEROUS: reviving a replay-PLAYED-OUT signal is gated behind
            # --allow-live-replay-played-out-legs (default OFF). By default a
            # played-out signal is never restored live -- this is the primary fix
            # for the stale-revival incident. Even when explicitly allowed, the
            # terminal-SL + LiveEntryGuard checks still apply before placing.
            if trailing_live_entry and allow_replay_played_out:
                _magic = signal_to_magic(signal.signal_key)
                _exp = getattr(rec.new_signal, "pending_expires_at", None)
                _window_open = _exp is None or replay_end < _exp
                if (_window_open
                        and not executor.find_orders(_magic)
                        and not executor.find_positions(_magic)):
                    _skip = _live_entry_skip(signal, rec)
                    if _skip is not None:
                        _record_live_skip(signal, _skip, dedup="SKIP_STALE")
                        continue
                    if _gate_rejects(signal, rec) is not None:
                        continue
                    plog = executor.place_signal(signal, rec.new_signal)
                    if (getattr(plog, "placed", 0) > 0 or getattr(plog, "modified", 0) > 0
                            or getattr(plog, "cancelled", 0) > 0 or getattr(plog, "closed", 0) > 0):
                        log.merge(plog)
                    else:
                        for action in getattr(plog, "actions", []):
                            _auto_record_candidate_action(
                                log, candidate_console_state, signal.signal_key, action)
                        log.warnings.extend(getattr(plog, "warnings", []))
                    if getattr(plog, "placed", 0) > 0:
                        placed_this_cycle += 1
                        registry.add(signal, equity, executed_at=_chart_now())
                        candidate_console_state[signal.signal_key] = "PLACED"
                    continue
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

        # Trailing-open LIVE entry on a PARTIAL FOLLOW: the replay filtered out the
        # legs it considers "already played out", but LIVE never traded them -- so a
        # fast trailing signal permanently DROPS those legs (the reported #07 #1/#2
        # bug). Rebuild the FULL ladder off the replay's PLANNED entries and place
        # every leg as a trailing-open STOP. Uses compute_entries (deterministic
        # planned prices) for the missing legs, because a played-out replay leg's
        # entry_price is overwritten with its modelled fill; SL/lot are NOT
        # overwritten so they come from the replay entry. The history gate +
        # find_orders keep it to one live placement, and the trailing-open arm only
        # fills in the pullback zone (no chasing). Mirrors the SKIP_INVALIDATED
        # rescue, but per-leg. Gated on --trailing-live-entry AND the dangerous
        # --allow-live-replay-played-out-legs (default OFF) -- restoring played-out
        # legs is exactly the stale-revival behavior, so by default the dropped
        # played-out legs are NOT recreated; only genuinely-placeable legs place.
        if trailing_live_entry and allow_replay_played_out:
            _rp = getattr(rec.new_signal, "replay_position", None)
            if _rp is not None and len(rec.new_signal.orders) < len(_rp.entries):
                _before = len(rec.new_signal.orders)
                _full = _restore_trailing_ladder_orders(
                    rec.new_signal.orders, _rp.entries,
                    list(compute_entries(signal, config)), signal.side)
                _added = len(_full) - _before
                rec.new_signal.orders = _full
                # Announce the restore ONCE per signal per session. The restore
                # re-runs every cycle while the signal stays an untracked candidate
                # (placed=0 -- already resolved / waiting to arm), and the per-cycle
                # candidate_console_state key gets clobbered by place_signal's own
                # action later in the same cycle, so dedup_text there re-fired the
                # line every watch interval (and dragged the "Pruned" line along).
                # notified_keys persists and is never overwritten, so the line shows
                # once. The order rebuild + placement below are UNCHANGED -- log only.
                _tle_seen = notified_keys.setdefault("tle_full_ladder", set())
                if signal.signal_key not in _tle_seen:
                    _tle_seen.add(signal.signal_key)
                    log.actions.append(
                        f"Signal {signal.signal_key}: --trailing-live-entry -- restored "
                        f"{_added} replay-played-out leg(s) to place the FULL {len(_full)}-leg "
                        f"trailing-open ladder live.")

        # Deployment-safety gate: reject the FOLLOW (including a restored full
        # trailing ladder) before placement when today's loss limit is hit, the
        # concurrency cap is full, the open-lots ceiling would be breached, or the
        # planned min-lot risk is too big for live equity. Placed AFTER the ladder
        # rebuild so the planned legs the gate scores are the ones we would send.
        if _gate_rejects(signal, rec) is not None:
            continue

        # Stale / terminal / RR / immediate-close guard before placing a FOLLOW
        # (especially under --trailing-live-entry, where the STOP force-fills at the
        # live price): if the original SL/target was already touched, the live price
        # is through the original SL, the live RR is too thin, or a tight
        # trailing-close would instantly stop it out, do NOT open. No-op when no
        # guard flag is set and nothing is terminal, so a fresh signal is unaffected.
        _skip = _live_entry_skip(signal, rec)
        if _skip is not None:
            _record_live_skip(signal, _skip, dedup="SKIP_STALE")
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
        if getattr(plog, "placed", 0) > 0:
            placed_this_cycle += 1
        if getattr(plog, "placed", 0) > 0 or _track_for_reopen:
            executed_at = _chart_now()
            registry.add(signal, equity, executed_at=executed_at)
            candidate_console_state[signal.signal_key] = "PLACED"
            if _track_for_reopen and getattr(plog, "placed", 0) == 0:
                # No MT5 footprint this cycle -> protect it from the same-cycle
                # prune so it isn't churned in/out of the registry.
                reopen_added_magics.add(signal_to_magic(signal.signal_key))

    if iteration == 1 and candidates:
        log.actions.append(
            f"Startup feed scan: {len(candidates)} recent untracked signal(s) -> "
            f"{_startup_placeable} placeable (pending/open), "
            f"{_startup_played_out} already played out, "
            f"{_startup_expired} expired. Only placeable signals produce live "
            f"orders; a restart never back-fills resolved signals. If this is a "
            f"fast-exit strategy, keep the feed loop + executor running so new "
            f"signals are caught while still pending."
        )

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
        } | reopen_added_magics
    report_entry_closures(
        executor, notifier, tracked,
        ledger_path=registry_path.with_name("closed_deals.json"),
        server_offset_hours=args.mt5_server_offset,
    )
    _handle_closures(notifier, forensic, tracked, alive)
    removed = registry.prune(alive)
    if removed:
        # Prune is registry bookkeeping (clearing entries whose MT5 footprint is
        # gone), not a trade event. Surface it on the console ONLY when the cycle
        # ALSO produced real output -- so an idle executor that re-tracks then
        # prunes a just-finished signal every watch cycle (the reopen add/prune
        # churn, very visible at --watch-interval 1) stays silent instead of
        # printing "Pruned N closed signal(s)" every second. The prune still
        # happens; only the console line is withheld on an otherwise-idle cycle.
        cycle_had_output = bool(
            log.actions or log.warnings
            or log.placed or log.modified or log.cancelled or log.closed
        )
        if cycle_had_output:
            log.actions.append(
                f"Pruned {removed} closed signal(s) from {registry_path.name}"
            )

    # Cross-cycle console dedup: a warning or action identical to one already
    # shown is suppressed until it clears and recurs, so a steady passive notice
    # (the external-SL-change warning, a repeating "partial placement", a retried
    # "FAILED re-open ... Invalid price") prints ONCE instead of every watch cycle.
    # Real state transitions (placements, fills, lock moves, closes) carry unique
    # text so they still show, and a genuinely repeated event re-fires after it
    # clears. Forensic + notifier already received these at generation time, so
    # this only quiets the console -- a no-op cycle now prints nothing.
    shown_w = notified_keys.setdefault("shown_warnings", set())
    shown_a = notified_keys.setdefault("shown_actions", set())
    cur_w = list(getattr(log, "warnings", []) or [])
    cur_a = list(getattr(log, "actions", []) or [])
    log.warnings = [w for w in cur_w if w not in shown_w]
    log.actions = [a for a in cur_a if a not in shown_a]
    notified_keys["shown_warnings"] = set(cur_w)
    notified_keys["shown_actions"] = set(cur_a)

    if _execution_log_has_output(log):
        print(_stamped(render_execution_log(log)))

    forensic.end_cycle(placed=log.placed, modified=log.modified,
                       cancelled=log.cancelled, closed=log.closed)
    return 0


def _patch_original_auto_entrypoints() -> None:
    _impl.cmd_auto = cmd_auto
    _impl._run_auto_watch = _run_auto_watch
    _impl._auto_pass = _auto_pass


def build_parser() -> argparse.ArgumentParser:
    _patch_original_auto_entrypoints()
    return _impl.build_parser()


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


def __getattr__(name: str) -> Any:
    return getattr(_impl, name)


if __name__ == "__main__":
    raise SystemExit(main())