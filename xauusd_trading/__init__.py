"""xauusd_trading — public re-export surface.

All callers (internal submodules, cli, tests, tools, listener) import
from `xauusd_trading` directly rather than from nested module paths.
Moving a function between files only requires updating this file.

Re-exports are ordered by dependency: each block can only reference
names defined by blocks above it. Reordering will break submodule
imports.
"""
from __future__ import annotations

# 1. core.config
from .core.config import (
    CHART_TIMEZONE_OFFSET,
    CONTRACT_SIZE_OZ,
    DEFAULT_CONFIG,
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

# 11. forensic (leaf -- engine -> JSONL post-mortem log)
from .forensic import (
    DEFAULT_FORENSIC_PATH,
    ForensicLog,
)

# 12. execution.mt5_executor
from .execution.mt5_executor import (
    ExecutionLog,
    Mt5Executor,
    SignalRegistry,
    render_execution_log,
    round_lot,
    signal_to_magic,
)


__all__ = [
    # core.config
    "CHART_TIMEZONE_OFFSET",
    "CONTRACT_SIZE_OZ",
    "DEFAULT_CONFIG",
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