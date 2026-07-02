#!/usr/bin/env python3
"""Shared Victor corrected-R:R rewrite core (ONE code path, live == backtest).

Victor's posted TP1 risk:reward collapsed to ~0.5-0.67 outside his Feb-Jun 2026
era. Both the batch backtest feed (``tools/generate_victor_rr_feed.py``) and the
LIVE provider filter (``tools/live_provider_signal_filter.py --rewrite-rr*``)
rewrite each signal's TP1/TP2/TP3 to a consistent asymmetric ladder off the
POSTED entry-edge and SL. This module is the single source of that math + number
formatting, so a signal rewritten live is byte-identical to the same signal
rewritten into the backtest feed -- that identity is the live/backtest parity
contract for the V073A book. Do not re-derive the ladder anywhere else.

    entry_edge = max(r1, r2) for BUY, min(r1, r2) for SELL   (range_high/low)
    risk       = |entry_edge - SL|                            (NOMINAL, posted SL)
    TPk        = entry_edge + rrk * risk (BUY) / entry_edge - rrk * risk (SELL)

A line whose risk is <= 0 or exceeds ``max_risk`` points is LEFT AS-POSTED
(returns None): those are provider SL typos (the wrong-hundreds ~100-pt shifts
and the extra-digit case ``apply_signal_corrections`` repairs live) and
rewriting TPs off a phantom 100+-pt risk would be nonsense. Keeping them verbatim
means the live feed and the backtest feed treat the typo lines identically.
"""
from __future__ import annotations

# ~3x Victor's widest real stop; above this a line is a provider SL typo and is
# left as-posted (see module docstring). Shared default for both feed paths.
DEFAULT_MAX_RISK = 30.0


def fmt_price(x: float) -> str:
    """Feed-style number: whole -> '4093', else 2dp with trailing zeros trimmed.

    IDENTICAL formatting on both the live and backtest paths so the rewritten TP
    strings match byte-for-byte."""
    v = round(float(x), 2)
    if v == int(v):
        return str(int(v))
    return f"{v:.2f}".rstrip("0").rstrip(".")


def entry_edge(side: str, r1: float, r2: float) -> float:
    """range_high for BUY, range_low for SELL -- the engine's entry_edge."""
    return max(r1, r2) if side.upper() == "BUY" else min(r1, r2)


def rewrite_tps(side: str, r1, r2, sl, rr1: float, rr2: float, rr3: float,
                max_risk: float = DEFAULT_MAX_RISK) -> tuple[str, str, str] | None:
    """Return the (tp1, tp2, tp3) ladder as FORMATTED strings, or ``None`` to
    leave the signal's TPs as posted (risk <= 0 or risk > ``max_risk``).

    ``r1``/``r2``/``sl`` accept str or float (feed fields are strings)."""
    side = side.upper()
    e1, e2, slf = float(r1), float(r2), float(sl)
    edge = entry_edge(side, e1, e2)
    risk = abs(edge - slf)
    if risk <= 0.0 or risk > max_risk:
        return None
    sign = 1.0 if side == "BUY" else -1.0
    return (
        fmt_price(edge + sign * rr1 * risk),
        fmt_price(edge + sign * rr2 * risk),
        fmt_price(edge + sign * rr3 * risk),
    )
