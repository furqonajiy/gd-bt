from __future__ import annotations

import csv
from pathlib import Path

from tools import entry_feature_variant_report as report


def _write_leaderboard(root: Path, variant: str, rows: list[dict[str, object]]) -> None:
    out = root / variant
    out.mkdir(parents=True)
    fields = sorted({key for row in rows for key in row})
    with (out / "leaderboard.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def _row(edge: float, edge_bonus: float, oos: float, dd: float, lots: float) -> dict[str, object]:
    return {
        "fixed_no_bonus_profit": edge,
        "fixed_with_bonus_profit": edge_bonus,
        "bonus_contribution": edge_bonus - edge,
        "oos_fixed_no_bonus_profit": oos,
        "concurrent_risk_max_dd_pct": dd,
        "fixed_closed_lots": lots,
        "cfg_entry_count": 5,
        "cfg_sl_multiplier": 2.3,
        "cfg_max_hold_minutes": 90,
        "cfg_tp1_lock_delay_minutes": 20,
    }


def test_variant_best_uses_edge_bonus_before_raw_edge(tmp_path: Path) -> None:
    _write_leaderboard(
        tmp_path,
        "fast",
        [
            _row(edge=2_000, edge_bonus=2_050, oos=500, dd=20, lots=10),
            _row(edge=1_900, edge_bonus=2_500, oos=550, dd=20, lots=200),
        ],
    )

    rows = report.load_variant_winners(tmp_path, dd_gate=40)

    assert len(rows) == 1
    assert rows[0].variant == "fast"
    assert rows[0].edge_bonus == 2_500
    assert rows[0].closed_lots == 200


def test_base_verdict_requires_edge_bonus_edge_and_oos(tmp_path: Path) -> None:
    _write_leaderboard(tmp_path, "base", [_row(1_000, 1_100, 500, 20, 33.33)])
    _write_leaderboard(tmp_path, "valid", [_row(1_100, 1_250, 700, 25, 50)])
    _write_leaderboard(tmp_path, "bonus_only", [_row(900, 1_500, 900, 25, 200)])
    _write_leaderboard(tmp_path, "oos_lag", [_row(1_200, 1_300, 400, 25, 33.33)])

    rows = report.load_variant_winners(tmp_path, dd_gate=40)
    base = next(row for row in rows if row.variant == "base")
    winners = report.variants_beating_base(rows, base)

    assert [row.variant for row in winners] == ["valid"]


def test_render_report_surfaces_bonus_and_lots(tmp_path: Path) -> None:
    _write_leaderboard(tmp_path, "base", [_row(1_000, 1_100, 500, 20, 33.33)])
    _write_leaderboard(tmp_path, "valid", [_row(1_100, 1_250, 700, 25, 50)])

    text = report.render_report(
        report.load_variant_winners(tmp_path, dd_gate=40),
        title="CHECK",
        base_variant="base",
        winner_label="GLOBAL CHECK",
    )

    assert "edge+bonus$" in text
    assert "lots" in text
    assert "GLOBAL CHECK: variant=valid" in text
    assert "edge+bonus=$1,250" in text
