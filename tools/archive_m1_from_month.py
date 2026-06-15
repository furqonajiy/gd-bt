"""Download MT5 M1 chart data month-by-month from a start month.

Example:

    python tools/archive_m1_from_month.py \
      --symbol XAUUSD \
      --start-month 2024-01 \
      --output-dir data \
      --server-offset 3

This writes files like:

    data/XAUUSD_M1_202401.csv
    data/XAUUSD_M1_202402.csv
    ...

The output format matches MT5's tab-separated chart export format used by the
backtester:

    <DATE> <TIME> <OPEN> <HIGH> <LOW> <CLOSE> <TICKVOL> <VOL> <SPREAD>
"""
from __future__ import annotations

import argparse
from datetime import UTC, datetime, timedelta
from pathlib import Path
import sys

# Allow running as `python tools/archive_m1_from_month.py` from repo root.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from xauusd_trading import Mt5Connection, archive_m1_by_month, render_archive_summary


def _parse_month(raw: str) -> tuple[int, int]:
    try:
        dt = datetime.strptime(raw, "%Y-%m")
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"month must be YYYY-MM, got {raw!r}"
        ) from exc
    return dt.year, dt.month


def _utc_epoch_to_naive(epoch: int) -> datetime:
    """Decode MT5 epoch as a naive UTC datetime without utcfromtimestamp()."""
    return datetime.fromtimestamp(int(epoch), UTC).replace(tzinfo=None)


def _first_of_next_month(year: int, month: int) -> datetime:
    if month == 12:
        return datetime(year + 1, 1, 1)
    return datetime(year, month + 1, 1)


def _month_count(start: tuple[int, int], end: tuple[int, int]) -> int:
    sy, sm = start
    ey, em = end
    count = (ey - sy) * 12 + (em - sm) + 1
    if count <= 0:
        raise SystemExit("--end-month must be the same as or after --start-month")
    return count


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="archive_m1_from_month",
        description="Download MT5 M1 data into one CSV per month from a chosen start month.",
    )
    p.add_argument("--symbol", default="XAUUSD", help="Exact MT5 symbol, e.g. XAUUSD, XAUUSD.r, GOLD.")
    p.add_argument("--start-month", required=True, type=_parse_month, help="First month to fetch, e.g. 2024-01.")
    p.add_argument("--end-month", type=_parse_month, default=None, help="Last month to fetch, e.g. 2026-05. Default: current chart month.")
    p.add_argument("--output-dir", default="data")
    p.add_argument("--server-offset", type=int, default=3, help="Broker server timezone offset. Use 3 for GMT+3.")
    p.add_argument("--overwrite", action="store_true", help="Overwrite existing monthly CSVs instead of merging/deduping.")

    p.add_argument("--mt5-path", default=None)
    p.add_argument("--mt5-login", type=int, default=None)
    p.add_argument("--mt5-password", default=None)
    p.add_argument("--mt5-server", default=None)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    with Mt5Connection(
        path=args.mt5_path,
        login=args.mt5_login,
        password=args.mt5_password,
        server=args.mt5_server,
    ) as conn:
        mt5 = conn.mt5
        if args.end_month is None:
            rates = mt5.copy_rates_from_pos(args.symbol, mt5.TIMEFRAME_M1, 0, 1)
            if rates is None or len(rates) == 0:
                raise SystemExit(f"No latest M1 bar returned for {args.symbol!r}; check symbol and MT5 connection.")
            broker_time = _utc_epoch_to_naive(int(rates[0]["time"]))
            end_chart_time = broker_time + timedelta(hours=3 - args.server_offset)
            end_month = (end_chart_time.year, end_chart_time.month)
        else:
            end_month = args.end_month
            end_chart_time = _first_of_next_month(*end_month) - timedelta(minutes=1)

        months_back = _month_count(args.start_month, end_month)
        summary = archive_m1_by_month(
            conn,
            args.symbol,
            args.output_dir,
            months_back=months_back,
            until_chart_time=end_chart_time,
            server_offset_hours=args.server_offset,
            overwrite=args.overwrite,
        )

    print(render_archive_summary(summary))
    if not summary:
        print(
            "\nMT5 returned no bars. Your broker may not have M1 history that far back. "
            "Open the symbol on M1 and scroll back, increase Max bars in chart, or use another historical data source."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
