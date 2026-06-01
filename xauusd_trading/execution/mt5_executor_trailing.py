"""Trailing-aware public MT5 executor.

A broker BUY LIMIT cannot express the requested trailing-open rule: if the limit is
4750 and Ask drops through 4750 on the way to 4740, MT5 fills immediately.  The
safe live behaviour until a virtual-order worker is implemented is to refuse
normal LIMIT placement whenever trailing_open_distance is enabled.  This prevents
live execution from diverging from the trailing-open backtest.
"""
from __future__ import annotations

from .mt5_executor_tp2 import Mt5Executor as _Tp2Mt5Executor
from .mt5_executor import ExecutionLog


class Mt5Executor(_Tp2Mt5Executor):
    """MT5 executor with trailing-open safety guard."""

    def place_signal(self, signal, plan) -> ExecutionLog:
        trailing_open_distance = float(getattr(plan, "trailing_open_distance", 0.0) or 0.0)
        if trailing_open_distance > 0:
            log = ExecutionLog()
            log.actions.append(
                f"Signal {signal.signal_key}: trailing-open distance "
                f"{trailing_open_distance:g} is enabled, so normal broker LIMIT "
                f"orders are not placed. A broker LIMIT would fill immediately "
                f"if price crosses the entry on the way down/up; use the shared "
                f"backtest lifecycle for research until live virtual trailing "
                f"orders are implemented."
            )
            return log
        return super().place_signal(signal, plan)
