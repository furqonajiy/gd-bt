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
    state = {"dashboard": False, "reconcile": False}

    def history_only_print(*args, **kwargs):
        file = kwargs.get("file", sys.stdout)
        if file is not sys.stdout:
            return original_print(*args, **kwargs)

        sep = kwargs.get("sep", " ")
        text = sep.join(str(arg) for arg in args)
        stripped = text.strip()

        # Keep execution/history records.
        if stripped.startswith("EXECUTION:"):
            state["dashboard"] = False
            state["reconcile"] = False
            return original_print(*args, **kwargs)
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
