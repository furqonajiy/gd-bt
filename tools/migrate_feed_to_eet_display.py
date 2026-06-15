#!/usr/bin/env python3
"""Migrate a SELF-generated feed from the old fixed-+3 display to EET/EEST display.

Old self-feeds were written assuming a fixed GMT+3 chart: the per-line clock was
``chart_local + (N - 3)`` under a ``GMT+N`` header. The engine is now DST-aware
(``core/chart_tz``: chart is EET/EEST, +2 winter / +3 summer), so those old feeds
mis-parse winter signals by 1h. This rewrites each feed so the display is
DST-correct WITHOUT moving the underlying chart bar:

  chart_local = T_disp + (3 - N)            # recover the bar from the old convention
  new_disp    = chart_tz.from_chart_tz(chart_local, N)   # DST-aware re-display

Summer signals are unchanged; winter signals shift +1h in display. Because the
chart bar is preserved, any backtest/sweep result is identical. Group by the new
display date so a cross-midnight shift lands in the right block; renumber per day.

ONLY run this on SELF-generated feeds (adaptive_*, self_*, better_*). Do NOT run
it on a real provider feed (victor_signals.txt): those times are genuine GMT+7
wall-clock and are already correct under the DST-aware parser.

    python tools/migrate_feed_to_eet_display.py generated/adaptive_adA_base.txt [...]
"""
from __future__ import annotations

import argparse
import re
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from xauusd_trading.core import chart_tz  # noqa: E402

HEADER_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\s+GMT\s*([+-]\s*\d+)\s*$")
SIGNAL_RE = re.compile(
    r"^\s*\d+\.\s*(?P<body>.*?)\s+(?P<time>\d{1,2}:\d{2})\s*(?P<mer>AM|PM)\s*$",
    re.IGNORECASE,
)


def _fmt_time(dt: datetime) -> str:
    return dt.strftime("%I:%M %p").lstrip("0")


def migrate(text: str) -> str:
    cur_date: str | None = None
    cur_off: int | None = None
    out_by_off: dict[int, list[tuple[datetime, str]]] = defaultdict(list)

    for raw in text.splitlines():
        line = raw.rstrip("\n")
        h = HEADER_RE.match(line)
        if h:
            cur_date, cur_off = h.group(1), int(h.group(2).replace(" ", ""))
            continue
        if not line.strip():
            continue
        m = SIGNAL_RE.match(line)
        if m is None or cur_date is None or cur_off is None:
            continue
        t_disp = datetime.strptime(
            f"{cur_date} {m.group('time')} {m.group('mer').upper()}", "%Y-%m-%d %I:%M %p")
        chart_local = t_disp + timedelta(hours=3 - cur_off)        # recover the bar
        new_disp = chart_tz.from_chart_tz(chart_local, cur_off)    # DST-aware re-display
        out_by_off[cur_off].append((new_disp, m.group("body").strip()))

    blocks: list[str] = []
    for off in sorted(out_by_off):
        by_day: dict[str, list[tuple[datetime, str]]] = defaultdict(list)
        for dt, body in out_by_off[off]:
            by_day[dt.strftime("%Y-%m-%d")].append((dt, body))
        for day in sorted(by_day):
            if blocks:
                blocks.append("")
            blocks.append(f"{day} GMT+{off}" if off >= 0 else f"{day} GMT{off}")
            for i, (dt, body) in enumerate(sorted(by_day[day], key=lambda db: db[0]), 1):
                blocks.append(f"{i}. {body} {_fmt_time(dt)}")
    return "\n".join(blocks) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("feeds", nargs="+")
    args = ap.parse_args(argv)
    for f in args.feeds:
        p = Path(f)
        after = migrate(p.read_text(encoding="utf-8"))
        p.write_text(after, encoding="utf-8")
        n = sum(1 for ln in after.splitlines() if re.match(r"^\d+\.\s", ln))
        print(f"migrated {p} ({n} signals)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
