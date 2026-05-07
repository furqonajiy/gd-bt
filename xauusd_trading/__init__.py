"""xauusd_trading -- validated XAUUSD strategy as a reusable engine.

Public entry points:

    from xauusd_trading import (
        decide, render_report, run_backtest,
        DEFAULT_CONFIG, StrategyConfig,
        Signal, parse_signals_file, parse_one_signal,
        CsvChartSource, ManualPositionSource,
    )

The same `core` modules (signal, chart, triggers, positions) power both the
backtest runner and the live decision engine, so behavior is guaranteed
identical.
"""
from .config import DEFAULT_CONFIG, StrategyConfig
from .signal import Signal, parse_signals_file, parse_one_signal
from .chart import Bar
from .adapters import (
    ChartSource, CsvChartSource,
    PositionSource, ManualPositionSource,
)
from .positions import Entry, Position, open_position, advance_one_bar, advance_bars
from .engine import (
    decide, render_report, Recommendation, NewSignalPlan,
    PositionStatus, EntryStatus, PlannedOrder,
)
from .backtest import run_backtest, replay_signal, position_status

# Optional MT5 executor exports (Windows-only; soft-fail on import).
try:
    from .mt5_executor import (
        Mt5Executor, SignalRegistry, signal_to_magic,
        round_lot, ExecutionLog, render_execution_log,
    )
    _MT5_EXEC_EXPORTS = [
        "Mt5Executor", "SignalRegistry", "signal_to_magic",
        "round_lot", "ExecutionLog", "render_execution_log",
    ]
except Exception:
    _MT5_EXEC_EXPORTS = []

__all__ = [
    "DEFAULT_CONFIG", "StrategyConfig",
    "Signal", "parse_signals_file", "parse_one_signal", "Bar",
    "ChartSource", "CsvChartSource", "PositionSource", "ManualPositionSource",
    "Entry", "Position", "open_position", "advance_one_bar", "advance_bars",
    "decide", "render_report",
    "Recommendation", "NewSignalPlan", "PositionStatus", "EntryStatus", "PlannedOrder",
    "run_backtest", "replay_signal", "position_status",
] + _MT5_EXEC_EXPORTS
