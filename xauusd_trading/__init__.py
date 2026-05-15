"""xauusd_trading -- public re-export surface.

Single point of contact for the package. Both internal submodules and
external callers (cli.py, tests/, tools/sweep.py, listener/) import
their dependencies from `xauusd_trading` directly rather than reaching
into the nested module paths. That keeps import paths stable across
internal refactors: moving a function from one submodule to another
only requires updating this file.

Re-exports are ordered by dependency. Each block can only reference
names defined by blocks above it. Submodules import from
`xauusd_trading` (e.g. `from xauusd_trading import POINT_VALUE`), and
Python adds each name to this package's namespace incrementally as the
imports below execute. Reordering will cause an ImportError from a
submodule complaining it can't find a name in `xauusd_trading`.

If a new symbol is added to a submodule and needs to be visible to
other submodules or to callers, add it to the matching block here and
to `__all__`.
"""
from __future__ import annotations

# ---------------------------------------------------------------------------
# 1. core.config -- constants + StrategyConfig (no internal deps)
# ---------------------------------------------------------------------------
from .core.config import (
    CHART_TIMEZONE_OFFSET,
    CONTRACT_SIZE_OZ,
    DEFAULT_CONFIG,
    POINT_VALUE,
    StrategyConfig,
)

# ---------------------------------------------------------------------------
# 2. core.chart -- Bar + MT5 M1 CSV loader (needs POINT_VALUE)
# ---------------------------------------------------------------------------
from .core.chart import (
    Bar,
    iter_bars,
    latest_bar,
    load_chart,
    slice_bars,
)

# ---------------------------------------------------------------------------
# 3. core.signal -- Signal dataclass + parser + compute_entries
#    (needs CHART_TIMEZONE_OFFSET; references StrategyConfig only via
#    TYPE_CHECKING so no runtime dep)
# ---------------------------------------------------------------------------
from .core.signal import (
    Signal,
    compute_entries,
    parse_one_signal,
    parse_signal_line,
    parse_signals_file,
)

# ---------------------------------------------------------------------------
# 4. core.triggers -- spread-aware fill / SL / TP predicates
#    (used by core.positions via relative import; re-exported for any
#    external tooling that needs them)
# ---------------------------------------------------------------------------
from .core.triggers import (
    fill_trigger,
    initial_stop_for_entry,
    stop_trigger,
    target_trigger,
)

# ---------------------------------------------------------------------------
# 5. core.positions -- Entry, Position, advance_one_bar, sizing
#    (needs StrategyConfig, Signal, compute_entries via xauusd_trading;
#    Bar + triggers via relative imports)
# ---------------------------------------------------------------------------
from .core.positions import (
    TERMINAL,
    Entry,
    Position,
    advance_bars,
    advance_one_bar,
    compute_lot,
    open_position,
)

# ---------------------------------------------------------------------------
# 6. io.adapters -- ChartSource / PositionSource bases + CSV impls
#    (needs Bar / load_chart / slice_bars / iter_bars / latest_bar / Position)
# ---------------------------------------------------------------------------
from .io.adapters import (
    ChartSource,
    CsvChartSource,
    ManualPositionSource,
    PositionSource,
)

# ---------------------------------------------------------------------------
# 7. strategy.engine -- decide() + render_report() + plan dataclasses
#    (needs ChartSource, PositionSource, config, Position/Entry,
#    Signal/compute_entries)
# ---------------------------------------------------------------------------
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

# ---------------------------------------------------------------------------
# 8. strategy.backtest -- historical replay + report writers
#    (needs CsvChartSource, position lifecycle, Signal)
# ---------------------------------------------------------------------------
from .strategy.backtest import (
    position_status,
    replay_signal,
    run_backtest,
    write_backtest_outputs,
)

# ---------------------------------------------------------------------------
# 9. io.mt5_adapter -- Live MT5 chart, equity, archive (Windows-only at
#    runtime, but the MODULE imports cleanly on any OS because MetaTrader5
#    is lazy-imported inside Mt5Connection.__init__, not at module load)
#    (needs ChartSource, Bar, POINT_VALUE)
# ---------------------------------------------------------------------------
from .io.mt5_adapter import (
    Mt5ChartSource,
    Mt5Connection,
    archive_m1_by_month,
    mt5_equity,
    mt5_open_positions_summary,
    render_archive_summary,
)

# ---------------------------------------------------------------------------
# 10. execution.mt5_executor -- placement + management + reconcile +
#     late-TP1 catch-up
#     (needs StrategyConfig, NewSignalPlan, Mt5Connection, Position
#     lifecycle, Signal)
# ---------------------------------------------------------------------------
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
    # execution.mt5_executor
    "ExecutionLog",
    "Mt5Executor",
    "SignalRegistry",
    "render_execution_log",
    "round_lot",
    "signal_to_magic",
]