"""Command-line interface.

Subcommands:
    backtest    Run historical backtest. Auto-fetches recent M1 from MT5 first.
    decide      Decide on one signal. --execute places + manages on MT5.
    manage      Manage tracked signals only; no new placement. Supports --watch.
    auto        Continuous live trading: read signals.txt, execute, manage.
    mt5-info    Diagnostic: latest bar, equity, open MT5 objects.
    fetch       Pull recent M1 to per-month CSVs.

Reconciliation runs on every MT5-connected cycle (decide / manage / auto)
to patch PENDING entries the bar-replay missed (same-minute fills, positive
slippage). Backtest is unaffected.

Observability:
  --notifications PATH / --no-notifications -- engine -> listener Saved
      Messages event stream (forwarded by the Telegram listener).
  --forensic-log PATH  / --no-forensic       -- engine -> JSONL post-mortem
      log of every cycle, snapshot, and order_send. Inspect with
      `python tools/dump_forensic.py [...]`.
"""
from __future__ import annotations
import argparse
import glob
import json
import sys
import time
import traceback
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from xauusd_trading import CsvChartSource, ManualPositionSource
from xauusd_trading import run_backtest, write_backtest_outputs
from xauusd_trading import (
    CHART_TIMEZONE_OFFSET, CONTRACT_SIZE_OZ, DEFAULT_CONFIG, StrategyConfig,
)
from xauusd_trading import decide, format_replay_outcome, render_report
from xauusd_trading.core import chart_tz
from xauusd_trading import Position, advance_bars, open_position
from xauusd_trading import parse_one_signal, parse_signals_file
from xauusd_trading import (
    DEFAULT_NOTIFICATIONS_PATH, Notifier, summarize_closed_position,
)
from xauusd_trading import DEFAULT_FORENSIC_PATH, ForensicLog


ARCHIVE_DIR = "data"
ARCHIVE_MONTHS = 2


# ---------------------------------------------------------------------------
# path helpers
# ---------------------------------------------------------------------------

def _expand_chart_paths(patterns: list[str]) -> list[Path]:
    if not patterns:
        return []
    out: list[Path] = []
    for pat in patterns:
        if any(ch in pat for ch in "*?["):
            matches = sorted(glob.glob(pat))
            if not matches:
                raise SystemExit(f"No files match pattern: {pat}")
            out.extend(Path(m) for m in matches)
        else:
            p = Path(pat)
            if not p.exists():
                raise SystemExit(f"Chart file not found: {pat}")
            out.append(p)
    return out


def _try_archive_from_mt5(symbol: str, server_offset: int) -> None:
    """Best-effort archive pull. Soft-fail (warn and continue) on errors."""
    try:
        from xauusd_trading import (
            Mt5Connection, archive_m1_by_month, render_archive_summary,
        )
    except Exception as e:
        print(f"[mt5] skipped archive (import failed: {e})", file=sys.stderr)
        return
    try:
        with Mt5Connection() as conn:
            summary = archive_m1_by_month(
                conn, symbol, ARCHIVE_DIR,
                months_back=ARCHIVE_MONTHS,
                server_offset_hours=server_offset,
                overwrite=False,
            )
            print(render_archive_summary(summary))
            print()
    except Exception as e:
        print(f"[mt5] skipped archive ({e})", file=sys.stderr)


# ---------------------------------------------------------------------------
# observability helpers (notifier + forensic)
# ---------------------------------------------------------------------------

def _make_notifier(args: argparse.Namespace) -> Notifier:
    if getattr(args, "no_notifications", False):
        return Notifier(path=None)
    path = getattr(args, "notifications", None) or DEFAULT_NOTIFICATIONS_PATH
    return Notifier(path=path)


def _make_forensic(args: argparse.Namespace) -> ForensicLog:
    if getattr(args, "no_forensic", False):
        return ForensicLog(path=None)
    path = getattr(args, "forensic_log", None) or DEFAULT_FORENSIC_PATH
    return ForensicLog(path=path)


def _emit_per_signal_snapshots(forensic: ForensicLog, executor, tracked: list) -> None:
    """For each tracked signal, capture engine state + MT5 footprint."""
    if not forensic.enabled:
        return
    from xauusd_trading import signal_to_magic
    for _ideal, actual, _exec_at in tracked:
        magic = signal_to_magic(actual.signal.signal_key)
        forensic.engine_snapshot(actual)
        try:
            orders = executor.find_orders(magic)
            positions = executor.find_positions(magic)
            forensic.mt5_snapshot(actual.signal.signal_key, magic, orders, positions)
        except Exception as e:
            forensic.error("mt5_snapshot",
                           f"{type(e).__name__}: {e}",
                           traceback.format_exc())


def _handle_closures(notifier: Notifier, forensic: ForensicLog,
                     tracked: list, alive: set[int]) -> None:
    """For each tracked signal whose magic disappeared, emit notification
    and forensic event. Called BEFORE registry.prune so `tracked` still
    contains the soon-to-be-pruned signals."""
    from xauusd_trading import signal_to_magic
    for _ideal, actual, _exec_at in tracked:
        magic = signal_to_magic(actual.signal.signal_key)
        if magic in alive:
            continue
        summary, per_entry = summarize_closed_position(actual)
        realized = actual.realized_pnl()
        if notifier.path is not None:
            notifier.signal_closed(
                signal_key=actual.signal.signal_key,
                side=actual.signal.side,
                summary=summary,
                realized_pnl=realized,
                per_entry=per_entry,
            )
        if forensic.enabled:
            forensic.closure_detected(
                signal_key=actual.signal.signal_key,
                side=actual.signal.side,
                summary=summary,
                realized_pnl=realized,
                per_entry=per_entry,
            )


# ---------------------------------------------------------------------------
# tracked-signal replay (used by decide, manage, auto)
# ---------------------------------------------------------------------------

def _chart_now() -> datetime:
    """Wall-clock current time in chart timezone (EET/EEST), naive."""
    return chart_tz.utc_to_chart(datetime.now(timezone.utc).replace(tzinfo=None))


def _parse_executed_at(raw) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw))
    except Exception:
        return None


def _replay_tracked_signal(item: dict, chart, replay_end: datetime,
                           config: StrategyConfig
                           ) -> tuple[Position, Position, datetime | None]:
    """Replay one registry entry. Returns (pos_ideal, pos_actual, executed_at).

    pos_actual is a distinct Position only when executed_at is recorded
    AND it's later than activation_time; otherwise pos_actual is pos_ideal.
    """
    psig = parse_one_signal(item["signal"], item["date"], int(item["tz"]))
    equity_at_open = float(item.get("equity_at_open", 0.0))
    executed_at = _parse_executed_at(item.get("executed_at"))

    pos_ideal = open_position(psig, equity_at_open, config)
    advance_bars(
        pos_ideal,
        chart.bars_between(pos_ideal.activation_time, replay_end),
        config,
    )
    pos_ideal.executed_at = executed_at

    if executed_at is not None and executed_at > pos_ideal.activation_time:
        pos_actual = open_position(psig, equity_at_open, config)
        advance_bars(
            pos_actual,
            chart.bars_between(executed_at, replay_end),
            config,
        )
        pos_actual.executed_at = executed_at
    else:
        pos_actual = pos_ideal

    return pos_ideal, pos_actual, executed_at


def _print_reconcile_log(reconcile_log) -> None:
    if not reconcile_log.actions and not reconcile_log.warnings:
        return
    print("RECONCILIATION:")
    for a in reconcile_log.actions:
        print(a)
    for w in reconcile_log.warnings:
        print(f"  ! {w}")
    print()


def _is_partial_placement(rec) -> bool:
    """True iff this is a partial FOLLOW (some entries filtered by replay)."""
    if rec.new_signal.action != "FOLLOW":
        return False
    rp = rec.new_signal.replay_position
    if rp is None:
        return False
    return len(rec.new_signal.orders) < len(rp.entries)


def _partial_placement_log_lines(signal_key: str, rec) -> list[str]:
    """EXECUTION-log block describing a partial placement, with per-entry breakdown."""
    rp = rec.new_signal.replay_position
    placed = len(rec.new_signal.orders)
    total = len(rp.entries)
    skipped = total - placed
    placed_ids = ", ".join(f"#{o.entry_index}" for o in rec.new_signal.orders)
    header = (
        f"Signal {signal_key}: partial placement -- {placed} of {total} "
        f"entries placeable ({placed_ids}). The other "
        f"{skipped} entr{'y' if skipped == 1 else 'ies'} already played "
        f"out in backtest replay; only PENDING/OPEN entries are sent to MT5."
    )
    lines = [header]
    lines.extend(format_replay_outcome(rp, indent="  "))
    return lines


def _skip_invalidated_log_lines(signal_key: str, rec) -> list[str]:
    rp = rec.new_signal.replay_position
    lines = [
        f"Signal {signal_key}: every entry has already played out in "
        f"backtest replay -- no orders placed."
    ]
    if rp is not None:
        lines.extend(format_replay_outcome(rp, indent="  "))
    return lines


# ---------------------------------------------------------------------------
# manage/auto output formatters
# ---------------------------------------------------------------------------

def _format_lateness(executed_at: datetime, signal_time: datetime) -> str:
    delta_min = (executed_at - signal_time).total_seconds() / 60.0
    if delta_min >= 0.5:
        return f"({delta_min:.1f} min late)"
    if delta_min <= -0.5:
        return f"({-delta_min:.1f} min early)"
    return "(on time)"


def _entry_floating(entry, side: str, bid: float, ask: float,
                    contract_size: float = CONTRACT_SIZE_OZ) -> float:
    """Floating P&L for one OPEN entry against current bid/ask."""
    if entry.status != "OPEN":
        return 0.0
    if side == "BUY":
        return (bid - entry.entry_price) * entry.lot * contract_size
    return (entry.entry_price - ask) * entry.lot * contract_size


def _format_entry_line(entry, side: str, bid: float, ask: float,
                       contract_size: float, ideal_entry=None) -> str:
    """Format one entry line, with optional '[if on time: ...]' annotation."""
    if entry.status == "OPEN":
        floating = _entry_floating(entry, side, bid, ask, contract_size)
        base = (f"    #{entry.entry_index}  ({entry.entry_price:g})  OPEN     "
                f"lot={entry.lot:.2f}   floating ${floating:+.2f}")
    elif entry.status == "PENDING":
        base = (f"    #{entry.entry_index}  ({entry.entry_price:g})  PENDING  "
                f"lot={entry.lot:.2f}  (limit waiting)")
    elif entry.status == "NO_FILL":
        base = f"    #{entry.entry_index}  ({entry.entry_price:g})  NO_FILL"
    else:
        pnl_str = f"${entry.pnl:+.2f}" if entry.pnl is not None else "-"
        exit_str = f"@ {entry.exit_price:g}" if entry.exit_price is not None else "-"
        base = (f"    #{entry.entry_index}  ({entry.entry_price:g})  "
                f"{entry.status} {exit_str}  pnl={pnl_str}")

    if ideal_entry is None or ideal_entry.status == entry.status:
        return base

    if ideal_entry.status == "OPEN":
        ideal_floating = _entry_floating(ideal_entry, side, bid, ask, contract_size)
        time_str = (f"{ideal_entry.fill_time:%H:%M}"
                    if ideal_entry.fill_time is not None else "?")
        ann = f"   [if on time: OPEN since {time_str}, ${ideal_floating:+.2f}]"
    elif ideal_entry.status == "PENDING":
        ann = "   [if on time: still PENDING]"
    elif ideal_entry.status == "NO_FILL":
        ann = "   [if on time: NO_FILL]"
    else:
        pnl = ideal_entry.pnl
        pnl_str = f"${pnl:+.2f}" if pnl is not None else "?"
        time_str = (f"{ideal_entry.exit_time:%H:%M}"
                    if ideal_entry.exit_time is not None else "?")
        ann = f"   [if on time: {ideal_entry.status} at {time_str}, {pnl_str}]"
    return base + ann


def _format_position_body(
        pos: Position, now: datetime,
        bid: float, ask: float, contract_size: float,
        *, ideal_for_annotations: Position | None = None,
) -> tuple[list[str], float, float]:
    """Format the stage/fill/entries block for one Position view."""
    lines: list[str] = []

    if not pos.filled_entries():
        stage = "Pending (no fills yet)"
    elif pos.stage >= 1:
        stage = "Stage 2 (TP1 touched -- SL locked at TP1)"
    else:
        stage = "Stage 1 (initial SL active)"
    lines.append(f"    Stage:         {stage}")

    if pos.first_fill_time is not None:
        lines.append(f"    First fill:    {pos.first_fill_time}  GMT+3")
    if pos.time_exit_deadline is not None:
        delta_min = (pos.time_exit_deadline - now).total_seconds() / 60.0
        countdown = (
            f"({delta_min:+.0f} min)" if delta_min > 0 else
            f"(deadline passed by {-delta_min:.0f} min -- will close on next manage)"
        )
        lines.append(f"    Time-exit at:  {pos.time_exit_deadline}  GMT+3  {countdown}")

    side = pos.signal.side
    floating_total = 0.0
    for i, e in enumerate(pos.entries):
        ideal_e = (ideal_for_annotations.entries[i]
                   if ideal_for_annotations is not None else None)
        lines.append(_format_entry_line(e, side, bid, ask, contract_size, ideal_e))
        floating_total += _entry_floating(e, side, bid, ask, contract_size)

    realized_total = pos.realized_pnl()
    lines.append(
        f"    Floating:      ${floating_total:+.2f}    "
        f"Realized: ${realized_total:+.2f}"
    )
    return lines, floating_total, realized_total


def _format_position_status(
        pos_ideal: Position, pos_actual: Position,
        executed_at: datetime | None, now: datetime,
        bid: float, ask: float, contract_size: float,
) -> tuple[str, float, float]:
    """One tracked-signal block. Returns (text, floating, realized)."""
    s = pos_ideal.signal
    lines: list[str] = []

    lines.append(
        f"  {s.signal_key}  {s.side} {s.r1:g}-{s.r2:g}  "
        f"SL={s.sl:g} TP1={s.tp1:g} TP2={s.tp2:g} TP3={s.tp3:g}"
    )
    lines.append(f"    Issued:        {s.signal_time_chart}  GMT+3")

    if executed_at is not None:
        lines.append(
            f"    Executed:      {executed_at:%Y-%m-%d %H:%M:%S}  GMT+3  "
            f"{_format_lateness(executed_at, s.signal_time_chart)}"
        )

    lines.append(f"    Pending until: {pos_ideal.expiry_time}  GMT+3")

    # Dual-view replay when actual placement was meaningfully later than ideal.
    if pos_actual is not pos_ideal:
        lines.append("")
        lines.append(
            f"    If executed on time ({s.signal_time_chart:%H:%M} -- "
            f"the strategy's assumption):"
        )
        ideal_lines, ideal_floating, ideal_realized = _format_position_body(
            pos_ideal, now, bid, ask, contract_size,
        )
        for line in ideal_lines:
            lines.append("  " + line)

        lines.append("")
        lines.append(f"    Actual (executed {executed_at:%H:%M} -- what MT5 sees):")
        actual_lines, actual_floating, actual_realized = _format_position_body(
            pos_actual, now, bid, ask, contract_size,
            ideal_for_annotations=pos_ideal,
        )
        for line in actual_lines:
            lines.append("  " + line)

        ideal_pnl = ideal_floating + ideal_realized
        actual_pnl = actual_floating + actual_realized
        pnl_delta = actual_pnl - ideal_pnl

        ideal_fills = sum(1 for e in pos_ideal.entries if e.fill_time is not None)
        actual_fills = sum(1 for e in pos_actual.entries if e.fill_time is not None)
        fill_diff = actual_fills - ideal_fills
        if fill_diff < 0:
            fill_note = f", {-fill_diff} missed fill{'s' if -fill_diff != 1 else ''}"
        elif fill_diff > 0:
            fill_note = f", {fill_diff} extra fill{'s' if fill_diff != 1 else ''}"
        else:
            fill_note = ""

        lines.append("")
        lines.append(f"    Lateness cost so far: ${pnl_delta:+.2f}{fill_note}")
        lines.append(f"    Total (actual):       ${actual_pnl:+.2f}")
        return "\n".join(lines), actual_floating, actual_realized

    body_lines, floating_total, realized_total = _format_position_body(
        pos_ideal, now, bid, ask, contract_size,
    )
    lines.extend(body_lines)
    total = floating_total + realized_total
    lines.append(f"    Total:         ${total:+.2f}")
    return "\n".join(lines), floating_total, realized_total


# ---------------------------------------------------------------------------
# scenario helpers (backtest --scenario)
# ---------------------------------------------------------------------------

def _parse_scenario_arg(arg: str) -> dict:
    """Parse 'capital=1500,start=2026-05-19' into a scenario dict."""
    out: dict = {}
    for token in arg.split(","):
        token = token.strip()
        if not token:
            continue
        if "=" not in token:
            raise SystemExit(
                f"Bad --scenario token {token!r}; expected key=value pairs."
            )
        k, v = token.split("=", 1)
        k = k.strip().lower()
        v = v.strip()
        if k == "capital":
            try:
                out["capital"] = float(v)
            except ValueError:
                raise SystemExit(f"--scenario capital must be numeric, got {v!r}.")
        elif k == "start":
            try:
                out["start"] = datetime.strptime(v, "%Y-%m-%d").date()
            except ValueError:
                raise SystemExit(
                    f"--scenario start must be YYYY-MM-DD, got {v!r}."
                )
        else:
            raise SystemExit(
                f"Unknown --scenario key {k!r}; supported: capital, start."
            )
    if "capital" not in out:
        raise SystemExit("--scenario requires 'capital=' (e.g. capital=1500).")
    if "start" not in out:
        raise SystemExit("--scenario requires 'start=' (e.g. start=2026-05-19).")
    return out


def _filter_signals_by_start(signals: list, start_date: date) -> list:
    """Keep signals whose signal_time_chart (GMT+3) is at or after
    start_date 00:00 GMT+3.

    Boundary is chart-tz. A GMT+7 signal at 02:00 AM May 19 has
    signal_time_chart = May 18 22:00 GMT+3 and is excluded for
    start=2026-05-19.
    """
    threshold = datetime(start_date.year, start_date.month, start_date.day)
    return [s for s in signals if s.signal_time_chart >= threshold]


def _scenario_filename(scenario: dict) -> str:
    cap = scenario["capital"]
    cap_str = f"{int(cap)}" if cap == int(cap) else f"{cap:g}"
    return f"backtest_results_{cap_str}_{scenario['start'].isoformat()}.xlsx"


# ---------------------------------------------------------------------------
# subcommand: backtest
# ---------------------------------------------------------------------------

def cmd_backtest(args: argparse.Namespace) -> int:
    config = _config_from_args(args)
    _try_archive_from_mt5(args.mt5_symbol, args.mt5_server_offset)

    signals = parse_signals_file(Path(args.signals))
    chart = CsvChartSource(_expand_chart_paths(args.charts))

    # Default run: full signal set, configured initial capital.
    result = run_backtest(
        signals, chart, config,
        exclude_structural_anomalies=args.exclude_structural_anomalies,
    )
    summary = {k: v for k, v in result.items() if k not in {"rows", "entry_rows"}}
    print(json.dumps(summary, indent=2, default=str))
    if args.output_dir:
        path = write_backtest_outputs(result, Path(args.output_dir))
        print(f"\nWrote default run to {path.resolve()}", file=sys.stderr)

    # Extra scenarios: same strategy parameters, different capital and signal window.
    scenarios = [_parse_scenario_arg(s) for s in (args.scenario or [])]
    for scen in scenarios:
        scen_config = replace(config, initial_capital=scen["capital"])
        scen_signals = _filter_signals_by_start(signals, scen["start"])

        scen_label = f"capital=${scen['capital']:g}, start={scen['start'].isoformat()}"
        print(f"\n--- Scenario {scen_label} ---")
        print(f"Signals after filter: {len(scen_signals)} of {len(signals)}")
        if not scen_signals:
            print(
                "No signals remain after the start-date filter; "
                "skipping this scenario."
            )
            continue

        scen_result = run_backtest(
            scen_signals, chart, scen_config,
            exclude_structural_anomalies=args.exclude_structural_anomalies,
        )
        scen_summary = {
            k: v for k, v in scen_result.items()
            if k not in {"rows", "entry_rows"}
        }
        print(json.dumps(scen_summary, indent=2, default=str))
        if args.output_dir:
            fname = _scenario_filename(scen)
            path = write_backtest_outputs(
                scen_result, Path(args.output_dir), filename=fname,
            )
            print(f"Wrote scenario to {path.resolve()}", file=sys.stderr)

    return 0


# ---------------------------------------------------------------------------
# subcommand: decide
# ---------------------------------------------------------------------------

def cmd_decide(args: argparse.Namespace) -> int:
    config = _config_from_args(args)
    notifier = _make_notifier(args)
    forensic = _make_forensic(args)

    use_mt5 = bool(args.mt5) or bool(args.execute)
    conn = None
    executor = None
    ExecutionLog = None  # imported lazily; only available when use_mt5

    if use_mt5:
        from xauusd_trading import (
            Mt5ChartSource, Mt5Connection, mt5_equity,
            archive_m1_by_month, render_archive_summary,
        )
        from xauusd_trading import Mt5Executor, ExecutionLog
        conn = Mt5Connection(
            path=args.mt5_path, login=args.mt5_login,
            password=args.mt5_password, server=args.mt5_server,
        )
        conn.initialize()
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
        # Build executor up front so reconciliation can run before decide().
        executor = Mt5Executor(
            conn, args.mt5_symbol,
            min_lot=config.minimum_lot or 0.01,
            lot_step=config.lot_step or 0.01,
            server_offset_hours=args.mt5_server_offset,
            notifier=notifier,
            forensic=forensic,
        )
        equity = mt5_equity(conn) if (args.equity_from_mt5 or args.execute) else args.equity
    else:
        if not args.charts:
            raise SystemExit("Either --charts or --mt5 (or --execute) must be provided.")
        chart = CsvChartSource(_expand_chart_paths(args.charts))
        equity = args.equity

    signal = parse_one_signal(args.signal, args.signal_date, args.signal_tz)

    now = None
    if args.now:
        now = datetime.fromisoformat(args.now)

    registry_path = Path(args.positions_json or "positions.json")
    prior_entries: list[dict] = []
    if registry_path.exists():
        try:
            prior_entries = json.loads(registry_path.read_text(encoding="utf-8"))
        except Exception:
            prior_entries = []

    replay_end = now if now is not None else chart.last_time()

    tracked: list[tuple[Position, Position, datetime | None]] = []
    if prior_entries and replay_end is not None:
        for item in prior_entries:
            tracked.append(_replay_tracked_signal(item, chart, replay_end, config))

    forensic.start_cycle(
        subcommand="decide", iteration=1,
        chart_time=replay_end, equity=equity,
        tracked_count=len(tracked),
    )

    # Reconcile actual replay with MT5 reality. CSV-only mode skips.
    if executor is not None and tracked:
        reconcile_log = ExecutionLog()
        for _ideal, actual, _exec_at in tracked:
            rlog = executor.reconcile_with_mt5(actual, config, chart, replay_end)
            reconcile_log.merge(rlog)
        _print_reconcile_log(reconcile_log)

    # Forensic snapshots: engine + MT5 state per tracked signal, post-reconcile.
    if executor is not None and tracked:
        _emit_per_signal_snapshots(forensic, executor, tracked)

    open_positions = [t[1] for t in tracked]

    positions = ManualPositionSource(equity=equity, positions=open_positions)
    rec = decide(signal, chart, positions, config, now=now)
    print(render_report(rec))

    forensic.decision(
        signal_key=signal.signal_key,
        action=rec.new_signal.action,
        rationale=getattr(rec.new_signal, "rationale", "") or "",
    )

    if args.execute:
        from xauusd_trading import (
            SignalRegistry, signal_to_magic, render_execution_log,
        )

        print()
        errors = executor.sanity_checks(expected_equity=equity)
        if errors:
            print("SANITY CHECKS FAILED -- aborting execution:")
            for e in errors:
                print(f"  ! {e}")
            forensic.end_cycle(errors=1)
            conn.shutdown()
            return 2

        registry = SignalRegistry(registry_path)
        log = ExecutionLog()

        for pos in open_positions:
            mlog = executor.manage_position(pos, config, rec.generated_at)
            log.merge(mlog)

        known = {signal_to_magic(p.signal.signal_key) for p in open_positions}
        known.add(signal_to_magic(signal.signal_key))
        log.warnings.extend(executor.warn_on_unknown(known))

        if any(p.signal.signal_key == signal.signal_key for p in open_positions):
            log.actions.append(
                f"Signal {signal.signal_key} is already tracked; managed above."
            )
        elif rec.new_signal.action == "SKIP_EXPIRED":
            log.actions.append(
                f"Signal {signal.signal_key}: pending window already closed at "
                f"{rec.new_signal.pending_expires_at:%Y-%m-%d %H:%M} GMT+3 "
                f"(now {rec.generated_at:%Y-%m-%d %H:%M} GMT+3). "
                f"Skipped placement to avoid orders that would be cancelled immediately."
            )
        elif rec.new_signal.action == "SKIP_INVALIDATED":
            log.actions.append("\n".join(
                _skip_invalidated_log_lines(signal.signal_key, rec)
            ))
        else:
            # FOLLOW (possibly partial).
            if _is_partial_placement(rec):
                log.actions.append("\n".join(
                    _partial_placement_log_lines(signal.signal_key, rec)
                ))
            plog = executor.place_signal(signal, rec.new_signal)
            log.merge(plog)
            if plog.placed > 0:
                executed_at = _chart_now()
                registry.add(signal, equity, executed_at=executed_at)
                lateness = _format_lateness(executed_at, signal.signal_time_chart)
                log.actions.append(
                    f"Recorded executed_at = "
                    f"{executed_at:%Y-%m-%d %H:%M:%S} GMT+3 {lateness}"
                )

        alive = executor.all_alive_magics()
        _handle_closures(notifier, forensic, tracked, alive)
        removed = registry.prune(alive)
        if removed:
            log.actions.append(f"Pruned {removed} closed signal(s) from {registry_path.name}")

        print(render_execution_log(log))
        forensic.end_cycle(placed=log.placed, modified=log.modified,
                           cancelled=log.cancelled, closed=log.closed)
    else:
        forensic.end_cycle()

    if conn is not None:
        conn.shutdown()
    return 0


# ---------------------------------------------------------------------------
# subcommand: manage
# ---------------------------------------------------------------------------

def cmd_manage(args: argparse.Namespace) -> int:
    """Manage tracked signals on MT5. Reconciliation runs first so the
    dashboard reflects MT5 reality; with --execute, manage actions follow.
    """
    config = _config_from_args(args)

    if args.watch:
        interval = float(args.watch_interval)
        if interval < 1.0:
            print(
                f"--watch-interval must be >= 1.0 (got {interval}). "
                f"5.0 is the recommended default. Aborting.",
                file=sys.stderr,
            )
            return 2
        if interval < 2.0:
            # M1 strategy: worst-case reversal window is 60s, so 5s gives
            # a 12x safety margin. Anything below 2s is a soft warning.
            print(
                f"WARNING: --watch-interval {interval}s is aggressive. The "
                f"strategy is M1 so the worst-case reversal window is 60s; "
                f"5s gives a 12x safety margin and is the recommended default."
            )
            print()

    from xauusd_trading import (
        Mt5ChartSource, Mt5Connection, archive_m1_by_month, render_archive_summary,
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

        if args.watch:
            return _run_manage_watch(args, config, conn, chart)
        exit_code, _ = _manage_pass(args, config, conn, chart, iteration=1)
        return exit_code
    finally:
        conn.shutdown()


def _manage_pass(args: argparse.Namespace, config: StrategyConfig,
                 conn, chart, iteration: int = 1) -> tuple[int, int]:
    """One manage cycle. Returns (exit_code, n_alive_on_mt5)."""
    from xauusd_trading import mt5_equity
    from xauusd_trading import (
        Mt5Executor, SignalRegistry, signal_to_magic,
        render_execution_log, ExecutionLog,
    )

    notifier = _make_notifier(args)
    forensic = _make_forensic(args)

    try:
        equity = mt5_equity(conn)
    except Exception as e:
        print(f"[mt5] account_info() failed: {e}", file=sys.stderr)
        forensic.error("manage_pass.mt5_equity", str(e), traceback.format_exc())
        return 2, 0

    registry_path = Path(args.positions_json or "positions.json")
    if not registry_path.exists():
        print(f"No registry file at {registry_path.resolve()}; nothing to manage.")
        return 0, 0

    try:
        prior_entries = json.loads(registry_path.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"Could not read {registry_path}: {e}", file=sys.stderr)
        forensic.error("manage_pass.read_registry", str(e), traceback.format_exc())
        return 2, 0

    if not prior_entries:
        print(f"{registry_path.name} is empty; nothing to manage.")
        return 0, 0

    replay_end = chart.last_time()
    if replay_end is None:
        print("[mt5] no chart data available; aborting.", file=sys.stderr)
        forensic.error("manage_pass.no_chart_data", "chart.last_time() = None")
        return 2, 0

    tracked: list[tuple[Position, Position, datetime | None]] = []
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
    tracked_magics = {
        signal_to_magic(actual.signal.signal_key)
        for _ideal, actual, _exec_at in tracked
    }

    forensic.start_cycle(
        subcommand="manage", iteration=iteration,
        chart_time=replay_end, equity=equity,
        bid=bid, ask=ask, tracked_count=len(tracked),
    )

    # Reconcile before rendering so the dashboard matches MT5.
    reconcile_log = ExecutionLog()
    for _ideal, actual, _exec_at in tracked:
        rlog = executor.reconcile_with_mt5(actual, config, chart, replay_end)
        reconcile_log.merge(rlog)
    _print_reconcile_log(reconcile_log)

    # Snapshots (engine + MT5) per tracked signal, post-reconcile.
    _emit_per_signal_snapshots(forensic, executor, tracked)

    print("=" * 70)
    print("XAUUSD POSITION MANAGEMENT")
    print(f"Chart time:      {replay_end}  GMT+3")
    print(f"Account equity:  ${equity:,.2f}")
    print(f"Tracked signals: {len(tracked)}")
    if bid > 0:
        print(f"Live bid/ask:    {bid:g} / {ask:g}")
    print("=" * 70)

    total_floating = 0.0
    total_realized = 0.0
    for pos_ideal, pos_actual, executed_at in tracked:
        text, sig_floating, sig_realized = _format_position_status(
            pos_ideal, pos_actual, executed_at, replay_end,
            bid, ask, CONTRACT_SIZE_OZ,
        )
        print(text)
        print()
        total_floating += sig_floating
        total_realized += sig_realized

    print("=" * 70)
    print(f"TOTAL FLOATING P&L:  ${total_floating:+.2f}    (engine view)")
    print(f"TOTAL REALIZED P&L:  ${total_realized:+.2f}    (tracked signals only, ex-prune)")
    print(f"TOTAL COMBINED:      ${total_floating + total_realized:+.2f}")
    print("=" * 70)
    print()

    if args.execute:
        errors = executor.sanity_checks(expected_equity=equity)
        if errors:
            print("SANITY CHECKS FAILED -- aborting execution:")
            for e in errors:
                print(f"  ! {e}")
            forensic.end_cycle(errors=1)
            return 2, len(tracked_magics)

        registry = SignalRegistry(registry_path)
        log = ExecutionLog()

        for _ideal, actual, _exec_at in tracked:
            mlog = executor.manage_position(actual, config, replay_end)
            log.merge(mlog)

        log.warnings.extend(executor.warn_on_unknown(tracked_magics))

        alive = executor.all_alive_magics()
        _handle_closures(notifier, forensic, tracked, alive)
        removed = registry.prune(alive)
        if removed:
            log.actions.append(f"Pruned {removed} closed signal(s) from {registry_path.name}")

        print(render_execution_log(log))
        forensic.end_cycle(placed=log.placed, modified=log.modified,
                           cancelled=log.cancelled, closed=log.closed)
    else:
        alive = executor.all_alive_magics()
        print("(read-only -- pass --execute to apply changes to MT5)")
        forensic.end_cycle()

    n_alive = len(tracked_magics & alive)
    return 0, n_alive


def _run_manage_watch(args: argparse.Namespace, config: StrategyConfig,
                      conn, chart) -> int:
    interval = float(args.watch_interval)
    clear_screen = not args.no_clear
    iteration = 0
    try:
        while True:
            iteration += 1
            if clear_screen:
                sys.stdout.write("\x1b[H\x1b[J")
                sys.stdout.flush()
            else:
                print()
            print(
                f"[watch iter #{iteration} -- "
                f"{datetime.now():%Y-%m-%d %H:%M:%S} local -- "
                f"interval {interval:g}s]"
            )
            exit_code, n_alive = _manage_pass(args, config, conn, chart,
                                              iteration=iteration)
            if exit_code != 0:
                return exit_code
            if n_alive == 0:
                print()
                print(
                    "All tracked signals have no live MT5 footprint; "
                    "exiting watch mode."
                )
                return 0
            time.sleep(interval)
    except KeyboardInterrupt:
        print()
        print("Interrupted; exiting watch mode.")
        return 0


# ---------------------------------------------------------------------------
# subcommand: auto
# ---------------------------------------------------------------------------

def cmd_auto(args: argparse.Namespace) -> int:
    """Continuous live trading. Reconciles every iteration so engine state
    stays in lockstep with MT5 even when bars miss fills.
    """
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

        return _run_auto_watch(args, config, conn, chart, signals_path)
    finally:
        conn.shutdown()


def _run_auto_watch(args: argparse.Namespace, config: StrategyConfig,
                    conn, chart, signals_path: Path) -> int:
    interval = float(args.watch_interval)
    clear_screen = not args.no_clear
    iteration = 0
    try:
        while True:
            iteration += 1
            if clear_screen:
                sys.stdout.write("\x1b[H\x1b[J")
                sys.stdout.flush()
            else:
                print()
            print(
                f"[auto iter #{iteration} -- "
                f"{datetime.now():%Y-%m-%d %H:%M:%S} local -- "
                f"interval {interval:g}s -- signals: {signals_path}]"
            )
            exit_code = _auto_pass(args, config, conn, chart,
                                   signals_path, iteration=iteration)
            if exit_code != 0:
                return exit_code
            time.sleep(interval)
    except KeyboardInterrupt:
        print()
        print("Interrupted; exiting auto mode.")
        return 0


def _auto_pass(args: argparse.Namespace, config: StrategyConfig,
               conn, chart, signals_path: Path, iteration: int = 1) -> int:
    from xauusd_trading import mt5_equity
    from xauusd_trading import (
        Mt5Executor, SignalRegistry, signal_to_magic,
        render_execution_log, ExecutionLog,
    )

    notifier = _make_notifier(args)
    forensic = _make_forensic(args)

    # 1. Account equity.
    try:
        equity = mt5_equity(conn)
    except Exception as e:
        print(f"[mt5] account_info() failed: {e}", file=sys.stderr)
        forensic.error("auto_pass.mt5_equity", str(e), traceback.format_exc())
        return 2

    # 2. Registry.
    registry_path = Path(args.positions_json or "positions.json")
    registry = SignalRegistry(registry_path)
    prior_entries = registry.load()

    # 3. Chart state.
    replay_end = chart.last_time()
    if replay_end is None:
        print("[mt5] no chart data available; skipping iteration")
        return 0

    # 4. Replay each tracked signal (ideal + actual views).
    tracked: list[tuple[Position, Position, datetime | None]] = []
    for item in prior_entries:
        tracked.append(_replay_tracked_signal(item, chart, replay_end, config))

    # 5. Live bid/ask.
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

    # 6. Executor.
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

    # 7. Reconcile before rendering so the dashboard reflects MT5.
    reconcile_log = ExecutionLog()
    for _ideal, actual, _exec_at in tracked:
        rlog = executor.reconcile_with_mt5(actual, config, chart, replay_end)
        reconcile_log.merge(rlog)
    _print_reconcile_log(reconcile_log)

    # Snapshots (engine + MT5) per tracked signal, post-reconcile.
    _emit_per_signal_snapshots(forensic, executor, tracked)

    # 8. Dashboard header.
    print("=" * 70)
    print("XAUUSD AUTO MODE  (signals + management)")
    print(f"Chart time:      {replay_end}  GMT+3")
    print(f"Account equity:  ${equity:,.2f}")
    print(f"Tracked signals: {len(tracked)}")
    if bid > 0:
        print(f"Live bid/ask:    {bid:g} / {ask:g}")
    print("=" * 70)

    # 9. Per-signal status blocks.
    total_floating = 0.0
    total_realized = 0.0
    if tracked:
        for pos_ideal, pos_actual, executed_at in tracked:
            text, sig_floating, sig_realized = _format_position_status(
                pos_ideal, pos_actual, executed_at, replay_end,
                bid, ask, CONTRACT_SIZE_OZ,
            )
            print(text)
            print()
            total_floating += sig_floating
            total_realized += sig_realized

        print("=" * 70)
        print(f"TOTAL FLOATING P&L:  ${total_floating:+.2f}    (engine view)")
        print(f"TOTAL REALIZED P&L:  ${total_realized:+.2f}    (tracked signals only)")
        print(f"TOTAL COMBINED:      ${total_floating + total_realized:+.2f}")
        print("=" * 70)
    else:
        print("  (no tracked signals)")
        print("=" * 70)
    print()

    # 10. Sanity checks.
    errors = executor.sanity_checks(expected_equity=equity)
    if errors:
        print("SANITY CHECKS FAILED -- skipping MT5 actions this iteration:")
        for e in errors:
            print(f"  ! {e}")
        forensic.end_cycle(errors=1)
        return 0

    log = ExecutionLog()

    # 11. Manage tracked positions (drive from actual replay).
    for _ideal, actual, _exec_at in tracked:
        mlog = executor.manage_position(actual, config, replay_end)
        log.merge(mlog)

    # 12. Re-read signals.txt.
    try:
        all_signals = parse_signals_file(signals_path)
    except Exception as e:
        print(f"[signals] failed to parse {signals_path}: {e}")
        forensic.error("auto_pass.parse_signals", str(e), traceback.format_exc())
        all_signals = []

    # 13. Filter candidates (not expired, not already tracked).
    existing_keys = {item.get("signal_key") for item in registry.load()}
    age_cutoff = replay_end - timedelta(minutes=config.pending_expiry_minutes + 5)

    candidates = [
        s for s in all_signals
        if s.signal_time_chart > age_cutoff
           and s.signal_key not in existing_keys
    ]
    candidates.sort(key=lambda s: s.signal_time_chart)

    # 14. Process candidates.
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
            log.actions.append(
                f"Signal {signal.signal_key}: pending window already closed at "
                f"{rec.new_signal.pending_expires_at:%Y-%m-%d %H:%M} GMT+3 "
                f"(now {replay_end:%H:%M}). Skipped."
            )
            continue

        if rec.new_signal.action == "SKIP_INVALIDATED":
            log.actions.append("\n".join(
                _skip_invalidated_log_lines(signal.signal_key, rec)
            ))
            continue

        # FOLLOW (possibly partial).
        if _is_partial_placement(rec):
            log.actions.append("\n".join(
                _partial_placement_log_lines(signal.signal_key, rec)
            ))

        plog = executor.place_signal(signal, rec.new_signal)
        log.merge(plog)

        if plog.placed > 0:
            executed_at = _chart_now()
            registry.add(signal, equity, executed_at=executed_at)
            lateness = _format_lateness(executed_at, signal.signal_time_chart)
            log.actions.append(
                f"Signal {signal.signal_key}: recorded executed_at = "
                f"{executed_at:%Y-%m-%d %H:%M:%S} GMT+3 {lateness}"
            )

    # 15. Unknown-position warnings.
    known_magics = {
        signal_to_magic(item.get("signal_key", "?"))
        for item in registry.load()
    }
    log.warnings.extend(executor.warn_on_unknown(known_magics))

    # 16. Detect closures, then prune.
    alive = executor.all_alive_magics()
    _handle_closures(notifier, forensic, tracked, alive)
    removed = registry.prune(alive)
    if removed:
        log.actions.append(
            f"Pruned {removed} closed signal(s) from {registry_path.name}"
        )

    # 17. Execution log.
    has_actions = (
            log.actions or log.warnings
            or log.placed > 0 or log.modified > 0
            or log.cancelled > 0 or log.closed > 0
    )
    if has_actions:
        print(render_execution_log(log))

    forensic.end_cycle(placed=log.placed, modified=log.modified,
                       cancelled=log.cancelled, closed=log.closed)
    return 0


# ---------------------------------------------------------------------------
# subcommand: mt5-info
# ---------------------------------------------------------------------------

def cmd_mt5_info(args: argparse.Namespace) -> int:
    from xauusd_trading import (
        Mt5ChartSource, Mt5Connection, mt5_equity, mt5_open_positions_summary,
    )
    with Mt5Connection(
            path=args.mt5_path, login=args.mt5_login,
            password=args.mt5_password, server=args.mt5_server,
    ) as conn:
        chart = Mt5ChartSource(
            conn, symbol=args.mt5_symbol,
            server_offset_hours=args.mt5_server_offset,
        )
        last = chart.latest()
        print(f"Symbol:           {args.mt5_symbol}")
        print(f"Server offset:    GMT+{args.mt5_server_offset}")
        print(f"Latest bar:       {last.time if last else '(none)'}  "
              f"close={last.close if last else '-'}  "
              f"spread={last.spread_points if last else '-'} pts")
        try:
            print(f"Account equity:   ${mt5_equity(conn):,.2f}")
        except Exception as e:
            print(f"Account equity:   <error: {e}>")
        print()
        print("Open MT5 positions / pending orders for the symbol:")
        rows = mt5_open_positions_summary(conn, args.mt5_symbol)
        if not rows:
            print("  (none)")
        for r in rows:
            print(f"  [{r['kind']}] #{r['ticket']}  {r['type']}  "
                  f"vol={r['volume']}  open={r['price_open']}  "
                  f"sl={r['sl']}  tp={r['tp']}  comment={r['comment']!r}")
    return 0


def cmd_fetch(args: argparse.Namespace) -> int:
    from xauusd_trading import (
        Mt5Connection, archive_m1_by_month, render_archive_summary,
    )
    with Mt5Connection(
            path=args.mt5_path, login=args.mt5_login,
            password=args.mt5_password, server=args.mt5_server,
    ) as conn:
        summary = archive_m1_by_month(
            conn, args.mt5_symbol, ARCHIVE_DIR,
            months_back=getattr(args, "months", ARCHIVE_MONTHS),
            server_offset_hours=args.mt5_server_offset,
            overwrite=False,
        )
        print(render_archive_summary(summary))
    return 0


# ---------------------------------------------------------------------------
# argparse
# ---------------------------------------------------------------------------

def _add_strategy_overrides(p: argparse.ArgumentParser) -> None:
    p.add_argument("--initial-capital", type=float, default=DEFAULT_CONFIG.initial_capital)
    p.add_argument("--risk", type=float, default=DEFAULT_CONFIG.risk_per_signal)
    p.add_argument("--entries", type=int, default=DEFAULT_CONFIG.entry_count,
                   help="Number of entry slots per signal (>=1). Default uses validated config.")
    p.add_argument("--entry-ladder", default=DEFAULT_CONFIG.entry_ladder,
                   choices=["range_uniform", "range_to_sl"],
                   help="How to space entries: within the signal range or extended toward SL.")
    p.add_argument("--entry-sl-gap", type=float, default=DEFAULT_CONFIG.entry_sl_gap,
                   help="Dollars between deepest entry and signal SL (range_to_sl only).")

    # Research toggles. Omitted flags keep the DD40 defaults (all disabled), so
    # these are set explicitly per run instead of via XAUUSD_* environment vars.
    g = p.add_argument_group("Research toggles (default OFF; explicit per run)")
    g.add_argument("--trailing-open-distance", type=float,
                   default=DEFAULT_CONFIG.trailing_open_distance,
                   help="Virtual trailing-open entry distance. 0 disables (default).")
    g.add_argument("--trailing-close-distance", type=float,
                   default=DEFAULT_CONFIG.trailing_close_distance,
                   help="Protective trailing-stop distance. 0 disables (default).")
    g.add_argument("--trend-runner", action="store_true",
                   help="Hold TP3 winners while EMA trend agrees, with an ATR trailing stop.")
    g.add_argument("--trend-runner-ema-fast", type=int,
                   default=DEFAULT_CONFIG.trend_runner_ema_fast)
    g.add_argument("--trend-runner-ema-slow", type=int,
                   default=DEFAULT_CONFIG.trend_runner_ema_slow)
    g.add_argument("--trend-runner-atr-period", type=int,
                   default=DEFAULT_CONFIG.trend_runner_atr_period)
    g.add_argument("--trend-runner-atr-multiplier", type=float,
                   default=DEFAULT_CONFIG.trend_runner_atr_multiplier)
    g.add_argument("--trend-runner-no-override-max-hold", action="store_true",
                   help="Keep max-hold time-exit even for an active runner "
                        "(default: an active runner overrides max-hold).")


def _config_from_args(args: argparse.Namespace) -> StrategyConfig:
    return StrategyConfig(
        initial_capital=getattr(args, "initial_capital", DEFAULT_CONFIG.initial_capital),
        risk_per_signal=getattr(args, "risk", DEFAULT_CONFIG.risk_per_signal),
        entry_count=getattr(args, "entries", DEFAULT_CONFIG.entry_count),
        entry_ladder=getattr(args, "entry_ladder", DEFAULT_CONFIG.entry_ladder),
        entry_sl_gap=getattr(args, "entry_sl_gap", DEFAULT_CONFIG.entry_sl_gap),
        trailing_open_distance=getattr(args, "trailing_open_distance",
                                       DEFAULT_CONFIG.trailing_open_distance),
        trailing_close_distance=getattr(args, "trailing_close_distance",
                                        DEFAULT_CONFIG.trailing_close_distance),
        trend_runner_enabled=getattr(args, "trend_runner",
                                     DEFAULT_CONFIG.trend_runner_enabled),
        trend_runner_ema_fast=getattr(args, "trend_runner_ema_fast",
                                      DEFAULT_CONFIG.trend_runner_ema_fast),
        trend_runner_ema_slow=getattr(args, "trend_runner_ema_slow",
                                      DEFAULT_CONFIG.trend_runner_ema_slow),
        trend_runner_atr_period=getattr(args, "trend_runner_atr_period",
                                        DEFAULT_CONFIG.trend_runner_atr_period),
        trend_runner_atr_multiplier=getattr(args, "trend_runner_atr_multiplier",
                                            DEFAULT_CONFIG.trend_runner_atr_multiplier),
        trend_runner_override_max_hold=not getattr(
            args, "trend_runner_no_override_max_hold", False),
    )


def _add_mt5_flags(p: argparse.ArgumentParser) -> None:
    g = p.add_argument_group("MT5 connection")
    g.add_argument("--mt5-symbol", default="XAUUSD")
    g.add_argument("--mt5-server-offset", type=int, default=3)
    g.add_argument("--mt5-history-bars", type=int, default=5_000)
    g.add_argument("--mt5-path", default=None)
    g.add_argument("--mt5-login", type=int, default=None)
    g.add_argument("--mt5-password", default=None)
    g.add_argument("--mt5-server", default=None)


def _add_notification_flags(p: argparse.ArgumentParser) -> None:
    g = p.add_argument_group("Telegram notifications (Saved Messages via listener)")
    g.add_argument(
        "--notifications", default=DEFAULT_NOTIFICATIONS_PATH,
        help=(f"Path to engine notifications JSONL "
              f"(default: {DEFAULT_NOTIFICATIONS_PATH}). Listener tails this "
              f"file and forwards each event to Saved Messages."),
    )
    g.add_argument(
        "--no-notifications", action="store_true",
        help="Disable engine notifications entirely.",
    )


def _add_forensic_flags(p: argparse.ArgumentParser) -> None:
    g = p.add_argument_group("Forensic JSONL log (post-mortem analysis)")
    g.add_argument(
        "--forensic-log", default=DEFAULT_FORENSIC_PATH,
        help=(f"Path to structured JSONL event log "
              f"(default: {DEFAULT_FORENSIC_PATH}). "
              f"Filter with `python tools/dump_forensic.py`."),
    )
    g.add_argument(
        "--no-forensic", action="store_true",
        help="Disable forensic logging entirely.",
    )


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="xauusd")
    sub = p.add_subparsers(dest="cmd", required=True)

    pb = sub.add_parser("backtest", help="Run historical backtest (auto-fetches 2mo from MT5 first)")
    pb.add_argument("--signals", required=True)
    pb.add_argument("--charts", required=True, nargs="+")
    pb.add_argument("--output-dir", default=None)
    pb.add_argument("--exclude-structural-anomalies", action="store_true")
    pb.add_argument(
        "--scenario", action="append", default=None, metavar="KV",
        help="Extra parallel backtest with different capital and signal "
             "start date. Repeatable. Format: 'capital=1500,start=2026-05-19'. "
             "Writes backtest_results_{capital}_{start}.xlsx alongside the "
             "default output.",
    )
    _add_strategy_overrides(pb)
    _add_mt5_flags(pb)
    pb.set_defaults(func=cmd_backtest)

    pd_ = sub.add_parser("decide", help="Decide on one signal (use --execute to place orders on MT5)")
    pd_.add_argument("--signal", required=True)
    pd_.add_argument("--signal-date", required=True)
    pd_.add_argument("--signal-tz", type=int, required=True)
    src = pd_.add_argument_group("Chart source (only one needed)")
    src.add_argument("--charts", nargs="+", default=None)
    src.add_argument("--mt5", action="store_true")
    pd_.add_argument("--equity", type=float, default=DEFAULT_CONFIG.initial_capital)
    pd_.add_argument("--equity-from-mt5", action="store_true")
    pd_.add_argument("--positions-json", default=None,
                     help="Tracked-signal registry (default: positions.json, auto-managed when --execute is set)")
    pd_.add_argument("--now", default=None)
    pd_.add_argument("--execute", action="store_true",
                     help="Place orders on MT5 directly (no confirmation prompt). Implies --mt5.")
    _add_strategy_overrides(pd_)
    _add_mt5_flags(pd_)
    _add_notification_flags(pd_)
    _add_forensic_flags(pd_)
    pd_.set_defaults(func=cmd_decide)

    pmg = sub.add_parser("manage",
                         help="Manage tracked signals: lock SL to TP1, cancel expired pendings, time-close positions.")
    pmg.add_argument("--positions-json", default=None)
    pmg.add_argument("--execute", action="store_true",
                     help="Apply changes to MT5. Without this flag, prints status only.")
    pmg.add_argument("--watch", action="store_true",
                     help="Loop the manage cycle every --watch-interval seconds.")
    pmg.add_argument("--watch-interval", type=float, default=5.0,
                     help="Seconds between watch iterations (default: 5.0). Minimum 1.0.")
    pmg.add_argument("--no-clear", action="store_true",
                     help="In watch mode, disable the screen clear between iterations.")
    _add_strategy_overrides(pmg)
    _add_mt5_flags(pmg)
    _add_notification_flags(pmg)
    _add_forensic_flags(pmg)
    pmg.set_defaults(func=cmd_manage)

    pa = sub.add_parser("auto",
                        help="One-command live trading: continuously read signals.txt + execute + manage.")
    pa.add_argument("--signals", required=True)
    pa.add_argument("--positions-json", default=None)
    pa.add_argument("--watch-interval", type=float, default=5.0)
    pa.add_argument("--no-clear", action="store_true")
    pa.add_argument("--replace-missing-entries", action="store_true",
                    help="Each cycle, re-place still-pending LIMIT entries that vanished from MT5 "
                         "(e.g. cancelled by hand) for a signal that still has an MT5 footprint.")
    pa.add_argument("--reopen-missing-positions", action="store_true",
                    help="Each cycle, re-open at market any entry the replay still holds OPEN "
                         "but that is missing from MT5 (e.g. closed by hand), so live execution "
                         "keeps mirroring the backtest.")
    _add_strategy_overrides(pa)
    _add_mt5_flags(pa)
    _add_notification_flags(pa)
    _add_forensic_flags(pa)
    pa.set_defaults(func=cmd_auto)

    pm = sub.add_parser("mt5-info", help="Diagnostic: latest bar, equity, open MT5 objects")
    _add_mt5_flags(pm)
    pm.set_defaults(func=cmd_mt5_info)

    pf = sub.add_parser("fetch", help="Pull last N months of M1 to data/ (default 2)")
    pf.add_argument("--months", type=int, default=ARCHIVE_MONTHS,
                    help="How many months back to refresh (default 2). Live feed "
                         "loops use 1: prior months' CSVs are immutable once the "
                         "month has rolled over, so only the current month needs "
                         "refreshing.")
    _add_mt5_flags(pf)
    pf.set_defaults(func=cmd_fetch)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())