"""Victor corrected-R:R rewrite: shared-core math + live/backtest parity.

Pins the V073A contract: the LIVE provider filter's TP rewrite and the BACKTEST
feed generator both go through tools/victor_rr_rewrite, so a signal rewritten
live is byte-identical (in TP levels) to the same signal rewritten into the
backtest feed. Also pins default-OFF byte-identity (existing Victor books
unaffected) and the SL-typo passthrough.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import live_provider_signal_filter as lf
from tools.generate_victor_rr_feed import rewrite_line
from tools.victor_rr_rewrite import fmt_price, rewrite_tps


# --------------------------------------------------------------------------- #
# shared-core math
# --------------------------------------------------------------------------- #
def test_buy_ladder_off_entry_edge_and_sl():
    # BUY: entry_edge = max(range) = 4543, SL 4536 -> risk 7.
    # TP1 = 4543 + 1.5*7 = 4553.5 ; TP2 = +3*7 = 4564 ; TP3 = +5*7 = 4578
    assert rewrite_tps("BUY", "4543", "4541", "4536", 1.5, 3.0, 5.0) == (
        "4553.5", "4564", "4578")


def test_sell_ladder_off_entry_edge_and_sl():
    # SELL: entry_edge = min(range) = 4600, SL 4607 -> risk 7.
    # TP1 = 4600 - 1.5*7 = 4589.5 ; TP2 = 4579 ; TP3 = 4565
    assert rewrite_tps("SELL", "4600", "4602", "4607", 1.5, 3.0, 5.0) == (
        "4589.5", "4579", "4565")


def test_typo_line_over_max_risk_left_as_posted():
    # SL 47802 (extra digit) -> risk ~43007 > max_risk -> None (keep as posted).
    assert rewrite_tps("SELL", "4795", "4797", "47802", 1.5, 3.0, 5.0) is None
    # a ~100-pt wrong-hundreds stop is also above the 30-pt default floor.
    assert rewrite_tps("BUY", "2309", "2307", "2205", 1.5, 3.0, 5.0) is None


def test_zero_or_negative_risk_left_as_posted():
    assert rewrite_tps("BUY", "4543", "4541", "4543", 1.5, 3.0, 5.0) is None


def test_fmt_price_trims_like_the_feed():
    assert fmt_price(4578.0) == "4578"
    assert fmt_price(4553.5) == "4553.5"
    assert fmt_price(4553.50) == "4553.5"
    assert fmt_price(4553.25) == "4553.25"


# --------------------------------------------------------------------------- #
# live filter <-> backtest generator parity (the V073A contract)
# --------------------------------------------------------------------------- #
RAW = (
    "2026-06-20 GMT+7\n"
    "1. BUY XAUUSD 4543 - 4541 SL 4536 TP1 4551 TP2 4561 TP3 4576 2:02 PM\n"
    "2. SELL XAUUSD 4600 - 4602 SL 4607 TP1 4595 TP2 4585 TP3 4570 3:10 PM\n"
    "3. BUY XAUUSD 4795 - 4797 SL 47802 TP1 4787 TP2 4777 TP3 4762 4:00 PM\n"
)


def _levels_from_lines(text: str) -> dict[int, tuple[str, str, str]]:
    import re
    out: dict[int, tuple[str, str, str]] = {}
    for ln in text.splitlines():
        m = re.match(r"\s*(\d+)\.\s+(?:BUY|SELL).*TP1\s+([\d.]+)\s+TP2\s+([\d.]+)\s+TP3\s+([\d.]+)", ln)
        if m:
            out[int(m.group(1))] = (m.group(2), m.group(3), m.group(4))
    return out


def test_live_filter_rewrite_matches_generator(tmp_path):
    raw = tmp_path / "raw.txt"
    raw.write_text(RAW, encoding="utf-8")

    # live filter WITH rewrite (preset all so the hour filter drops nothing)
    live_out = tmp_path / "live.txt"
    lf.run_once(raw, live_out, "all", rewrite=(1.5, 3.0, 5.0, 30.0))

    # backtest generator on the same raw
    gen_out = tmp_path / "gen.txt"
    subprocess.run(
        [sys.executable, "tools/generate_victor_rr_feed.py", "--input", str(raw),
         "--rr1", "1.5", "--rr2", "3.0", "--rr3", "5.0", "--output", str(gen_out)],
        cwd=ROOT, check=True, capture_output=True,
    )

    live_levels = _levels_from_lines(live_out.read_text(encoding="utf-8"))
    gen_levels = _levels_from_lines(gen_out.read_text(encoding="utf-8"))
    assert live_levels and live_levels == gen_levels
    # signal 1 (BUY, risk 7) rewritten; signal 3 (typo) kept as posted
    assert live_levels[1] == ("4553.5", "4564", "4578")
    assert live_levels[3] == ("4787", "4777", "4762")


def test_live_filter_default_off_is_byte_identical(tmp_path):
    raw = tmp_path / "raw.txt"
    raw.write_text(RAW, encoding="utf-8")
    off = tmp_path / "off.txt"
    on_none = tmp_path / "none.txt"
    lf.run_once(raw, off, "all")                 # no rewrite kwarg
    lf.run_once(raw, on_none, "all", rewrite=None)
    baseline = off.read_text(encoding="utf-8")
    assert baseline == on_none.read_text(encoding="utf-8")
    # TPs are the provider's posted values, untouched
    assert _levels_from_lines(baseline)[1] == ("4551", "4561", "4576")


def test_rewrite_line_and_core_agree():
    line = "1. BUY XAUUSD 4543 - 4541 SL 4536 TP1 4551 TP2 4561 TP3 4576 2:02 PM\n"
    out, changed = rewrite_line(line, 1.5, 3.0, 5.0)
    assert changed and "TP1 4553.5 TP2 4564 TP3 4578" in out


def test_filter_rejects_non_monotone_ladder(tmp_path):
    raw = tmp_path / "raw.txt"
    raw.write_text(RAW, encoding="utf-8")
    out = tmp_path / "o.txt"
    with pytest.raises(SystemExit):
        lf.main(["--input", str(raw), "--output", str(out),
                 "--preset", "all", "--rewrite-rr1", "3.0",
                 "--rewrite-rr2", "2.0", "--rewrite-rr3", "5.0"])
