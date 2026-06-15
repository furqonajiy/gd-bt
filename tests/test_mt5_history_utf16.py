"""MT5 'Report History' HTML exports are UTF-16-LE with a BOM.

The reader used to decode them as UTF-8, producing NUL-laced garbage in which
the HTML parser found zero tags — parse_mt5_history then failed "no deals
table" even though the table was there (hit during the 2026-06-12
reconciliation). The reader now sniffs the BOM; UTF-8 exports keep working.
"""
from __future__ import annotations

from xauusd_trading.reporting.mt5_history import _read_html_rows, parse_mt5_history

_HTML = """<html><body><table>
<tr><th>Time</th><th>Comment</th><th>Price</th><th>Profit</th></tr>
<tr><td>2026.06.12 01:00:00</td><td>2026-06-12#10.1</td><td>4218.00</td><td>9.13</td></tr>
</table></body></html>"""


def _check(path):
    rows = _read_html_rows(path)
    assert ["Time", "Comment", "Price", "Profit"] in rows
    parsed = parse_mt5_history(path)
    assert parsed["2026-06-12#10.1"]["profit"] == 9.13


def test_utf16_report_html_parses(tmp_path):
    p = tmp_path / "ReportHistory.html"
    p.write_bytes(_HTML.encode("utf-16"))  # writes the BOM, like the terminal
    _check(p)


def test_utf8_report_html_still_parses(tmp_path):
    p = tmp_path / "report.html"
    p.write_text(_HTML, encoding="utf-8")
    _check(p)
