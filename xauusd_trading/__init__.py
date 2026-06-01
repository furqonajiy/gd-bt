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
    """Keep live auto stdout focused on execution/history only."""
    import builtins
    import sys

    if "auto" not in sys.argv[1:3]:
        return
    if getattr(builtins.print, "_xauusd_auto_history_filter", False):
        return

    original_print = builtins.print
    original_stdout = sys.stdout
    state = {"dashboard": False, "reconcile": False, "auto_started": False}

    class _AutoHistoryStdout:
        """Suppress auto's screen-clear refresh while preserving real output."""

        def __init__(self, wrapped):
            self._wrapped = wrapped
            self._clear_seq = chr(27) + "[H" + chr(27) + "[J"

        def write(self, data):
            if data == self._clear_seq:
                return len(data)
            cleaned = data.replace(self._clear_seq, "")
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
        filtered: list[str] = []
        for line in text.splitlines():
            if "every entry has already played out in backtest replay" in line:
                continue
            if "partial placement --" in line and "backtest replay" in line:
                continue
            if _is_replay_detail_line(line):
                continue
            filtered.append(line)

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

        if text.startswith("[auto iter #"):
            if not state["auto_started"]:
                state["auto_started"] = True
                return original_print(text, **kwargs)
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

# 5. core.positions data classes / sizing / construction
from .core.positions import (
    TERMINAL,
    Entry,
    Position,
    compute_lot,
    open_position,
)

# 5b. shared lifecycle. This wrapper preserves the old lifecycle when trailing
# distances are 0, and adds virtual trailing-open / trailing-close when enabled.
from .core.trailing_positions import (
    advance_bars,
    advance_one_bar,
)

# 6. io.adapters
from .io.adapters import (
    ChartSource,
    CsvChartSource,
    ManualPositionSource,
    PositionSource,
)

# 7. strategy.engine public data/rendering
from .strategy.engine import (
    EntryStatus,
    NewSignalPlan,
    PlannedOrder,
    PositionStatus,
    Recommendation,
    format_replay_outcome,
    render_report,
)
from .strategy.trailing_engine import decide

# 8. strategy.backtest
from .strategy.backtest import (
    position_status,
    replay_signal,
    run_backtest,
    write_backtest_outputs,
)

# 9. io.mt5_adapter
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
    "CHART_TIMEZONE_OFFSET", "CONTRACT_SIZE_OZ", "DEFAULT_CONFIG", "POINT_VALUE", "StrategyConfig",
    "Bar", "iter_bars", "latest_bar", "load_chart", "slice_bars",
    "Signal", "compute_entries", "parse_one_signal", "parse_signal_line", "parse_signals_file",
    "fill_trigger", "initial_stop_for_entry", "stop_trigger", "target_trigger",
    "TERMINAL", "Entry", "Position", "advance_bars", "advance_one_bar", "compute_lot", "open_position",
    "ChartSource", "CsvChartSource", "ManualPositionSource", "PositionSource",
    "EntryStatus", "NewSignalPlan", "PlannedOrder", "PositionStatus", "Recommendation",
    "decide", "format_replay_outcome", "render_report",
    "position_status", "replay_signal", "run_backtest", "write_backtest_outputs",
    "Mt5ChartSource", "Mt5Connection", "archive_m1_by_month", "mt5_equity",
    "mt5_open_positions_summary", "render_archive_summary",
    "DEFAULT_NOTIFICATIONS_PATH", "Notifier", "summarize_closed_position",
    "DEFAULT_FORENSIC_PATH", "ForensicLog",
    "ExecutionLog", "Mt5Executor", "SignalRegistry", "render_execution_log", "round_lot", "signal_to_magic",
]
