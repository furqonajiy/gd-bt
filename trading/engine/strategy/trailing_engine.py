"""Decision wrapper that carries trailing settings into live execution.

The existing engine already builds the normal NewSignalPlan. MT5 execution also
needs to know whether a signal is using virtual trailing-open entries and whether
a trailing-close strategy should omit broker-side TP, otherwise live placement can
defeat the shared lifecycle model. Dataclasses in this project are not slotted,
so attaching metadata keeps the public plan shape backwards compatible.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from trading.engine import ChartSource, PositionSource
from trading.engine import CONTRACT_SIZE_OZ, DEFAULT_CONFIG, StrategyConfig
from .engine import Recommendation, decide as _decide


def _broker_take_profit_price(config: StrategyConfig, final_target_price: float) -> float | None:
    """Return the broker TP to attach, or None when trailing-close owns exit.

    A trailing-close strategy should not also carry a broker TP3 cap. TP3 remains
    a model/reference level, but live MT5 exits by executor-owned SL only.
    """
    has_trailing_close = float(getattr(config, "trailing_close_distance", 0.0) or 0.0) > 0
    if has_trailing_close:
        return None
    return float(final_target_price)


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
    setattr(rec.new_signal, "runner_no_final_cap", bool(getattr(config, "runner_no_final_cap", False)))
    setattr(
        rec.new_signal,
        "broker_take_profit_price",
        _broker_take_profit_price(config, rec.new_signal.final_target_price),
    )
    return rec
