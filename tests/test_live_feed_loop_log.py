"""live_feed_loop signal-diff logging: header-then-events, like auto.

Pure functions only -- no MT5, no generator run.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for p in (str(ROOT), str(ROOT / "tools")):
    if p not in sys.path:
        sys.path.insert(0, p)

from live_feed_loop import _new_signals, _output_path, _signal_lines  # noqa: E402

_FEED = """2026-06-12 GMT+3
73. SELL XAUUSD 4075.5 - 4077.5 SL 4079.5 TP1 4071.5 TP2 4069.5 TP3 4067.5 8:15 PM
74. BUY XAUUSD 4154.5 - 4152.5 SL 4145 TP1 4164 TP2 4168.5 TP3 4173.5 8:57 PM
"""


def test_signal_lines_skips_headers_and_blanks():
    lines = _signal_lines(_FEED)
    assert len(lines) == 2
    assert lines[0].startswith("73. SELL XAUUSD")
    assert "GMT+3" not in "".join(lines)


def test_new_signals_returns_only_unseen_in_order():
    seen = {"73. SELL XAUUSD 4075.5 - 4077.5 SL 4079.5 TP1 4071.5 TP2 4069.5 TP3 4067.5 8:15 PM"}
    new = _new_signals(_FEED, seen)
    assert new == ["74. BUY XAUUSD 4154.5 - 4152.5 SL 4145 TP1 4164 TP2 4168.5 TP3 4173.5 8:57 PM"]


def test_new_signals_empty_when_all_seen():
    seen = set(_signal_lines(_FEED))
    assert _new_signals(_FEED, seen) == []


def test_new_signal_appended_next_cycle_is_detected():
    seen = set(_signal_lines(_FEED))
    grown = _FEED + "75. BUY XAUUSD 4202.5 - 4200.5 SL 4190.5 TP1 4214.5 TP2 4220.5 TP3 4226.5 9:04 PM\n"
    new = _new_signals(grown, seen)
    assert new == ["75. BUY XAUUSD 4202.5 - 4200.5 SL 4190.5 TP1 4214.5 TP2 4220.5 TP3 4226.5 9:04 PM"]


def test_output_path_parsing():
    assert _output_path(["--charts", "x.csv", "--output", "signals/f.txt",
                         "--start", "2026-06-10"]) == "signals/f.txt"
    assert _output_path(["--charts", "x.csv"]) is None


def test_effective_gen_argv_rolls_start_and_narrows_charts(tmp_path, monkeypatch):
    from datetime import datetime
    from live_feed_loop import _effective_gen_argv, _recent_month_charts
    # make 3 month files; only 2 most recent should be selected, oldest-first
    for ym in ("202604", "202605", "202606"):
        (tmp_path / f"XAUUSD_M1_{ym}_ELEV8.csv").write_text("x")
    tmpl = str(tmp_path / "XAUUSD_M1_*_ELEV8.csv")
    today = datetime(2026, 6, 12)
    files = _recent_month_charts(tmpl, 2, today)
    assert files == [str(tmp_path / "XAUUSD_M1_202605_ELEV8.csv"),
                     str(tmp_path / "XAUUSD_M1_202606_ELEV8.csv")]

    argv = ["--charts", tmpl, "--output", "f.txt", "--start", "2025-01-01",
            "--session-start", "0"]
    eff = _effective_gen_argv(argv, start_days=3, recent_months=2, today=today)
    assert "--start" in eff and eff[eff.index("--start") + 1] == "2026-06-09"
    ci = eff.index("--charts")
    assert eff[ci + 1:ci + 3] == files          # glob replaced by the 2 files
    assert eff[eff.index("--output") + 1] == "f.txt"  # rest intact


def test_effective_gen_argv_noop_when_flags_absent():
    from datetime import datetime
    from live_feed_loop import _effective_gen_argv
    argv = ["--charts", "data/*.csv", "--output", "f.txt", "--start", "2025-01-01"]
    assert _effective_gen_argv(argv, start_days=None, recent_months=None,
                               today=datetime(2026, 6, 12)) == argv
