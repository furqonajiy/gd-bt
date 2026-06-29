"""parse_backtest must resolve Per-Entry columns by HEADER NAME, not fixed index.

The hybrid (tick) backtest inserts a ``Data Source`` column (TICK/M1) that the
pure-M1 workbook lacks, shifting the whole EXECUTED block one to the right. The
old fixed-index parser then read Status from the Lot column and Exit Price from
the Exit Time column, so every leg on a tick workbook looked unfilled and NOTHING
matched the live report. These tests pin that both layouts parse correctly.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import openpyxl

ROOT = Path(__file__).resolve().parents[1]


def _load():
    spec = importlib.util.spec_from_file_location(
        "rec", ROOT / "tools" / "reconcile_report_html.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


rec = _load()

# Column NAMES in the 2nd header row, in order, for each layout. The hybrid sheet
# carries "Data Source" after "Side"; the M1 sheet does not (everything shifts).
_M1_COLS = ["Entry Key", "Date", "Time (GMT+7)", "Time (chart EET/EEST)", "Side",
            "Range Low", "Range High", "Orig SL", "TP1", "TP2", "TP3", "Target",
            "Entry Price", "Effective SL", "Lot", "Status", "Fill Time",
            "Exit Time", "Exit Price", "P&L ($)", "R:R", "R"]
_HYBRID_COLS = _M1_COLS[:5] + ["Data Source"] + _M1_COLS[5:]


def _write_workbook(path: Path, columns: list[str], leg: dict) -> None:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Per-Entry Detail"
    ws.append(["SIGNAL"] + [None] * (len(columns) - 1))  # row 1: group banner
    ws.append(columns)                                    # row 2: column names
    ws.append([leg.get(c) for c in columns])              # row 3: one data leg
    wb.save(path)


def _leg_row(data_source: bool) -> dict:
    row = {
        "Entry Key": "2026-06-29#02.1", "Side": "SELL", "Orig SL": 4082.5,
        "TP1": 4070, "TP2": 4060, "TP3": 4045, "Target": 4060,
        "Entry Price": 4057.43, "Lot": 0.05, "Status": "SL",
        "Exit Time": "2026-06-29 13:10", "Exit Price": 4050.00, "P&L ($)": -37.15,
    }
    if data_source:
        row["Data Source"] = "TICK"
    return row


def test_parse_hybrid_workbook_reads_shifted_columns(tmp_path):
    p = tmp_path / "HYB_202606.xlsx"
    _write_workbook(p, _HYBRID_COLS, _leg_row(data_source=True))
    bt = rec.parse_backtest(str(p))
    assert "0629#02.1" in bt
    leg = bt["0629#02.1"]
    # The fields that the old fixed indices got WRONG on a hybrid sheet:
    assert leg["status"] == "SL"            # was reading "Lot" (0.05)
    assert leg["entry_p"] == 4057.43        # was reading "Target"
    assert leg["exit_p"] == 4050.00         # was reading "Exit Time" (None)
    assert leg["pnl"] == -37.15
    assert leg["side"] == "SELL"
    # _bt_fixed must now be computable (entry+exit present) so the leg can match.
    assert rec._bt_fixed(leg, 1.0) is not None


def test_parse_m1_workbook_still_parses(tmp_path):
    p = tmp_path / "M1_202606.xlsx"
    _write_workbook(p, _M1_COLS, _leg_row(data_source=False))
    bt = rec.parse_backtest(str(p))
    leg = bt["0629#02.1"]
    assert leg["status"] == "SL"
    assert leg["entry_p"] == 4057.43
    assert leg["exit_p"] == 4050.00
    assert leg["side"] == "SELL"


def test_bt_fixed_signs_buy_and_sell(tmp_path):
    # A SELL that exits below entry is a WIN (+); a BUY that exits below is a loss.
    sell = {"side": "SELL", "entry_p": 4057.43, "exit_p": 4050.0}
    buy = {"side": "BUY", "entry_p": 4050.0, "exit_p": 4057.43}
    assert rec._bt_fixed(sell, 1.0) > 0
    assert rec._bt_fixed(buy, 1.0) > 0
    assert rec._bt_fixed({"side": "BUY", "entry_p": 4057.43, "exit_p": 4050.0}, 1.0) < 0
