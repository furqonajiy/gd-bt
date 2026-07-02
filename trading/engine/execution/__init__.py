from __future__ import annotations

from . import mt5_executor_trailing
from .mt5_executor_uncapped_runner import Mt5Executor

mt5_executor_trailing.__dict__["Mt5Executor"] = Mt5Executor

__all__ = ["Mt5Executor"]
