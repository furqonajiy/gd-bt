from __future__ import annotations

import importlib.util
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _load_tool():
    spec = importlib.util.spec_from_file_location(
        "regime_calibration_report", ROOT / "tools" / "regime_calibration_report.py"
    )
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


rc = _load_tool()


def _synthetic_month(month: str, *, base: float, wiggle: float, drift: float = 0.0) -> pd.DataFrame:
    start = datetime.strptime(month + "-03", "%Y-%m-%d")
    idx = pd.date_range(start, periods=60 * 24 * 3, freq="1min")
    close = [base + i * drift + ((i % 2) * wiggle * 0.2) for i in range(len(idx))]
    return pd.DataFrame(
        {
            "open": close,
            "high": [v + wiggle for v in close],
            "low": [v - wiggle for v in close],
            "close": close,
        },
        index=idx,
    )


def test_monthly_features_measure_atr_and_trend():
    chart = _synthetic_month("2025-01", base=2600.0, wiggle=2.5, drift=0.01)
    rows = rc.monthly_features(chart, atr_period=14)
    assert len(rows) == 1
    row = rows[0]
    assert row["month"] == "2025-01"
    assert row["bars"] == 60 * 24 * 3
    assert row["m15_atr_mean"] > 0.0
    assert row["monthly_return_pct"] > 0.0
    assert row["trend_atr_multiple"] > 0.0


def test_kmeans_1d_returns_sorted_centers_and_assignments():
    centers, assignments = rc.kmeans_1d([2.0, 2.2, 5.8, 6.0, 12.0, 12.5], k=3)
    assert centers == sorted(centers)
    assert assignments[0] == assignments[1]
    assert assignments[2] == assignments[3]
    assert assignments[4] == assignments[5]
    assert rc.learned_boundaries(centers)[0] < rc.learned_boundaries(centers)[1]


def test_annotate_months_compares_learned_router_and_calendar():
    rows = [
        {"month": "2022-01", "bars": 1000, "first_close": 1800.0, "last_close": 1790.0,
         "high": 1820.0, "low": 1780.0, "m15_atr_mean": 2.0, "m15_atr_median": 2.0,
         "m15_atr_p90": 2.4, "monthly_return_pct": -0.5, "range_pct": 2.2, "trend_atr_multiple": -5.0},
        {"month": "2024-01", "bars": 1000, "first_close": 2000.0, "last_close": 2070.0,
         "high": 2090.0, "low": 1980.0, "m15_atr_mean": 3.0, "m15_atr_median": 3.0,
         "m15_atr_p90": 3.4, "monthly_return_pct": 3.5, "range_pct": 5.5, "trend_atr_multiple": 23.3},
        {"month": "2025-01", "bars": 1000, "first_close": 2600.0, "last_close": 2680.0,
         "high": 2700.0, "low": 2580.0, "m15_atr_mean": 6.0, "m15_atr_median": 6.0,
         "m15_atr_p90": 7.0, "monthly_return_pct": 3.1, "range_pct": 4.6, "trend_atr_multiple": 13.3},
        {"month": "2026-01", "bars": 1000, "first_close": 3300.0, "last_close": 3450.0,
         "high": 3500.0, "low": 3250.0, "m15_atr_mean": 13.0, "m15_atr_median": 13.0,
         "m15_atr_p90": 15.0, "monthly_return_pct": 4.5, "range_pct": 7.6, "trend_atr_multiple": 11.5},
    ]
    annotated, meta = rc.annotate_months(rows, boundary_ambiguity_pct=5.0)
    assert [row.learned_regime for row in annotated] == ["R1quiet", "R2bull", "R3strong", "R4parab"]
    assert annotated[0].calendar_regime == "R1quiet"
    assert annotated[1].router_regime == "R2bull"
    assert annotated[2].router_regime == "R3strong"
    assert annotated[3].router_regime == "R4parab"
    assert len(meta["learned_centers"]) == 4
    assert len(meta["learned_boundaries"]) == 3


def test_calendar_regime_boundaries():
    assert rc.calendar_regime("2021-10") == "pre_real_m1"
    assert rc.calendar_regime("2021-11") == "R1quiet"
    assert rc.calendar_regime("2023-09") == "R1quiet"
    assert rc.calendar_regime("2023-10") == "R2bull"
    assert rc.calendar_regime("2025-01") == "R3strong"
    assert rc.calendar_regime("2026-01") == "R4parab"


def test_render_markdown_includes_agreement_and_monthly_map():
    rows = [
        {"month": "2022-01", "bars": 1000, "first_close": 1800.0, "last_close": 1790.0,
         "high": 1820.0, "low": 1780.0, "m15_atr_mean": 2.0, "m15_atr_median": 2.0,
         "m15_atr_p90": 2.4, "monthly_return_pct": -0.5, "range_pct": 2.2, "trend_atr_multiple": -5.0},
        {"month": "2024-01", "bars": 1000, "first_close": 2000.0, "last_close": 2070.0,
         "high": 2090.0, "low": 1980.0, "m15_atr_mean": 3.0, "m15_atr_median": 3.0,
         "m15_atr_p90": 3.4, "monthly_return_pct": 3.5, "range_pct": 5.5, "trend_atr_multiple": 23.3},
        {"month": "2025-01", "bars": 1000, "first_close": 2600.0, "last_close": 2680.0,
         "high": 2700.0, "low": 2580.0, "m15_atr_mean": 6.0, "m15_atr_median": 6.0,
         "m15_atr_p90": 7.0, "monthly_return_pct": 3.1, "range_pct": 4.6, "trend_atr_multiple": 13.3},
        {"month": "2026-01", "bars": 1000, "first_close": 3300.0, "last_close": 3450.0,
         "high": 3500.0, "low": 3250.0, "m15_atr_mean": 13.0, "m15_atr_median": 13.0,
         "m15_atr_p90": 15.0, "monthly_return_pct": 4.5, "range_pct": 7.6, "trend_atr_multiple": 11.5},
    ]
    annotated, meta = rc.annotate_months(rows)
    md = rc.render_markdown(annotated, meta)
    assert "# Regime Calibration Report" in md
    assert "## Agreement" in md
    assert "## Monthly Map" in md
    assert "2026-01" in md
    assert "R4parab" in md
