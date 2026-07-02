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

    The broker TP is dropped only for a trailing-close RUNNER -- a strategy that
    both trails (``trailing_close_distance > 0``) AND runs past the final target
    (``runner_no_final_cap`` / ``--runner-final-cap none``). For a runner, TP3
    stays a model/reference level and live MT5 exits by the executor-owned trailing
    SL only.

    A trailing-close strategy that STILL caps at its final target keeps its broker
    TP. This is the same switch (``runner_no_final_cap``) the engine uses to skip
    the final-target close, so live and the replay agree: if the engine still banks
    at TP3, live must too -- and the broker TP is the ONLY thing that closes a leg
    at its target (the executor manage has no final-target close). Dropping it for a
    capped book would leave the leg riding past a target the backtest banks at,
    with a stale SL, until the max-hold timer -- the exact live/replay drift this
    gate prevents.
    """
    runs_past_target = bool(getattr(config, "runner_no_final_cap", False))
    has_trailing_close = float(getattr(config, "trailing_close_distance", 0.0) or 0.0) > 0
    if runs_past_target and has_trailing_close:
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
