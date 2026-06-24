"""tools/parse_tick_run.py: collapse a tick_backtest dump into a score JSON."""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
for p in (str(_ROOT), str(_ROOT / "tools")):
    if p not in sys.path:
        sys.path.insert(0, p)

from parse_tick_run import parse  # noqa: E402

_DUMP = """ticks: 123 rows
=== 2026-06-22#01 ===
  0622#01.1 BUY open=4000.00 close=4010.00 lot=0.02 pnl=$20.00 reason=TP (01:00:00->02:00:00)
  TICK  : trading=$20.00 + bonus=$0.10 => $20.10
  M1    : status=WIN realized=$25.00  (engine baseline; bonus excl.)
=== 2026-06-22#02 ===
  0622#02.1 SELL open=4000.00 close=4005.00 lot=0.02 pnl=$-10.00 reason=SL (03:00:00->03:30:00)
  TICK  : trading=$-10.00 + bonus=$0.00 => $-10.00
  M1    : status=LOSS realized=$-8.00  (engine baseline; bonus excl.)
=== 2026-06-23#01 ===
  0623#01.1 BUY open=4000.00 close=4000.00 lot=0.00 pnl=$0.00 reason=market_close (04:00:00->05:00:00)
  TICK  : trading=$0.00 + bonus=$0.00 => $0.00
  M1    : status=LOSS realized=$0.00  (engine baseline; bonus excl.)
"""


def test_parse_totals_reasons_and_gap():
    s = parse(_DUMP)
    assert s["tick_pnl"] == 10.0           # 20 - 10 + 0
    assert s["m1_pnl"] == 17.0             # 25 - 8 + 0
    assert s["gap"] == 7.0                 # m1 - tick (M1 over-states)
    assert s["n_signals"] == 3
    assert s["n_nofill"] == 1              # the $0.00 signal
    assert s["reasons"] == {"TP": 1, "SL": 1, "market_close": 1}


def test_drawdown_proxy_is_peak_to_trough_on_key_order():
    # key order: 0622#01 (+20) -> 0622#02 (-10) -> 0623#01 (0)
    # equity: 20, 10, 10 ; peak 20, trough after = 10 -> DD 10
    s = parse(_DUMP)
    assert s["max_drawdown"] == 10.0
