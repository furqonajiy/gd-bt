#!/usr/bin/env python3
"""Rebate-aware scoring for the TSL18 quality-entry research sweep.

"Rebate" here is the broker's **$3 / closed-lot bonus** the engine already models
(`bonus_per_closed_lot`, surfaced in the workbook Summary as *Closed-lot bonus*).
A dense, high-frequency feed can post a positive **net** P&L that is mostly rebate
while its **pure** trading P&L (fills only, no bonus) is flat or negative — i.e. it
is *rebate-farming*, not trading an edge. These helpers separate the two so the
sweep can rank candidates and **refuse to promote a rebate-farm with bad pure P&L**.

Pure functions only — no I/O, no pandas — so the math is unit-testable in isolation
and shared by `tools/sweep_tsl18_quality_entry.py`.

Metrics (all from one closed-trade run):

    pure_trading_pnl       trading-only P&L, no bonus
    rebate_pnl             closed_lots * rebate_per_lot   (the $3/lot bonus)
    net_pnl                pure_trading_pnl + rebate_pnl
    closed_lots            lots that closed (and therefore earned the rebate)
    pure_pnl_per_lot       pure_trading_pnl / closed_lots
    net_pnl_per_lot        net_pnl / closed_lots
    rebate_share_of_profit rebate_pnl / net_pnl when net > 0 (how much of the
                           profit is rebate, not trading edge)
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

# The engine's modelled broker bonus (see CLAUDE.md / TSL18 geometry).
DEFAULT_REBATE_PER_LOT = 3.0

# Selection objectives the sweep can rank by.
SCORE_OBJECTIVES = ("net_pnl", "pure_pnl", "edge_plus_rebate_guarded", "dd_adjusted_net")


@dataclass(frozen=True)
class RebateMetrics:
    pure_trading_pnl: float
    rebate_pnl: float
    net_pnl: float
    closed_lots: float
    pure_pnl_per_lot: float
    net_pnl_per_lot: float
    rebate_share_of_profit: float

    def as_dict(self) -> dict:
        return asdict(self)


def compute_rebate_metrics(pure_trading_pnl: float, closed_lots: float,
                           rebate_per_lot: float = DEFAULT_REBATE_PER_LOT,
                           rebate_pnl: float | None = None) -> RebateMetrics:
    """Build the rebate metrics from a run's pure trading P&L and closed lots.

    ``rebate_pnl`` defaults to ``closed_lots * rebate_per_lot`` but can be passed
    directly when read from a workbook (the engine's *Closed-lot bonus* total),
    so the math matches the report exactly.
    """
    pure = float(pure_trading_pnl)
    lots = float(closed_lots)
    rebate = float(rebate_pnl) if rebate_pnl is not None else lots * float(rebate_per_lot)
    net = pure + rebate
    pure_per_lot = pure / lots if lots > 0 else 0.0
    net_per_lot = net / lots if lots > 0 else 0.0
    # Share of PROFIT that is rebate — only meaningful when the run is net-green.
    # A candidate that is green ONLY because of the rebate (pure <= 0 < net) has a
    # share of 1.0; a net-loser has 0.0 (there is no profit to apportion).
    if net > 0:
        share = max(0.0, min(1.0, rebate / net))
    else:
        share = 1.0 if rebate > 0 and pure <= 0 else 0.0
    return RebateMetrics(
        pure_trading_pnl=round(pure, 2),
        rebate_pnl=round(rebate, 2),
        net_pnl=round(net, 2),
        closed_lots=round(lots, 2),
        pure_pnl_per_lot=round(pure_per_lot, 4),
        net_pnl_per_lot=round(net_per_lot, 4),
        rebate_share_of_profit=round(share, 4),
    )


def passes_rebate_guards(m: RebateMetrics, *, min_pure_trading_pnl: float = 0.0,
                         max_rebate_share_of_profit: float = 0.50) -> tuple[bool, str]:
    """(ok, reason). Reject rebate-farming candidates.

    A candidate fails if its **pure** trading P&L is below the floor (the primary
    guard — a negative-pure / positive-net rebate-only candidate is always
    rejected), or if too much of its **net** profit is rebate rather than edge.
    """
    if m.pure_trading_pnl < min_pure_trading_pnl:
        return False, "pure_pnl_below_min"
    if m.net_pnl > 0 and m.rebate_share_of_profit > max_rebate_share_of_profit:
        return False, "rebate_share_too_high"
    return True, "ok"


def score_candidate(m: RebateMetrics, objective: str = "net_pnl", *,
                    max_drawdown_pct: float = 0.0,
                    min_pure_trading_pnl: float = 0.0,
                    max_rebate_share_of_profit: float = 0.50) -> float:
    """Score a candidate for ranking under one of ``SCORE_OBJECTIVES``.

    - ``net_pnl``                 — raw net (pure + rebate); rebate-blind.
    - ``pure_pnl``                — trading edge only; ignores the rebate entirely.
    - ``edge_plus_rebate_guarded``— net **only when the rebate guards pass**, else
      the pure edge alone, so a rebate-farm scores on its (often negative) pure P&L.
    - ``dd_adjusted_net``         — net penalised by drawdown (return-per-unit-DD).
    """
    if objective not in SCORE_OBJECTIVES:
        raise ValueError(f"unknown score objective {objective!r}; choose from {SCORE_OBJECTIVES}")
    if objective == "net_pnl":
        return m.net_pnl
    if objective == "pure_pnl":
        return m.pure_trading_pnl
    if objective == "edge_plus_rebate_guarded":
        ok, _ = passes_rebate_guards(
            m, min_pure_trading_pnl=min_pure_trading_pnl,
            max_rebate_share_of_profit=max_rebate_share_of_profit)
        return m.net_pnl if ok else m.pure_trading_pnl
    # dd_adjusted_net
    dd = abs(float(max_drawdown_pct))
    return m.net_pnl / (1.0 + dd / 100.0)
