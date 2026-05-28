"""xauusd_trading — public re-export surface.

All callers (internal submodules, cli, tests, tools, listener) import
from `xauusd_trading` directly rather than from nested module paths.
Moving a function between files only requires updating this file.

Re-exports are ordered by dependency: each block can only reference
names defined by blocks above it. Reordering will break submodule
imports.
"""
from __future__ import annotations


def _install_auto_execution_history_filter() -> None:
    """Keep live auto stdout focused on execution/history only.

    Auto mode used to render a full live dashboard, including the dual replay
    block that compares "if executed on time" against actual MT5 execution.
    In unattended auto execution that projection is noisy; operators only need
    the event/history stream: placements, reconciliation, SL moves, cancels,
    closes, warnings, and hard errors.

    This is intentionally scoped to the CLI `auto` subcommand so backtest,
    decide, manage, tests, and library imports keep their normal output.
    """
    import builtins
    import sys

    if "auto" not in sys.argv[1:3]:
        return
    if getattr(builtins.print, "_xauusd_auto_history_filter", False):
        return

    original_print = builtins.print
    original_stdout = sys.stdout
    state = {"dashboard": False, "reconcile": False}

    class _AutoHistoryStdout:
        """Suppress auto's screen-clear refresh while preserving real output."""

        def __init__(self, wrapped):
            self._wrapped = wrapped

        def write(self, data):
            # _run_auto_watch clears the terminal every iteration with this
            # escape sequence. Hide it so Auto behaves like an append-only
            # activity log instead of a refreshing dashboard.
            if data == "\x1b[H\x1b[J":
                return len(data)
            cleaned = data.replace("\x1b[H\x1b[J", "")
            if cleaned == "":
                return len(data)
            return self._wrapped.write(cleaned)

        def flush(self):
            return self._wrapped.flush()

        def __getattr__(self, name):
            return getattr(self._wrapped, name)

    sys.stdout = _AutoHistoryStdout(original_stdout)

    def _is_replay_detail_line(line: str) -> bool:
        stripped = line.strip()
        return (
            (stripped.startswith("#") and "(" in stripped and "):" in stripped)
            or stripped.startswith("Backtest realized so far:")
        )

    def _filter_execution_log_text(text: str) -> str | None:
        """Remove replay-only debug blocks from Auto's EXECUTION output."""
        filtered: list[str] = []
        for line in text.splitlines():
            stripped = line.strip()
            if "every entry has already played out in backtest replay" in line:
                continue
            if "partial placement --" in line and "backtest replay" in line:
                continue
            if _is_replay_detail_line(line):
                continue
            filtered.append(line)

        # If all that remains is a zero-count EXECUTION header, do not print it.
        meaningful = [line for line in filtered[1:] if line.strip()]
        if (
            filtered
            and filtered[0].startswith("EXECUTION:  placed=0  modified=0  cancelled=0  closed=0")
            and not meaningful
        ):
            return None
        return "\n".join(filtered) if filtered else None

    def history_only_print(*args, **kwargs):
        file = kwargs.get("file", sys.stdout)
        if file is not sys.stdout:
            return original_print(*args, **kwargs)

        sep = kwargs.get("sep", " ")
        text = sep.join(str(arg) for arg in args)
        stripped = text.strip()

        # Keep execution/history records, but strip replay-only debug details.
        if stripped.startswith("EXECUTION:"):
            state["dashboard"] = False
            state["reconcile"] = False
            filtered_text = _filter_execution_log_text(text)
            if filtered_text is None:
                return None
            return original_print(filtered_text, **kwargs)
        if stripped.startswith("RECONCILIATION:"):
            state["dashboard"] = False
            state["reconcile"] = True
            return original_print(*args, **kwargs)
        if state["reconcile"]:
            original_print(*args, **kwargs)
            if stripped == "":
                state["reconcile"] = False
            return None

        # Keep actionable failures/errors, but hide routine archive/loop noise.
        if stripped.startswith((
            "SANITY CHECKS FAILED",
            "[signals]",
            "[mt5]",
            "Interrupted;",
        )):
            state["dashboard"] = False
            return original_print(*args, **kwargs)
        if stripped.startswith("Archive:"):
            return None

        # Hide auto loop and dashboard/projection output.
        if text.startswith("[auto iter #"):
            return None
        if stripped == "=" * 70:
            return None
        if stripped.startswith("XAUUSD AUTO MODE"):
            state["dashboard"] = True
            return None
        if state["dashboard"]:
            return None

        return original_print(*args, **kwargs)

    history_only_print._xauusd_auto_history_filter = True
    builtins.print = history_only_print


_install_auto_execution_history_filter()

# 1. core.config
from .core.config import (
    BALANCED_LIVE_CONFIG,
    CHART_TIMEZONE_OFFSET,
    CONTRACT_SIZE_OZ,
    DEFAULT_CONFIG,
    HIGHEST_PROFIT_CONFIG,
    LOWER_EXPOSURE_CONFIG,
    POINT_VALUE,
    StrategyConfig,
)

# 2. core.chart
from .core.chart import (
    Bar,
    iter_bars,
    latest_bar,
    load_chart,
    slice_bars,
)

# 3. core.signal
from .core.signal import (
    Signal,
    compute_entries,
    parse_one_signal,
    parse_signal_line,
    parse_signals_file,
)

# 4. core.triggers
from .core.triggers import (
    fill_trigger,
    initial_stop_for_entry,
    stop_trigger,
    target_trigger,
)

# 5. core.positions
from .core.positions import (
    TERMINAL,
    Entry,
    Position,
    advance_bars,
    advance_one_bar,
    compute_lot,
    open_position,
)

# 6. io.adapters
from .io.adapters import (
    ChartSource,
    CsvChartSource,
    ManualPositionSource,
    PositionSource,
)

# 7. strategy.engine
from .strategy.engine import (
    EntryStatus,
    NewSignalPlan,
    PlannedOrder,
    PositionStatus,
    Recommendation,
    decide,
    format_replay_outcome,
    render_report,
)

# 8. strategy.backtest
from .strategy.backtest import (
    position_status,
    replay_signal,
    run_backtest,
    write_backtest_outputs,
)

# 9. io.mt5_adapter
# Module imports cleanly on any OS; MetaTrader5 is lazy-imported inside
# Mt5Connection.__init__, not at module load.
# _MT5_EXPORT_COLUMNS and _merge_with_existing are private archive helpers
# re-exported here so tests/test_archive.py can import them from the
# package root; they are not part of the public surface (not in __all__).
from .io.mt5_adapter import (
    Mt5ChartSource,
    Mt5Connection,
    archive_m1_by_month,
    mt5_equity,
    mt5_open_positions_summary,
    render_archive_summary,
    _MT5_EXPORT_COLUMNS,
    _merge_with_existing,
)

# 10. notifications (leaf -- engine -> listener Saved Messages stream)
from .notifications import (
    DEFAULT_NOTIFICATIONS_PATH,
    Notifier,
    summarize_closed_position,
)

# 11. forensic (leaf -> JSONL post-mortem log)
from .forensic import (
    DEFAULT_FORENSIC_PATH,
    ForensicLog,
)

# 12. execution.mt5_executor
from .execution.mt5_executor import (
    ExecutionLog,
    SignalRegistry,
    render_execution_log,
    round_lot,
    signal_to_magic,
)
from .execution.mt5_executor_tp2 import Mt5Executor


def _install_auto_live_limit_guard() -> None:
    """Skip stale/marketable LIMIT orders before Auto calls MT5 order_send."""
    import sys

    if "auto" not in sys.argv[1:3]:
        return
    if getattr(Mt5Executor.place_signal, "_xauusd_auto_live_limit_guard", False):
        return

    original_place_signal = Mt5Executor.place_signal
    skipped_entries: set[str] = set()
    failed_signals: set[str] = set()

    def _entry_key(signal_key: str, entry_index: int) -> str:
        return f"{signal_key}.{entry_index + 1}"

    def guarded_place_signal(self, signal, plan):
        if signal.signal_key in failed_signals:
            return ExecutionLog()

        log = ExecutionLog()
        tick = self.mt5.symbol_info_tick(self.symbol)
        if tick is None or tick.bid <= 0 or tick.ask <= 0:
            place_log = original_place_signal(self, signal, plan)
            if place_log.placed == 0 and (place_log.actions or place_log.warnings):
                failed_signals.add(signal.signal_key)
            return place_log

        bid = float(tick.bid)
        ask = float(tick.ask)
        valid_orders = []
        for order in plan.orders:
            key = _entry_key(signal.signal_key, order.entry_index)
            price = float(order.entry_price)
            stale_reason = None
            if signal.side == "BUY" and price >= ask:
                stale_reason = f"stale BUY LIMIT {price:g} >= live ask {ask:g}"
            elif signal.side == "SELL" and price <= bid:
                stale_reason = f"stale SELL LIMIT {price:g} <= live bid {bid:g}"

            if stale_reason is None:
                valid_orders.append(order)
                continue

            if key not in skipped_entries:
                log.actions.append(f"  {key}: skipped {stale_reason}")
                skipped_entries.add(key)

        if not valid_orders:
            return log

        original_orders = plan.orders
        plan.orders = valid_orders
        try:
            place_log = original_place_signal(self, signal, plan)
        finally:
            plan.orders = original_orders

        log.merge(place_log)
        if place_log.placed == 0 and (place_log.actions or place_log.warnings):
            failed_signals.add(signal.signal_key)
            log.actions.append(
                f"Signal {signal.signal_key}: placement failed; skipped further "
                f"retries in this Auto run. Restart Auto to retry manually."
            )
        return log

    guarded_place_signal._xauusd_auto_live_limit_guard = True
    Mt5Executor.place_signal = guarded_place_signal


_install_auto_live_limit_guard()


__all__ = [
    # core.config
    "BALANCED_LIVE_CONFIG",
    "CHART_TIMEZONE_OFFSET",
    "CONTRACT_SIZE_OZ",
    "DEFAULT_CONFIG",
    "HIGHEST_PROFIT_CONFIG",
    "LOWER_EXPOSURE_CONFIG",
    "POINT_VALUE",
    "StrategyConfig",
    # core.chart
    "Bar",
    "iter_bars",
    "latest_bar",
    "load_chart",
    "slice_bars",
    # core.signal
    "Signal",
    "compute_entries",
    "parse_one_signal",
    "parse_signal_line",
    "parse_signals_file",
    # core.triggers
    "fill_trigger",
    "initial_stop_for_entry",
    "stop_trigger",
    "target_trigger",
    # core.positions
    "TERMINAL",
    "Entry",
    "Position",
    "advance_bars",
    "advance_one_bar",
    "compute_lot",
    "open_position",
    # io.adapters
    "ChartSource",
    "CsvChartSource",
    "ManualPositionSource",
    "PositionSource",
    # strategy.engine
    "EntryStatus",
    "NewSignalPlan",
    "PlannedOrder",
    "PositionStatus",
    "Recommendation",
    "decide",
    "format_replay_outcome",
    "render_report",
    # strategy.backtest
    "position_status",
    "replay_signal",
    "run_backtest",
    "write_backtest_outputs",
    # io.mt5_adapter
    "Mt5ChartSource",
    "Mt5Connection",
    "archive_m1_by_month",
    "mt5_equity",
    "mt5_open_positions_summary",
    "render_archive_summary",
    # notifications
    "DEFAULT_NOTIFICATIONS_PATH",
    "Notifier",
    "summarize_closed_position",
    # forensic
    "DEFAULT_FORENSIC_PATH",
    "ForensicLog",
    # execution.mt5_executor
    "ExecutionLog",
    "Mt5Executor",
    "SignalRegistry",
    "render_execution_log",
    "round_lot",
    "signal_to_magic",
]
