"""Provider signal filtering rules shared by backtest tools and live listener.

The high-growth preset is intentionally aggressive. It was selected from the
uploaded provider signal sample by filtering signal side by chart-time hour, then
running the same backtest engine with risk sizing.

Keep this module dependency-light so telegram/telegram_listener.py can import it
without importing MT5 or pandas.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta


@dataclass(frozen=True)
class ProviderSignalFilterDecision:
    keep: bool
    preset: str
    reason: str
    chart_time: datetime


PRESETS = {
    "all",
    "no_bad_hours",
    "best_hours",
    "high_growth_hour_side",
    "research_month_hour_side",
}


HIGH_GROWTH_BUY_HOURS = {4, 9, 10, 11, 12, 13, 14, 17, 18, 20}
HIGH_GROWTH_SELL_HOURS = {7, 8, 9, 10, 11, 14, 15, 16, 17, 18, 19}
NO_BAD_HOURS_EXCLUDED = {5, 6, 8, 12, 13, 19, 21, 22}
BEST_HOURS = {9, 10, 11, 14, 15, 16, 17, 18, 20}
RESEARCH_EXCLUDED_MONTHS = {7, 11, 12}


def source_to_chart_time(source_time: datetime, source_tz_offset: int = 7, chart_tz_offset: int = 3) -> datetime:
    """Convert a naive source timestamp to naive chart timestamp."""
    return source_time + timedelta(hours=chart_tz_offset - source_tz_offset)


def decide_provider_signal_filter(
    *,
    side: str,
    source_time: datetime,
    preset: str = "high_growth_hour_side",
    source_tz_offset: int = 7,
    chart_tz_offset: int = 3,
) -> ProviderSignalFilterDecision:
    """Return whether a provider signal should be tradable under a preset.

    ``source_time`` should be the signal timestamp in the provider timezone
    (VICTOR currently posts in GMT+7). The rule evaluates chart-time hour so it
    matches backtest, where parsed signals are normalized to GMT+3.
    """
    preset = preset.strip().lower()
    if preset not in PRESETS:
        raise ValueError(f"Unknown provider filter preset {preset!r}; choose one of {sorted(PRESETS)}")

    side = side.upper()
    chart_time = source_to_chart_time(source_time, source_tz_offset, chart_tz_offset)
    hour = chart_time.hour
    month = chart_time.month

    if preset == "all":
        return ProviderSignalFilterDecision(True, preset, "preset=all", chart_time)

    if preset == "no_bad_hours":
        keep = hour not in NO_BAD_HOURS_EXCLUDED
        reason = f"chart_hour={hour:02d} {'allowed' if keep else 'excluded'} by no_bad_hours"
        return ProviderSignalFilterDecision(keep, preset, reason, chart_time)

    if preset == "best_hours":
        keep = hour in BEST_HOURS
        reason = f"chart_hour={hour:02d} {'allowed' if keep else 'not in best_hours'}"
        return ProviderSignalFilterDecision(keep, preset, reason, chart_time)

    if preset == "high_growth_hour_side":
        if side == "BUY":
            keep = hour in HIGH_GROWTH_BUY_HOURS
            reason = f"BUY chart_hour={hour:02d} {'allowed' if keep else 'not in high-growth BUY hours'}"
        elif side == "SELL":
            keep = hour in HIGH_GROWTH_SELL_HOURS
            reason = f"SELL chart_hour={hour:02d} {'allowed' if keep else 'not in high-growth SELL hours'}"
        else:
            keep = False
            reason = f"unknown side={side!r}"
        return ProviderSignalFilterDecision(keep, preset, reason, chart_time)

    if preset == "research_month_hour_side":
        base = decide_provider_signal_filter(
            side=side,
            source_time=source_time,
            preset="high_growth_hour_side",
            source_tz_offset=source_tz_offset,
            chart_tz_offset=chart_tz_offset,
        )
        keep = base.keep and month not in RESEARCH_EXCLUDED_MONTHS
        reason = base.reason
        if month in RESEARCH_EXCLUDED_MONTHS:
            reason += f"; month={month:02d} excluded by research preset"
        return ProviderSignalFilterDecision(keep, preset, reason, chart_time)

    raise AssertionError(f"Unhandled preset {preset!r}")
