"""Shared deploy objective for bonus-aware sweep ranking.

The deploy winner should be chosen on fixed-lot edge plus the broker
$3/closed-lot bonus. Risk-sized compounded net is useful context, but it is too
sensitive to late-regime compounding to decide a live champion.
"""
from __future__ import annotations

from typing import Any


def f(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def edge_profit(row: dict) -> float:
    if "edge" in row:
        return f(row.get("edge"))
    return f(row.get("fixed_no_bonus_profit"))


def bonus_profit(row: dict) -> float:
    if "bonus_contribution" in row:
        return f(row.get("bonus_contribution"))
    if "bonus" in row:
        return f(row.get("bonus"))
    if "fixed_with_bonus_profit" in row or "edge_bonus" in row:
        return fixed_with_bonus_profit(row) - edge_profit(row)
    return 0.0


def fixed_with_bonus_profit(row: dict) -> float:
    for key in ("deploy_objective", "fixed_with_bonus_profit", "edge_bonus"):
        if key in row:
            return f(row.get(key))
    bonus_keys = {"bonus_contribution", "bonus"}
    if bonus_keys.intersection(row):
        return edge_profit(row) + bonus_profit(row)
    return edge_profit(row)


def oos_profit(row: dict) -> float:
    if "oos" in row:
        return f(row.get("oos"))
    return f(row.get("oos_fixed_no_bonus_profit"))


def drawdown_pct(row: dict):
    value = row.get("dd") if "dd" in row else row.get("concurrent_risk_max_dd_pct")
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def compounded_net_bonus(row: dict) -> float:
    if "net_bonus" in row:
        return f(row.get("net_bonus"))
    return f(row.get("risk_net_profit_with_bonus"))


def closed_lots(row: dict) -> float:
    return f(row.get("closed_lots"))


def rank_key(row: dict) -> tuple[float, float, float, float, float]:
    """Higher-is-better deploy rank.

    Primary objective is fixed edge + bonus; OOS and raw fixed edge protect
    against overfit ties. Closed lots rewards the bonus mechanism only after PnL
    and OOS are already equal.
    """
    return (
        fixed_with_bonus_profit(row),
        oos_profit(row),
        edge_profit(row),
        closed_lots(row),
        compounded_net_bonus(row),
    )


def is_deploy_survivor(row: dict, dd_gate: float) -> bool:
    if row.get("error"):
        return False
    dd = drawdown_pct(row)
    if dd is None or dd > dd_gate:
        return False
    return oos_profit(row) > 0.0


def survivors(rows: list[dict], dd_gate: float) -> list[dict]:
    out = [row for row in rows if is_deploy_survivor(row, dd_gate)]
    out.sort(key=rank_key, reverse=True)
    return out


def strictly_beats(challenger: dict, incumbent: dict) -> bool:
    return rank_key(challenger) > rank_key(incumbent)
