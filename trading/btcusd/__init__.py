"""BTC self-rejection strategy layer.

Reuses the shared engine, executor, parser, signal generator, notifications, and
forensic from `trading.engine`. Only the BTC strategy -- its parameters and
symbol constants -- lives here (see strategy.py). Import direction is one-way:
trading.btcusd depends on trading.engine, never the reverse.
"""
from .strategy import (
    BTC_MOMENTUM_CONFIG,
    BTC_MOMENTUM_M15_CONFIG,
    BTC_REJECTION_CONFIG,
    BTC_SPEC,
    BTC_SPEC_CONFIGURED,
    BTC_STRATEGY_CONFIG,
    assert_configured,
)

__all__ = [
    "BTC_SPEC",
    "BTC_MOMENTUM_CONFIG",
    "BTC_MOMENTUM_M15_CONFIG",
    "BTC_SPEC_CONFIGURED",
    "BTC_REJECTION_CONFIG",
    "BTC_STRATEGY_CONFIG",
    "assert_configured",
]