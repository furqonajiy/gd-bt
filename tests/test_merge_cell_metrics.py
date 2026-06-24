"""tools/merge_cell_metrics.py: merge TICK + M1 cell results into one row."""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
for p in (str(_ROOT), str(_ROOT / "tools")):
    if p not in sys.path:
        sys.path.insert(0, p)

from merge_cell_metrics import merge  # noqa: E402


def test_merge_combines_and_computes_discrepancy():
    cfg = {"id": "c001", "slm": 1.9, "entries": 8}
    tick = {"tick_pnl": 26345.0, "max_drawdown_pct": 30.0, "n_nofill": 146, "reasons": {"SL": 100}}
    m1_risk = {"net_profit": 35000.0, "trading_pnl": 33100.0, "bonus": 1900.0,
               "max_drawdown_pct": -21.77, "closed_lots": 632.99, "win_rate_pct": 56.1}
    m1_fixed = {"net_profit": 540.0}

    row = merge(cfg, tick, m1_risk, m1_fixed)
    assert row["id"] == "c001" and row["slm"] == 1.9          # config carried through
    assert row["tick_net"] == 26345.0
    assert row["m1_net"] == 35000.0 and row["m1_net_nobonus"] == 33100.0
    assert row["m1_dd_pct"] == 21.77                          # abs() of negative DD
    assert row["m1_edge_fixed"] == 540.0
    assert row["disc_m1_minus_tick"] == 8655.0                # 35000 - 26345
    assert row["disc_pct"] == 32.9                            # 8655 / 26345 * 100


def test_merge_tolerates_missing_m1():
    # a failed/absent backtest must not crash the row
    row = merge({"id": "c002"}, {"tick_pnl": 100.0}, {}, {})
    assert row["tick_net"] == 100.0
    assert row["m1_net"] is None
    assert row["disc_m1_minus_tick"] is None and row["disc_pct"] is None
