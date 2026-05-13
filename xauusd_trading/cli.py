"""Command-line interface. Replaces xauusd_trading/cli.py completely.

Subcommands:
    xauusd backtest   --signals SIGNALS_FILE --charts CSV [CSV ...] [--output-dir DIR]
                      (always fetches latest 2 months of M1 from MT5 first if available)

    xauusd decide     --signal "..." --signal-date YYYY-MM-DD --signal-tz N [--execute]
                      (default: print-only. With --execute: places + manages on MT5.
                       If the signal's pending window has already closed by the time
                       you run this, no orders are placed. Successful placement
                       records `executed_at` (wall-clock chart-tz) in positions.json
                       so later runs can show "X min late" and replay the actual
                       MT5 trajectory alongside the ideal one.)

    xauusd manage     [--execute] [--watch]
                      (manage existing tracked signals only; no new signal placement.
                       Cancels expired pendings, locks SL to TP1 after TP1 touch,
                       time-closes positions past max-hold deadline. Run periodically.
                       When a tracked signal has `executed_at` and you were late,
                       the report shows BOTH the ideal-execution replay and the
                       actual-execution replay so you can see what the lag cost.)

    xauusd auto       --signals signals.txt
                      (one-command live trading. Loops forever: reads signals.txt for
                       new signals to execute, manages all tracked positions on each
                       cycle. Combines `decide --execute` and `manage --watch --execute`
                       into one workflow. Execution is implicit -- this command places
                       real orders. Exits only on Ctrl+C. The 3-layer re-entry guard
                       and SKIP_EXPIRED gate apply on every iteration, so re-reading
                       signals.txt with already-processed signals is a no-op.)

    xauusd mt5-info   diagnostic
    xauusd fetch      pull M1 to per-month CSVs (no decision)
"""
from __future__ import annotations
import argparse
import glob
import json
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

from .adapters import CsvChartSource, ManualPositionSource
from .backtest import run_backtest, write_backtest_outputs
from .config import (
    CHART_TIMEZONE_OFFSET, CONTRACT_SIZE_OZ, DEFAULT_CONFIG, StrategyConfig,
)
from .engine import decide, render_report
from .positions import Position, advance_bars, open_position
from .signal import parse_one_signal, parse_signals_file

# Hardcoded archive policy (per project preference: minimal flags).
ARCHIVE_DIR = "data"
ARCHIVE_MONTHS = 2


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
    """Best-effort: pull last ARCHIVE_MONTHS from MT5 to ARCHIVE_DIR.
    Soft-fail (warn and continue) if MT5 isn't reachable.
    """
    try:
        from .mt5_adapter import (
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
# tracked-signal replay (used by decide, manage, and auto)
# ---------------------------------------------------------------------------

def _chart_now() -> datetime:
    """Wall-clock current time in chart timezone (GMT+3), naive.

    Matches the project convention of using GMT+3 naive datetimes everywhere
    internally. `datetime.utcnow()` is fine here; the project already uses it
    elsewhere in mt5_adapter.py. If you migrate to a stricter tz-aware setup
    later, do it in one pass across the codebase.
    """
    return datetime.utcnow() + timedelta(hours=CHART_TIMEZONE_OFFSET)


def _parse_executed_at(raw) -> datetime | None:
    """Parse an executed_at field from the registry. None on missing/invalid."""
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw))
    except Exception:
        return None


def _replay_tracked_signal(item: dict, chart, replay_end: datetime,
                           config: StrategyConfig
                           ) -> tuple[Position, Position, datetime | None]:
    """Replay one registry entry, returning (pos_ideal, pos_actual, executed_at).

    pos_ideal:  replayed from the signal's activation_time. This is the
                "strategy assumes instant placement at signal time" view,
                identical to what `replay_signal` does in backtest -- so it
                serves as the baseline of what *should* have happened.

    pos_actual: replayed from executed_at when that timestamp is present AND
                later than activation_time. Otherwise it's the SAME Python
                object as pos_ideal (no second replay; both views collapse).

    executed_at: parsed datetime in chart tz, or None for legacy entries
                that predate the field.

    Both Position objects have their `executed_at` field stamped (where
    available) so downstream renderers can show "X min late" inline.
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
        # Replay from the real placement moment. Bars before executed_at
        # don't trigger fills here because pos_actual never saw them.
        advance_bars(
            pos_actual,
            chart.bars_between(executed_at, replay_end),
            config,
        )
        pos_actual.executed_at = executed_at
    else:
        # Either no executed_at recorded (legacy entry) or it equals/predates
        # activation_time (on-time placement). Single view suffices.
        pos_actual = pos_ideal

    return pos_ideal, pos_actual, executed_at


# ---------------------------------------------------------------------------
# manage/auto output formatters
# ---------------------------------------------------------------------------

def _format_lateness(executed_at: datetime, signal_time: datetime) -> str:
    """Return '(X.X min late)' / '(on time)' / '(X.X min early)' annotation."""
    delta_min = (executed_at - signal_time).total_seconds() / 60.0
    if delta_min >= 0.5:
        return f"({delta_min:.1f} min late)"
    if delta_min <= -0.5:
        return f"({-delta_min:.1f} min early)"
    return "(on time)"


def _entry_floating(entry, side: str, bid: float, ask: float,
                    contract_size: float = CONTRACT_SIZE_OZ) -> float:
    """Floating P&L for one OPEN entry against current bid/ask.

    Returns 0.0 for non-OPEN entries (no exposure). Mirrors the formula in
    engine.py's `_snapshot_position`: BUYs would close at the bid, SELLs at
    the ask (= bid + spread). Used both for the actual-view OPEN entries
    (matches MT5's "Profit" column closely, modulo swap/commission) and for
    the ideal-view counterfactual annotations.
    """
    if entry.status != "OPEN":
        return 0.0
    if side == "BUY":
        return (bid - entry.entry_price) * entry.lot * contract_size
    return (entry.entry_price - ask) * entry.lot * contract_size


def _format_entry_line(entry, side: str, bid: float, ask: float,
                       contract_size: float, ideal_entry=None) -> str:
    """Format one entry line. When `ideal_entry` is given AND its status
    diverges from `entry.status`, append a "[if on time: ...]" annotation
    explaining the counterfactual.
    """
    if entry.status == "OPEN":
        floating = _entry_floating(entry, side, bid, ask, contract_size)
        base = (f"    #{entry.entry_index}  ({entry.entry_price:g})  OPEN     "
                f"lot={entry.lot:.2f}   floating ${floating:+.2f}")
    elif entry.status == "PENDING":
        base = (f"    #{entry.entry_index}  ({entry.entry_price:g})  PENDING  "
                f"lot={entry.lot:.2f}  (limit waiting)")
    elif entry.status == "NO_FILL":
        base = f"    #{entry.entry_index}  ({entry.entry_price:g})  NO_FILL"
    else:  # SL / LOCK_TP1 / TP1 / TP2 / TP3 / TIME_EXIT
        pnl_str = f"${entry.pnl:+.2f}" if entry.pnl is not None else "-"
        exit_str = f"@ {entry.exit_price:g}" if entry.exit_price is not None else "-"
        base = (f"    #{entry.entry_index}  ({entry.entry_price:g})  "
                f"{entry.status} {exit_str}  pnl={pnl_str}")

    if ideal_entry is None or ideal_entry.status == entry.status:
        return base

    # Statuses diverge -- annotate the actual-view line with what the
    # ideal-view counterfactual would say. Practical cases (ideal is always
    # at-or-ahead of actual because the actual replay starts later):
    #   ideal=OPEN, actual=PENDING       -> you missed the fill so far
    #   ideal=<terminal>, actual=PENDING -> ideal already filled & exited
    #   ideal=<terminal>, actual=OPEN    -> ideal exited, actual still holding
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
    """Format the stage/fill/entries block for one Position view.

    Returns (lines, floating_total, realized_total). Lines are 4-space-
    indented; caller may add further indent when nesting under "If executed
    on time" / "Actual" sub-headers.

    `ideal_for_annotations`, when given, is the ideal Position to compare
    against -- used only for the ACTUAL view of a dual-view block, so
    entries whose status diverges from the ideal get an inline
    "[if on time: ...]" annotation pointing at the counterfactual.
    """
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
    """Render one tracked-signal block. Returns (text, floating, realized)
    for the ACTUAL view, which the caller uses to roll up the grand total.

    When `executed_at` is recorded AND the user was meaningfully late, this
    emits a dual view:
      - "If executed on time" -- the strategy's ideal-execution assumption
        (replayed from signal time); matches what backtest produces.
      - "Actual"              -- what MT5 saw, replayed from `executed_at`;
        bars before placement can't fire fills.

    On divergence between the views, each diverging entry in the actual
    view gets a `[if on time: ...]` annotation, and a "Lateness cost so far"
    summary line gives the net P&L delta plus a missed/extra-fill count.

    For legacy entries (no `executed_at`) and on-time placements the dual
    view collapses to a single view; the output looks like before but with
    per-entry floating P&L and a Floating/Realized footer added.
    """
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

    if pos_actual is not pos_ideal:
        # Dual view: real late-execution split exists.
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

        # Lateness cost: delta in total P&L between actual and ideal.
        # Negative means lateness hurt you ($X less than on-time path);
        # positive means lateness helped you (avoided an adverse fill).
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

    # Single view: legacy entry or on-time placement.
    body_lines, floating_total, realized_total = _format_position_body(
        pos_ideal, now, bid, ask, contract_size,
    )
    lines.extend(body_lines)
    total = floating_total + realized_total
    lines.append(f"    Total:         ${total:+.2f}")
    return "\n".join(lines), floating_total, realized_total


# ---------------------------------------------------------------------------
# subcommand: backtest
# ---------------------------------------------------------------------------

def cmd_backtest(args: argparse.Namespace) -> int:
    config = _config_from_args(args)
    _try_archive_from_mt5(args.mt5_symbol, args.mt5_server_offset)

    signals = parse_signals_file(Path(args.signals))
    chart = CsvChartSource(_expand_chart_paths(args.charts))
    result = run_backtest(
        signals, chart, config,
        exclude_structural_anomalies=args.exclude_structural_anomalies,
    )
    summary = {k: v for k, v in result.items() if k not in {"rows", "entry_rows"}}
    print(json.dumps(summary, indent=2, default=str))
    if args.output_dir:
        write_backtest_outputs(result, Path(args.output_dir))
        print(f"\nWrote outputs to {Path(args.output_dir).resolve()}", file=sys.stderr)
    return 0


# ---------------------------------------------------------------------------
# subcommand: decide
# ---------------------------------------------------------------------------

def cmd_decide(args: argparse.Namespace) -> int:
    config = _config_from_args(args)

    use_mt5 = bool(args.mt5) or bool(args.execute)
    conn = None

    if use_mt5:
        from .mt5_adapter import (
            Mt5ChartSource, Mt5Connection, mt5_equity,
            archive_m1_by_month, render_archive_summary,
        )
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

    # Build (ideal, actual, executed_at) for every tracked signal. Use the
    # actual view for engine / executor calls so MT5 actions and the report's
    # OPEN POSITIONS section reflect what MT5 actually sees.
    tracked: list[tuple[Position, Position, datetime | None]] = []
    if prior_entries and replay_end is not None:
        for item in prior_entries:
            tracked.append(_replay_tracked_signal(item, chart, replay_end, config))

    open_positions = [t[1] for t in tracked]  # pos_actual

    positions = ManualPositionSource(equity=equity, positions=open_positions)
    rec = decide(signal, chart, positions, config, now=now)
    print(render_report(rec))

    if args.execute:
        from .mt5_executor import (
            Mt5Executor, SignalRegistry, signal_to_magic,
            render_execution_log, ExecutionLog,
        )
        executor = Mt5Executor(
            conn, args.mt5_symbol,
            min_lot=config.minimum_lot or 0.01,
            lot_step=config.lot_step or 0.01,
            server_offset_hours=args.mt5_server_offset,
        )

        print()
        errors = executor.sanity_checks(expected_equity=equity)
        if errors:
            print("SANITY CHECKS FAILED -- aborting execution:")
            for e in errors:
                print(f"  ! {e}")
            conn.shutdown()
            return 2

        registry = SignalRegistry(registry_path)
        log = ExecutionLog()

        # Manage existing tracked signals first, against their actual replay
        # so cancel/lock/time-exit decisions match MT5's true state.
        for pos in open_positions:
            mlog = executor.manage_position(pos, config, rec.generated_at)
            log.merge(mlog)

        known = {signal_to_magic(p.signal.signal_key) for p in open_positions}
        known.add(signal_to_magic(signal.signal_key))
        log.warnings.extend(executor.warn_on_unknown(known))

        # Decide what to do with the new signal:
        #   1. Already tracked  -> management above handled it; do not re-place.
        #   2. Pending window already closed -> skip placement (would only be
        #      cancelled immediately by the next manage cycle anyway).
        #   3. Otherwise        -> place the planned orders, capture executed_at.
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
        else:
            plog = executor.place_signal(signal, rec.new_signal)
            log.merge(plog)
            if plog.placed > 0:
                # Stamp the registry with the wall-clock placement moment.
                # Captured AFTER place_signal returns so it reflects the time
                # MT5 actually accepted the orders, not when we started the
                # command. Subsequent `manage` runs use this to compute how
                # late you were and replay the actual MT5 trajectory.
                executed_at = _chart_now()
                registry.add(signal, equity, executed_at=executed_at)
                lateness = _format_lateness(executed_at, signal.signal_time_chart)
                log.actions.append(
                    f"Recorded executed_at = "
                    f"{executed_at:%Y-%m-%d %H:%M:%S} GMT+3 {lateness}"
                )

        alive = executor.all_alive_magics()
        removed = registry.prune(alive)
        if removed:
            log.actions.append(f"Pruned {removed} closed signal(s) from {registry_path.name}")

        print(render_execution_log(log))

    if conn is not None:
        conn.shutdown()
    return 0


# ---------------------------------------------------------------------------
# subcommand: manage
# ---------------------------------------------------------------------------

def cmd_manage(args: argparse.Namespace) -> int:
    """Manage existing tracked signals on MT5; do NOT place anything new.

    Reads positions.json, replays each tracked signal against the live MT5
    chart up to "now", and (with --execute) applies engine-driven changes:
      - Cancel pending orders that have expired (older than pending_expiry_minutes)
      - Lock SL to TP1 once TP1 has been touched on a filled entry
      - Time-close positions whose first fill is older than max_hold_minutes

    For tracked signals that have an `executed_at` and were placed late, the
    report shows TWO replays per signal:
      - "If executed on time" -- the strategy's ideal-execution assumption
      - "Actual"              -- what MT5 saw, starting from your real
                                 placement moment
    Use the gap between them to gauge how much manual lag is costing you.
    Auto-execution (planned) will close that gap to zero.

    Without --execute, prints status only (safe to run any time).

    With --watch, loops the manage cycle every --watch-interval seconds
    (default 5s) until positions.json has no live MT5 footprint or the user
    interrupts with Ctrl+C. Keeps one MT5 connection open across iterations
    to avoid the re-import / re-init / re-archive overhead of looping at the
    PowerShell level. The archive runs once at startup only.

    Note: for one-command live trading (read signals.txt + execute + manage),
    use `auto` instead. `manage --watch` is for "I'm already in trades and
    just want to babysit them"; `auto` is for "let the system handle the
    whole day".
    """
    config = _config_from_args(args)

    # Validate --watch-interval if watch mode is on.
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
            print(
                f"WARNING: --watch-interval {interval}s is aggressive. The "
                f"strategy is M1 so the worst-case reversal window is 60s; "
                f"5s gives a 12x safety margin and is the recommended default."
            )
            print()

    from .mt5_adapter import (
        Mt5ChartSource, Mt5Connection, mt5_equity,
        archive_m1_by_month, render_archive_summary,
    )

    conn = Mt5Connection(
        path=args.mt5_path, login=args.mt5_login,
        password=args.mt5_password, server=args.mt5_server,
    )
    conn.initialize()

    try:
        # One-time archive at startup. In watch mode this is intentionally
        # NOT repeated per iteration -- the chart source re-queries MT5 on
        # every call, so fresh bars are always used for replay; the archive
        # is only for offline backtesting and doesn't need refreshing during
        # an interactive watch session.
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
        exit_code, _ = _manage_pass(args, config, conn, chart)
        return exit_code
    finally:
        conn.shutdown()


def _manage_pass(args: argparse.Namespace, config: StrategyConfig,
                 conn, chart) -> tuple[int, int]:
    """Run one manage cycle.

    Returns (exit_code, n_alive_on_mt5) where n_alive_on_mt5 is the count of
    currently-tracked signals that still have at least one order or position
    on MT5. The watch loop uses 0 to detect "all closed; exit cleanly".

    Works in both --execute and read-only modes. In read-only mode the alive
    count is computed by querying MT5 directly without mutating the registry,
    so watch + no-execute can still terminate naturally when everything closes.
    """
    from .mt5_adapter import mt5_equity
    from .mt5_executor import (
        Mt5Executor, SignalRegistry, signal_to_magic,
        render_execution_log, ExecutionLog,
    )

    try:
        equity = mt5_equity(conn)
    except Exception as e:
        print(f"[mt5] account_info() failed: {e}", file=sys.stderr)
        return 2, 0

    registry_path = Path(args.positions_json or "positions.json")
    if not registry_path.exists():
        print(f"No registry file at {registry_path.resolve()}; nothing to manage.")
        return 0, 0

    try:
        prior_entries = json.loads(registry_path.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"Could not read {registry_path}: {e}", file=sys.stderr)
        return 2, 0

    if not prior_entries:
        print(f"{registry_path.name} is empty; nothing to manage.")
        return 0, 0

    replay_end = chart.last_time()
    if replay_end is None:
        print("[mt5] no chart data available; aborting.", file=sys.stderr)
        return 2, 0

    tracked: list[tuple[Position, Position, datetime | None]] = []
    for item in prior_entries:
        tracked.append(_replay_tracked_signal(item, chart, replay_end, config))

    # Live bid/ask for floating P&L. The tick is fresher (0-60s) than the
    # last completed M1 bar's close, so per-entry floating numbers match
    # MT5's "Profit" column tightly. If the market is closed or the tick is
    # missing, fall back to the latest bar's close as bid and bar.close +
    # bar.spread_price as ask -- floating numbers will be slightly stale but
    # the display still works.
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

    # Build the executor up front -- in --execute mode it runs management
    # actions further down; in read-only mode it's used to query alive magics
    # for the watch-mode exit check.
    executor = Mt5Executor(
        conn, args.mt5_symbol,
        min_lot=config.minimum_lot or 0.01,
        lot_step=config.lot_step or 0.01,
        server_offset_hours=args.mt5_server_offset,
    )
    tracked_magics = {
        signal_to_magic(actual.signal.signal_key)
        for _ideal, actual, _exec_at in tracked
    }

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

    # Grand total across all tracked signals (engine view).
    # In divergence cases the engine's floating shows $0 for entries it
    # considers LOCK_TP1-terminal even when MT5 still has them open; under
    # --execute, `manage_position` then runs a "Late TP1 catch-up" market
    # close on those positions to cap the divergence and bring MT5 into line
    # with the backtest path.
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
            return 2, len(tracked_magics)

        registry = SignalRegistry(registry_path)
        log = ExecutionLog()

        # Drive MT5 actions from the actual replay -- that's the one whose
        # stage / time_exit_deadline match what MT5 has actually seen.
        for _ideal, actual, _exec_at in tracked:
            mlog = executor.manage_position(actual, config, replay_end)
            log.merge(mlog)

        log.warnings.extend(executor.warn_on_unknown(tracked_magics))

        alive = executor.all_alive_magics()
        removed = registry.prune(alive)
        if removed:
            log.actions.append(f"Pruned {removed} closed signal(s) from {registry_path.name}")

        print(render_execution_log(log))
    else:
        # Read-only path: still query MT5 for the alive set so watch mode
        # can exit cleanly when nothing's left. No registry mutation.
        alive = executor.all_alive_magics()
        print("(read-only -- pass --execute to apply changes to MT5)")

    # n_alive = how many of OUR tracked signals still have any MT5 footprint.
    # Both branches compute `alive` from the same all_alive_magics() call
    # after any management actions, so closed signals are reflected.
    n_alive = len(tracked_magics & alive)
    return 0, n_alive


def _run_manage_watch(args: argparse.Namespace, config: StrategyConfig,
                      conn, chart) -> int:
    """Loop the manage cycle until the registry has no live MT5 footprint or
    the user hits Ctrl+C. One MT5 connection is shared across iterations.

    By default each iteration clears the terminal (move cursor home + erase
    from cursor to end of screen) so the output behaves like a live dashboard:
    one screenful at a time, no scrolling. Modern terminals (PowerShell 7+,
    Windows Terminal, anything VT-aware) preserve content above the clear
    point in their scrollback buffer, so you can still scroll up to see prior
    iterations if needed. Pass --no-clear to fall back to scrolling-log
    behavior (useful when you want a permanent record of EXECUTION lines).
    """
    interval = float(args.watch_interval)
    clear_screen = not args.no_clear
    iteration = 0
    try:
        while True:
            iteration += 1
            if clear_screen:
                # \x1b[H = move cursor to top-left (row 1, col 1).
                # \x1b[J = erase from cursor to end of screen.
                # Combined: clear the visible area without nuking scrollback.
                sys.stdout.write("\x1b[H\x1b[J")
                sys.stdout.flush()
            else:
                print()
            print(
                f"[watch iter #{iteration} -- "
                f"{datetime.now():%Y-%m-%d %H:%M:%S} local -- "
                f"interval {interval:g}s]"
            )
            exit_code, n_alive = _manage_pass(args, config, conn, chart)
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
    """One-command live trading: read signals.txt + execute + manage in a loop.

    Combines `decide --execute` and `manage --watch --execute` into a single
    workflow. Designed to run for the duration of a trading session and let
    you ignore everything except keeping signals.txt up to date.

    On every iteration:
      1. Replay each tracked position against the latest MT5 chart.
      2. Render the dashboard (current state of every tracked signal,
         with live floating P&L from symbol_info_tick).
      3. Run management on MT5 (cancel expired pendings, Late TP1 catch-up,
         lock SL to TP1 after TP1 touch, time-close past max-hold).
      4. Re-parse signals.txt and pick out candidate signals:
         - signal_time within the last (pending_expiry + 5min) -- older ones
           would SKIP_EXPIRED anyway, no point flooding the log;
         - signal_key not already in positions.json.
      5. Run each candidate through the engine. If the engine says FOLLOW,
         place via `executor.place_signal` (the 3-layer re-entry guard catches
         anything that's already on MT5 or in recent MT5 history). If the
         engine says SKIP_EXPIRED (race: aged out between filter and decide),
         log and continue.
      6. Stamp executed_at on the registry for every successful placement.
      7. Prune closed signals.
      8. Sleep --watch-interval seconds.

    Exits only on Ctrl+C. Unlike `manage --watch`, the loop does NOT exit
    when positions.json is empty -- new signals from signals.txt can arrive
    at any moment, and the whole point of this mode is to wait for them.

    Execution is implicit -- this command places real orders. No --execute
    flag, no dry-run mode here. If you want a preview, use `decide` on a
    single signal without --execute.

    SAFETY:
    - Do NOT run `auto` and a Task Scheduler `manage --execute` against the
      same positions.json. They will race on SL modifications.
    - Anything you paste into signals.txt will be executed if it's within
      the 4-hour pending window. Treat signals.txt as the trust boundary --
      same risk profile as `decide --execute`, but with less manual friction
      between Telegram and MT5.
    - Drawdown tolerance is still 50%. If realized DD goes past -42.8%
      (the backtest's max), regime may have changed; consider Ctrl+C and
      re-evaluating before letting it keep auto-firing.

    REALISTIC EXPECTATIONS:
    - Forward expectation 2-10x/month anchored on the IS column, not the
      OOS/full-period backtest headlines.
    - Concurrent same-direction signals can compound losses faster than the
      backtest's -42.8% max DD because backtest runs signals sequentially.
    """
    config = _config_from_args(args)

    # Validate --watch-interval.
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

    # Validate signals file up front. Re-read happens every iteration, so a
    # transient permission error here would surface again; better to fail
    # before the loop starts.
    signals_path = Path(args.signals)
    if not signals_path.exists():
        print(f"signals file not found: {signals_path}", file=sys.stderr)
        return 2
    try:
        parse_signals_file(signals_path)
    except Exception as e:
        print(f"signals file failed to parse: {e}", file=sys.stderr)
        return 2

    from .mt5_adapter import (
        Mt5ChartSource, Mt5Connection,
        archive_m1_by_month, render_archive_summary,
    )

    conn = Mt5Connection(
        path=args.mt5_path, login=args.mt5_login,
        password=args.mt5_password, server=args.mt5_server,
    )
    conn.initialize()

    try:
        # One-time archive at startup. Subsequent iterations re-query MT5
        # for chart data directly; the archive is only for offline backtest
        # use and doesn't need refreshing during an active session.
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
    """Loop _auto_pass until Ctrl+C. Unlike manage --watch, this never exits
    on an empty registry -- new signals can arrive in signals.txt at any time.
    """
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
            exit_code = _auto_pass(args, config, conn, chart, signals_path)
            if exit_code != 0:
                return exit_code
            time.sleep(interval)
    except KeyboardInterrupt:
        print()
        print("Interrupted; exiting auto mode.")
        return 0


def _auto_pass(args: argparse.Namespace, config: StrategyConfig,
               conn, chart, signals_path: Path) -> int:
    """One auto cycle. Returns 0 to continue looping, nonzero to abort.

    Recoverable conditions (closed market, transient MT5 issues, transient
    signals.txt read errors) return 0 so the loop retries on the next tick.
    Only conditions that won't get better with a retry (account_info failure,
    no chart data) return nonzero.
    """
    from .mt5_adapter import mt5_equity
    from .mt5_executor import (
        Mt5Executor, SignalRegistry, signal_to_magic,
        render_execution_log, ExecutionLog,
    )

    # 1. Account equity.
    try:
        equity = mt5_equity(conn)
    except Exception as e:
        print(f"[mt5] account_info() failed: {e}", file=sys.stderr)
        return 2

    # 2. Registry.
    registry_path = Path(args.positions_json or "positions.json")
    registry = SignalRegistry(registry_path)
    prior_entries = registry.load()

    # 3. Chart state.
    replay_end = chart.last_time()
    if replay_end is None:
        print("[mt5] no chart data available; skipping iteration")
        return 0  # transient: retry next iter

    # 4. Replay each tracked signal (ideal + actual views).
    tracked: list[tuple[Position, Position, datetime | None]] = []
    for item in prior_entries:
        tracked.append(_replay_tracked_signal(item, chart, replay_end, config))

    # 5. Live bid/ask. Fresh tick if available, else last bar's close +
    # spread. Used for floating P&L in the dashboard.
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
    )

    # 7. Dashboard header.
    print("=" * 70)
    print("XAUUSD AUTO MODE  (signals + management)")
    print(f"Chart time:      {replay_end}  GMT+3")
    print(f"Account equity:  ${equity:,.2f}")
    print(f"Tracked signals: {len(tracked)}")
    if bid > 0:
        print(f"Live bid/ask:    {bid:g} / {ask:g}")
    print("=" * 70)

    # 8. Per-signal status blocks.
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

        # 9. Grand total.
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
        # Market may be closed or the broker dropped trading temporarily;
        # neither is a reason to abort the loop. Retry next tick.
        return 0

    log = ExecutionLog()

    # 11. Manage tracked positions (drive from actual replay so MT5 actions
    # match what MT5 has actually seen, not the ideal-execution assumption).
    for _ideal, actual, _exec_at in tracked:
        mlog = executor.manage_position(actual, config, replay_end)
        log.merge(mlog)

    # 12. Re-read signals.txt. Cheap (regex over a few-KB text file) so we
    # don't need mtime tracking. A transient read error here just means we
    # skip placement this iteration and try again next tick.
    try:
        all_signals = parse_signals_file(signals_path)
    except Exception as e:
        print(f"[signals] failed to parse {signals_path}: {e}")
        all_signals = []

    # 13. Filter candidates:
    #   (a) signal_time within the pending window (with a small buffer so
    #       signals that just expired don't oscillate);
    #   (b) signal_key not already in the registry.
    # The 3-layer guard in executor.place_signal handles everything else
    # (current MT5 footprint, recent MT5 history). Re-load the registry
    # here because manage_position above may have closed positions that
    # haven't been pruned yet -- but those signals would still be in the
    # registry, so the membership check stays correct.
    existing_keys = {item.get("signal_key") for item in registry.load()}
    age_cutoff = replay_end - timedelta(minutes=config.pending_expiry_minutes + 5)

    candidates = [
        s for s in all_signals
        if s.signal_time_chart > age_cutoff
           and s.signal_key not in existing_keys
    ]
    # Process oldest-first. Matches backtest's chronological order; ensures
    # that if two new signals appear in the same iteration, the earlier one
    # is placed first so its lot is sized off the current equity before the
    # second one (which sees the same equity here, since MT5 hasn't realized
    # P&L on the just-placed limits yet -- but the order is still stable).
    candidates.sort(key=lambda s: s.signal_time_chart)

    # 14. Process candidates.
    for signal in candidates:
        positions_source = ManualPositionSource(
            equity=equity,
            positions=[t[1] for t in tracked],
        )
        rec = decide(signal, chart, positions_source, config)

        if rec.new_signal.action == "SKIP_EXPIRED":
            # Race: signal aged out between the age filter and now. Rare
            # (the buffer covers normal iteration jitter) but possible.
            log.actions.append(
                f"Signal {signal.signal_key}: pending window already closed at "
                f"{rec.new_signal.pending_expires_at:%Y-%m-%d %H:%M} GMT+3 "
                f"(now {replay_end:%H:%M}). Skipped."
            )
            continue

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

    # 15. Unknown-position warnings. Re-load registry because step 14 may
    # have added new entries.
    known_magics = {
        signal_to_magic(item.get("signal_key", "?"))
        for item in registry.load()
    }
    log.warnings.extend(executor.warn_on_unknown(known_magics))

    # 16. Prune closed signals.
    alive = executor.all_alive_magics()
    removed = registry.prune(alive)
    if removed:
        log.actions.append(
            f"Pruned {removed} closed signal(s) from {registry_path.name}"
        )

    # 17. Execution log. Only render when something actually happened --
    # most iterations will be quiet, and printing an empty "EXECUTION:
    # placed=0 modified=0 cancelled=0 closed=0" line every 5s is noise.
    has_actions = (
            log.actions or log.warnings
            or log.placed > 0 or log.modified > 0
            or log.cancelled > 0 or log.closed > 0
    )
    if has_actions:
        print(render_execution_log(log))

    return 0


# ---------------------------------------------------------------------------
# subcommand: mt5-info
# ---------------------------------------------------------------------------

def cmd_mt5_info(args: argparse.Namespace) -> int:
    from .mt5_adapter import (
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
    from .mt5_adapter import (
        Mt5Connection, archive_m1_by_month, render_archive_summary,
    )
    with Mt5Connection(
            path=args.mt5_path, login=args.mt5_login,
            password=args.mt5_password, server=args.mt5_server,
    ) as conn:
        summary = archive_m1_by_month(
            conn, args.mt5_symbol, ARCHIVE_DIR,
            months_back=ARCHIVE_MONTHS,
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


def _config_from_args(args: argparse.Namespace) -> StrategyConfig:
    return StrategyConfig(
        initial_capital=getattr(args, "initial_capital", DEFAULT_CONFIG.initial_capital),
        risk_per_signal=getattr(args, "risk", DEFAULT_CONFIG.risk_per_signal),
        entry_count=getattr(args, "entries", DEFAULT_CONFIG.entry_count),
        entry_ladder=getattr(args, "entry_ladder", DEFAULT_CONFIG.entry_ladder),
        entry_sl_gap=getattr(args, "entry_sl_gap", DEFAULT_CONFIG.entry_sl_gap),
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


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="xauusd")
    sub = p.add_subparsers(dest="cmd", required=True)

    pb = sub.add_parser("backtest", help="Run historical backtest (auto-fetches 2mo from MT5 first)")
    pb.add_argument("--signals", required=True)
    pb.add_argument("--charts", required=True, nargs="+")
    pb.add_argument("--output-dir", default=None)
    pb.add_argument("--exclude-structural-anomalies", action="store_true")
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
                     help="Place orders on MT5 directly (no confirmation prompt). Implies --mt5. "
                          "Skips placement for signals whose pending window has already closed. "
                          "On successful placement, records executed_at in positions.json so "
                          "later runs can show how late you were.")
    _add_strategy_overrides(pd_)
    _add_mt5_flags(pd_)
    pd_.set_defaults(func=cmd_decide)

    pmg = sub.add_parser("manage",
                         help="Manage tracked signals: lock SL to TP1, cancel expired pendings, time-close positions. "
                              "Run periodically. Without --execute prints status only. Shows ideal vs actual "
                              "replay side-by-side for signals you placed late.")
    pmg.add_argument("--positions-json", default=None,
                     help="Tracked-signal registry (default: positions.json)")
    pmg.add_argument("--execute", action="store_true",
                     help="Apply changes to MT5. Without this flag, prints status only.")
    pmg.add_argument("--watch", action="store_true",
                     help="Loop the manage cycle every --watch-interval seconds, keeping "
                          "one MT5 connection open across iterations. Exits when no tracked "
                          "signal has any live MT5 footprint, or on Ctrl+C. For one-command "
                          "live trading (read signals.txt + execute + manage), use `auto` instead.")
    pmg.add_argument("--watch-interval", type=float, default=5.0,
                     help="Seconds between watch iterations (default: 5.0). Minimum 1.0. "
                          "Values under 2.0 print a warning at startup -- the strategy is M1 "
                          "so 5s gives a 12x safety margin against the worst-case 60s reversal "
                          "window between TP1 touch and SL hit.")
    pmg.add_argument("--no-clear", action="store_true",
                     help="In watch mode, disable the screen clear between iterations. "
                          "Default in watch mode is to clear (dashboard behavior): output "
                          "stays on one screen and refreshes in place. With --no-clear, "
                          "iterations scroll instead -- useful when you want a permanent "
                          "record of EXECUTION lines or you're piping output to a file.")
    _add_strategy_overrides(pmg)
    _add_mt5_flags(pmg)
    pmg.set_defaults(func=cmd_manage)

    pa = sub.add_parser("auto",
                        help="One-command live trading: continuously read signals.txt to "
                             "execute valid new signals and manage all tracked positions. "
                             "Execution is implicit (real orders placed). Exits only on Ctrl+C. "
                             "Combines `decide --execute` + `manage --watch --execute`.")
    pa.add_argument("--signals", required=True,
                    help="Path to signals.txt. Paste new signals into this file as they "
                         "arrive (with date headers like '2026-05-13 GMT+7' on top); the "
                         "auto loop re-reads it on every iteration and picks up new ones.")
    pa.add_argument("--positions-json", default=None,
                    help="Tracked-signal registry (default: positions.json). Auto-managed: "
                         "new signals are added on successful placement, closed signals are "
                         "pruned, executed_at is stamped for every placement.")
    pa.add_argument("--watch-interval", type=float, default=5.0,
                    help="Seconds between iterations (default: 5.0). Minimum 1.0. Values "
                         "under 2.0 print a warning at startup -- the strategy is M1 so 5s "
                         "gives a 12x safety margin against the worst-case 60s TP1-to-SL "
                         "reversal window.")
    pa.add_argument("--no-clear", action="store_true",
                    help="Disable the screen-clear between iterations. Default is to clear "
                         "(live dashboard behavior). With --no-clear, iterations scroll -- "
                         "useful for piping to a log file or when you want a permanent "
                         "audit trail of EXECUTION lines.")
    _add_strategy_overrides(pa)
    _add_mt5_flags(pa)
    pa.set_defaults(func=cmd_auto)

    pm = sub.add_parser("mt5-info", help="Diagnostic: latest bar, equity, open MT5 objects")
    _add_mt5_flags(pm)
    pm.set_defaults(func=cmd_mt5_info)

    pf = sub.add_parser("fetch", help="Pull last 2 months of M1 to data/")
    _add_mt5_flags(pf)
    pf.set_defaults(func=cmd_fetch)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())