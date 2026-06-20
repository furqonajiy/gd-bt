"""Decision wrapper that carries trailing settings into live execution.

The existing engine already builds the normal NewSignalPlan.  MT5 execution also
needs to know whether a signal is using virtual trailing-open entries, otherwise
it would place regular broker LIMITs and defeat the mechanic.  Dataclasses in
this project are not slotted, so attaching metadata keeps the public plan shape
backwards compatible.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from trading.engine import ChartSource, PositionSource
from trading.engine import CONTRACT_SIZE_OZ, DEFAULT_CONFIG, StrategyConfig
from .engine import Recommendation, decide as _decide


def decide(
        signal,
        chart: ChartSource,
        positions: PositionSource,
        config: StrategyConfig = DEFAULT_CONFIG,
        *,
        now: Optional[datetime] = None,
        contract_size: float = CONTRACT_SIZE_OZ,
) -> Recommendation:
    rec = _decide(
        signal,
        chart,
        positions,
        config,
        now=now,
        contract_size=contract_size,
    )
    setattr(rec.new_signal, "trailing_open_distance", float(getattr(config, "trailing_open_distance", 0.0) or 0.0))
    setattr(rec.new_signal, "trailing_close_distance", float(getattr(config, "trailing_close_distance", 0.0) or 0.0))
    return rec
