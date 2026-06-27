"""Guardrail: every committed tick part uses the agreed DAY-WINDOW format.

The on-disk archive (data/ticks/) must be packaged as
``XAUUSD_TICK_YYYYMM_D<startday>_p<sub>_ELEV8.csv`` -- the day-window split that
tools/split_ticks_by_days.py, ``export_ticks --split-days`` and
``backtest_hybrid --sync-ticks`` all produce. This test fails if any legacy
size-split part (``_pN`` with no ``_D``), half-month part (``_H1``/``_H2``), or
full unsplit month file is committed, so a regression in ANY generation path --
or a hand-dropped legacy file -- is caught in CI instead of silently
re-introducing the mixed archive that motivated this guardrail (June 2026 had
been committed as legacy ``_pN`` while May was already ``_D<start>_pN``).
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TICKS = ROOT / "data" / "ticks"

# Any committed tick CSV for a month: <SYM>_TICK_YYYYMM<...>.csv
_TICK_FILE_RE = re.compile(r"_TICK_\d{6}.*\.csv$")
# The agreed day-window part: <SYM>_TICK_YYYYMM_D<start>_p<sub>_<SOURCE>.csv
_DAY_PART_RE = re.compile(r"_TICK_\d{6}_D\d+_p\d+_[A-Za-z0-9]+\.csv$")


def test_committed_tick_archive_is_day_window_format():
    if not TICKS.is_dir():
        return  # no tick archive in this checkout -> nothing to police
    tick_files = sorted(p.name for p in TICKS.glob("*.csv")
                        if _TICK_FILE_RE.search(p.name))
    offenders = [n for n in tick_files if not _DAY_PART_RE.search(n)]
    assert not offenders, (
        "Non-day-window tick files committed under data/ticks/. Re-split them with "
        "`python tools/split_ticks_by_days.py --input data/ticks/<SYM>_TICK_<YYYYMM>_"
        "ELEV8.csv --days 3 --max-mb 95 --remove-source` (or regenerate via "
        "`export_ticks --split-days 3` / `backtest_hybrid --sync-ticks`, which now "
        "emit day windows). Offenders:\n  " + "\n  ".join(offenders)
    )
