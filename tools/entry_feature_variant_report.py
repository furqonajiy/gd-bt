#!/usr/bin/env python3
"""Summarize entry-feature variant winners from aggregated leaderboards."""
from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


@dataclass(frozen=True)
class VariantWinner:
    variant: str
    edge_bonus: float
    edge: float
    bonus: float
    oos: float
    dd: float
    closed_lots: float | None
    entry_count: str
    sl_multiplier: str
    max_hold_minutes: str
    tp1_lock_delay_minutes: str
    source: Path


def _num(value) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _text(row: dict[str, str], *names: str) -> str:
    for name in names:
        value = row.get(name)
        if value not in (None, ""):
            return str(value)
    return "-"


def _closed_lots(row: dict[str, str], bonus: float | None) -> float | None:
    for key in ("fixed_closed_lots", "closed_lots"):
        value = _num(row.get(key))
        if value is not None:
            return value
    bonus_per_lot = _num(row.get("cfg_bonus_per_closed_lot"))
    if bonus is not None and bonus_per_lot and bonus_per_lot > 0:
        return bonus / bonus_per_lot
    return None


def _row_winner(variant: str, row: dict[str, str], source: Path) -> VariantWinner | None:
    edge = _num(row.get("fixed_no_bonus_profit"))
    edge_bonus = _num(row.get("fixed_with_bonus_profit"))
    oos = _num(row.get("oos_fixed_no_bonus_profit"))
    dd = _num(row.get("concurrent_risk_max_dd_pct"))
    if edge is None or oos is None or dd is None:
        return None
    edge_bonus = edge if edge_bonus is None else edge_bonus
    bonus = _num(row.get("bonus_contribution"))
    if bonus is None:
        bonus = edge_bonus - edge
    return VariantWinner(
        variant=variant,
        edge_bonus=edge_bonus,
        edge=edge,
        bonus=bonus,
        oos=oos,
        dd=dd,
        closed_lots=_closed_lots(row, bonus),
        entry_count=_text(row, "cfg_entry_count", "cfg_entries", "entry_count", "entries"),
        sl_multiplier=_text(row, "cfg_sl_multiplier", "sl_multiplier"),
        max_hold_minutes=_text(row, "cfg_max_hold_minutes", "max_hold_minutes"),
        tp1_lock_delay_minutes=_text(
            row, "cfg_tp1_lock_delay_minutes", "tp1_lock_delay_minutes"
        ),
        source=source,
    )


def _candidate_allowed(row: VariantWinner, dd_gate: float) -> bool:
    return abs(row.dd) <= dd_gate and row.oos > 0.0


def load_variant_winners(results_dir: Path, dd_gate: float) -> list[VariantWinner]:
    winners: list[VariantWinner] = []
    for csv_path in sorted(results_dir.glob("*/leaderboard.csv")):
        variant = csv_path.parent.name
        best: VariantWinner | None = None
        with csv_path.open(newline="", encoding="utf-8") as f:
            for raw in csv.DictReader(f):
                if raw.get("error"):
                    continue
                row = _row_winner(variant, raw, csv_path)
                if row is None or not _candidate_allowed(row, dd_gate):
                    continue
                key = (row.edge_bonus, row.oos, row.edge)
                if best is None or key > (best.edge_bonus, best.oos, best.edge):
                    best = row
        if best is not None:
            winners.append(best)
    winners.sort(key=lambda row: (row.edge_bonus, row.oos, row.edge), reverse=True)
    return winners


def variants_beating_base(
    rows: Iterable[VariantWinner],
    base: VariantWinner,
) -> list[VariantWinner]:
    winners = [
        row
        for row in rows
        if row.variant != base.variant
        and row.edge_bonus > base.edge_bonus
        and row.edge > base.edge
        and row.oos > base.oos
    ]
    winners.sort(key=lambda row: (row.edge_bonus, row.oos, row.edge), reverse=True)
    return winners


def _money(value: float) -> str:
    return f"{value:,.0f}"


def _closed_lots_text(value: float | None) -> str:
    if value is None:
        return "-"
    return f"{value:,.2f}".rstrip("0").rstrip(".")


def render_report(
    rows: list[VariantWinner],
    *,
    title: str,
    base_variant: str,
    winner_label: str,
) -> str:
    lines = [f"==================== {title} ===================="]
    if not rows:
        lines.append("(no DD/OOS survivor found)")
        return "\n".join(lines)

    lines.append(
        f"{'variant':12}{'edge+bonus$':>14}{'edge$':>10}{'bonus$':>9}"
        f"{'OOS$':>9}{'DD%':>7}{'lots':>9}  e/slm/hold/d"
    )
    for row in rows:
        cfg = (
            f"e{row.entry_count}/slm{row.sl_multiplier}/"
            f"h{row.max_hold_minutes}/d{row.tp1_lock_delay_minutes}"
        )
        lines.append(
            f"{row.variant:12}{_money(row.edge_bonus):>14}{_money(row.edge):>10}"
            f"{_money(row.bonus):>9}{_money(row.oos):>9}{row.dd:>7.1f}"
            f"{_closed_lots_text(row.closed_lots):>9}  {cfg}"
        )

    base = next((row for row in rows if row.variant == base_variant), None)
    if base is None:
        best = rows[0]
        lines.append("")
        lines.append(f"(no {base_variant!r} survivor to compare against)")
        lines.append(
            f"{winner_label}: variant={best.variant} edge+bonus=${_money(best.edge_bonus)} "
            f"edge=${_money(best.edge)} bonus=${_money(best.bonus)} "
            f"OOS=${_money(best.oos)} DD={best.dd:.1f}%"
        )
        return "\n".join(lines)

    winners = variants_beating_base(rows, base)
    lines.append("")
    lines.append(
        f"base: edge+bonus=${_money(base.edge_bonus)} edge=${_money(base.edge)} "
        f"bonus=${_money(base.bonus)} OOS=${_money(base.oos)} DD={base.dd:.1f}%"
    )
    if winners:
        best = winners[0]
        lines.append(
            "BEATS BASE on edge+bonus, edge, AND OOS: "
            + ", ".join(row.variant for row in winners)
        )
        lines.append(
            f"{winner_label}: variant={best.variant} edge+bonus=${_money(best.edge_bonus)} "
            f"edge=${_money(best.edge)} bonus=${_money(best.bonus)} "
            f"OOS=${_money(best.oos)} DD={best.dd:.1f}%"
        )
    else:
        lines.append(
            "NO entry-feature variant beats base on edge+bonus, edge, AND OOS "
            "-> keep base (unfiltered)."
        )
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results-dir", default="results", help="Directory with */leaderboard.csv")
    parser.add_argument("--dd-gate", type=float, default=40.0)
    parser.add_argument("--base-variant", default="base")
    parser.add_argument("--title", default="PER-VARIANT WINNERS")
    parser.add_argument("--winner-label", default="GLOBAL WINNER")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    rows = load_variant_winners(Path(args.results_dir), args.dd_gate)
    print(
        render_report(
            rows,
            title=args.title,
            base_variant=args.base_variant,
            winner_label=args.winner_label,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
