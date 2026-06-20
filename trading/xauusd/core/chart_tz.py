"""DST-aware chart timezone (Eastern European Time).

The MT5 server clock that stamps the CSV chart bars is **Eastern European Time**:
EET (UTC+2) in winter and EEST (UTC+3) in summer, switching on the **EU rule** --
the last Sunday of March at 01:00 UTC (+2 -> +3) and the last Sunday of October
at 01:00 UTC (+3 -> +2). This was confirmed empirically from the ELEV8 archive:
the weekly close sits at 23:59 server-local most of the year but shifts to 22:59
ONLY during the windows where US and EU daylight saving disagree (mid/late March
and late October / early November), every year 2021-2026 -- the unmistakable
signature of an EU-rule +2/+3 clock.

So "chart time" is NOT a fixed GMT+3. ``CHART_TIMEZONE_OFFSET`` (==3) remains the
*summer* reference, but every conversion between chart-local time and an absolute
clock (a provider signal's source tz, MT5 epoch, "now") must use the offset that
applies at that instant. This module is the single source of truth for that.

Transitions happen at 02:00-04:00 local on a Sunday, when XAUUSD is closed, so no
trading bar falls in the skipped/ambiguous hour and a threshold rule is exact for
every real bar.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

EEST_OFFSET = 3   # summer (UTC+3)
EET_OFFSET = 2    # winter (UTC+2)


def _last_sunday(year: int, month: int) -> int:
    """Day-of-month of the last Sunday of ``month`` (March/October => 31 days)."""
    d = date(year, month, 31)
    return 31 - ((d.weekday() + 1) % 7)  # weekday: Mon=0..Sun=6


def offset_at_utc(utc: datetime) -> int:
    """Chart offset (hours east of UTC) in effect at a given UTC instant."""
    y = utc.year
    start = datetime(y, 3, _last_sunday(y, 3), 1, 0)    # 01:00 UTC, last Sun March
    end = datetime(y, 10, _last_sunday(y, 10), 1, 0)     # 01:00 UTC, last Sun October
    return EEST_OFFSET if start <= utc < end else EET_OFFSET


def offset_at_chart_local(local: datetime) -> int:
    """Chart offset for a naive chart-LOCAL (EET/EEST) datetime.

    Spring forward skips local 03:00->04:00; autumn fall-back repeats 03:00->04:00.
    Markets are closed then, so the threshold (>=04:00 on the fall date is already
    winter; >=03:00 on the spring date is already summer) is exact for trading
    bars and never wrong by more than that closed hour at the boundary itself.
    """
    y = local.year
    spring = datetime(y, 3, _last_sunday(y, 3), 3, 0)    # 03:00 local -> jumps to 04:00 (EEST)
    autumn = datetime(y, 10, _last_sunday(y, 10), 4, 0)  # 04:00 local -> falls to 03:00 (EET)
    return EEST_OFFSET if spring <= local < autumn else EET_OFFSET


def utc_to_chart(utc: datetime) -> datetime:
    """Naive UTC -> naive chart-local (EET/EEST)."""
    return utc + timedelta(hours=offset_at_utc(utc))


def chart_to_utc(local: datetime) -> datetime:
    """Naive chart-local (EET/EEST) -> naive UTC."""
    return local - timedelta(hours=offset_at_chart_local(local))


def to_chart_tz(dt: datetime, source_offset: int) -> datetime:
    """Convert a naive clock in ``source_offset`` (e.g. a GMT+7 signal time) to
    naive chart-local time, honouring EET/EEST at that instant."""
    return utc_to_chart(dt - timedelta(hours=source_offset))


def from_chart_tz(local: datetime, target_offset: int) -> datetime:
    """Convert a naive chart-local time to a naive clock in ``target_offset``
    (e.g. render a chart bar in GMT+7 for display), honouring EET/EEST."""
    return chart_to_utc(local) + timedelta(hours=target_offset)


# Provider and self signals are authored in GMT+7, so live logs render chart-time
# instants in that zone -- the operator reads the log against the signals (and a
# GMT+7 wall clock), not the GMT+3 chart/server clock. DST-aware via from_chart_tz.
LOG_DISPLAY_OFFSET = 7


def to_log_tz(local: datetime) -> datetime:
    """Chart-local datetime -> the GMT+7 display zone used in live logs."""
    return from_chart_tz(local, LOG_DISPLAY_OFFSET)
