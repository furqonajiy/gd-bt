"""MT5 adapter — live M1 bars and account equity from MetaTrader 5.

Drop-in replacement for `CsvChartSource` when running against a live MT5
terminal. Windows-only at runtime; the module imports cleanly elsewhere
because `MetaTrader5` is lazy-imported inside Mt5Connection.

Broker timezone: MT5 returns bar timestamps in server local time. Most
XAUUSD brokers run on GMT+3 (default `server_offset_hours=3`, no shift
needed). Set explicitly if your broker is different.

Symbol name varies by broker ("XAUUSD", "XAUUSD.r", "GOLD", "XAUUSDm",
...). Check Market Watch and pass the exact name via `symbol=`.
"""
from __future__ import annotations
import calendar
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional

from xauusd_trading import ChartSource
from xauusd_trading import Bar
from xauusd_trading import POINT_VALUE

# MT5 Python interprets naive datetimes as the host's LOCAL time, and even
# tz-aware datetimes can be re-routed through local time on some versions
# before being epoch-encoded. To eliminate ambiguity, we pass UTC-epoch ints
# (calendar.timegm) — ints have no timezone interpretation.
_UTC = timezone.utc


def _broker_naive_to_mt5_epoch(broker_naive: datetime) -> int:
    """Encode a broker-naive datetime as the UTC-epoch int that MT5 uses
    internally for bar.time. `calendar.timegm` treats input as UTC, so
    the result is independent of the caller's local timezone.
    """
    return calendar.timegm(broker_naive.timetuple())


def _chart_time_to_mt5_epoch(chart_naive: datetime, server_offset_hours: int) -> int:
    """Convert a chart-time (GMT+3) naive datetime to the MT5 epoch."""
    shift = timedelta(hours=3 - server_offset_hours)
    broker_naive = chart_naive - shift
    return _broker_naive_to_mt5_epoch(broker_naive)


def _import_mt5():
    """Lazy import so this module loads on non-Windows machines."""
    try:
        import MetaTrader5 as mt5
    except ImportError as e:
        raise RuntimeError(
            "MetaTrader5 package not installed. Run: pip install MetaTrader5\n"
            "(Windows-only; MT5 terminal must be installed and running.)"
        ) from e
    return mt5


# ---------------------------------------------------------------------------
# connection
# ---------------------------------------------------------------------------

class Mt5Connection:
    """Manages the MT5 terminal connection.

    Use as a context manager:

        with Mt5Connection() as conn:
            chart = Mt5ChartSource(conn, symbol="XAUUSD")
            ...
    """

    def __init__(
            self, path: Optional[str] = None,
            login: Optional[int] = None, password: Optional[str] = None,
            server: Optional[str] = None, timeout: int = 60_000,
    ):
        self._mt5 = _import_mt5()
        self._kwargs = {"timeout": timeout}
        if path: self._kwargs["path"] = path
        if login: self._kwargs["login"] = login
        if password: self._kwargs["password"] = password
        if server: self._kwargs["server"] = server
        self._initialized = False

    @property
    def mt5(self):
        return self._mt5

    def initialize(self) -> None:
        if self._initialized:
            return
        if not self._mt5.initialize(**self._kwargs):
            raise RuntimeError(
                f"MT5 initialize() failed: {self._mt5.last_error()}"
            )
        self._initialized = True

    def shutdown(self) -> None:
        if self._initialized:
            self._mt5.shutdown()
            self._initialized = False

    def __enter__(self):
        self.initialize()
        return self

    def __exit__(self, *exc):
        self.shutdown()


# ---------------------------------------------------------------------------
# chart source
# ---------------------------------------------------------------------------

class Mt5ChartSource(ChartSource):
    """ChartSource backed by MT5 1-minute bars."""

    def __init__(
            self, connection: Mt5Connection, symbol: str = "XAUUSD",
            server_offset_hours: int = 3, history_bars: int = 5_000,
    ):
        """
        connection : an initialized Mt5Connection.
        symbol : exact symbol string from MT5 Market Watch.
        server_offset_hours : broker server timezone offset from UTC.
            Most XAUUSD brokers use 3. Common alternatives: 2, 0.
        history_bars : bounds the backfill window for replaying prior
            open positions. The engine re-queries the latest bar on
            every call regardless.
        """
        self._mt5 = connection.mt5
        self._symbol = symbol
        self._server_offset = server_offset_hours
        self._history_bars = history_bars
        self._shift = timedelta(hours=3 - server_offset_hours)
        if not self._mt5.symbol_select(symbol, True):
            raise RuntimeError(
                f"Symbol {symbol!r} not found in Market Watch. "
                f"Check the exact name (e.g. XAUUSD, XAUUSD.r, GOLD)."
            )

    def _to_chart_time(self, mt5_epoch: int) -> datetime:
        # MT5 returns server-local-time-as-if-it-were-UTC.
        broker_naive = datetime.fromtimestamp(int(mt5_epoch), timezone.utc).replace(tzinfo=None)
        return broker_naive + self._shift

    def _to_server_time(self, chart_time: datetime) -> datetime:
        return chart_time - self._shift

    def _rate_to_bar(self, r) -> Bar:
        spread = int(r["spread"])
        return Bar(
            time=self._to_chart_time(r["time"]),
            open=float(r["open"]), high=float(r["high"]),
            low=float(r["low"]), close=float(r["close"]),
            spread_points=spread, spread_price=spread * POINT_VALUE,
        )

    def first_time(self) -> Optional[datetime]:
        rates = self._mt5.copy_rates_from_pos(
            self._symbol, self._mt5.TIMEFRAME_M1, self._history_bars - 1, 1,
                                                  )
        if rates is None or len(rates) == 0:
            return None
        return self._to_chart_time(rates[0]["time"])

    def last_time(self) -> Optional[datetime]:
        rates = self._mt5.copy_rates_from_pos(
            self._symbol, self._mt5.TIMEFRAME_M1, 0, 1,
        )
        if rates is None or len(rates) == 0:
            return None
        return self._to_chart_time(rates[0]["time"])

    def latest(self, at_or_before: Optional[datetime] = None) -> Optional[Bar]:
        if at_or_before is None:
            rates = self._mt5.copy_rates_from_pos(
                self._symbol, self._mt5.TIMEFRAME_M1, 0, 1,
            )
        else:
            end_epoch = _chart_time_to_mt5_epoch(at_or_before, self._server_offset)
            start_epoch = end_epoch - 5 * 60  # 5-minute buffer
            rates = self._mt5.copy_rates_range(
                self._symbol, self._mt5.TIMEFRAME_M1, start_epoch, end_epoch,
            )
        if rates is None or len(rates) == 0:
            return None
        return self._rate_to_bar(rates[-1])

    def bars_between(self, start: datetime, end: datetime) -> Iterable[Bar]:
        start_epoch = _chart_time_to_mt5_epoch(start, self._server_offset)
        end_epoch = _chart_time_to_mt5_epoch(end, self._server_offset)
        rates = self._mt5.copy_rates_range(
            self._symbol, self._mt5.TIMEFRAME_M1, start_epoch, end_epoch,
        )
        if rates is None:
            return iter([])
        return (self._rate_to_bar(r) for r in rates)

    def recent_closed_bars(self, count: int) -> list[Bar]:
        """The last `count` CLOSED M1 bars, oldest first.

        copy_rates_from_pos(...,0,N) returns the still-forming current bar at
        the newest index; it is dropped here so the rejection detector only
        ever sees completed candles. This is what keeps live generation in
        parity with the M1 backtest (signal_time = rejected_bar + 1 min, and
        the rejected bar must be closed before it can produce a signal).
        """
        n = max(1, int(count))
        rates = self._mt5.copy_rates_from_pos(
            self._symbol, self._mt5.TIMEFRAME_M1, 0, n + 1,
                                                     )
        if rates is None or len(rates) <= 1:
            return []
        return [self._rate_to_bar(r) for r in rates[:-1]]


# ---------------------------------------------------------------------------
# account equity
# ---------------------------------------------------------------------------

def mt5_equity(connection: Mt5Connection) -> float:
    """Current account equity from MT5."""
    info = connection.mt5.account_info()
    if info is None:
        raise RuntimeError(f"account_info() failed: {connection.mt5.last_error()}")
    return float(info.equity)


# ---------------------------------------------------------------------------
# diagnostic: open positions / pending orders
# ---------------------------------------------------------------------------

def mt5_open_positions_summary(connection: Mt5Connection, symbol: str = "XAUUSD") -> list[dict]:
    """List currently-open MT5 positions and pending orders for the symbol.

    Read-only diagnostic. The engine's Position model needs full signal
    context (TP1/2/3, side, time) that MT5 alone does not store; for the
    engine, `positions.json` is the source of truth. This helper just
    lets you sanity-check that MT5 reflects what the JSON says.
    """
    mt5 = connection.mt5
    out = []
    for p in mt5.positions_get(symbol=symbol) or []:
        out.append({
            "kind": "position", "ticket": p.ticket, "symbol": p.symbol,
            "type": "BUY" if p.type == mt5.POSITION_TYPE_BUY else "SELL",
            "volume": p.volume, "price_open": p.price_open,
            "sl": p.sl, "tp": p.tp, "profit": p.profit,
            "comment": p.comment, "magic": p.magic,
            "time": datetime.fromtimestamp(p.time, timezone.utc).replace(tzinfo=None),
        })
    for o in mt5.orders_get(symbol=symbol) or []:
        out.append({
            "kind": "order", "ticket": o.ticket, "symbol": o.symbol,
            "type": _order_type_name(mt5, o.type),
            "volume": o.volume_initial, "price_open": o.price_open,
            "sl": o.sl, "tp": o.tp, "comment": o.comment, "magic": o.magic,
            "time_setup": datetime.fromtimestamp(o.time_setup, timezone.utc).replace(tzinfo=None),
        })
    return out


def _order_type_name(mt5, type_id: int) -> str:
    names = {
        mt5.ORDER_TYPE_BUY: "BUY", mt5.ORDER_TYPE_SELL: "SELL",
        mt5.ORDER_TYPE_BUY_LIMIT: "BUY_LIMIT",
        mt5.ORDER_TYPE_SELL_LIMIT: "SELL_LIMIT",
        mt5.ORDER_TYPE_BUY_STOP: "BUY_STOP",
        mt5.ORDER_TYPE_SELL_STOP: "SELL_STOP",
    }
    return names.get(type_id, f"TYPE_{type_id}")


# ---------------------------------------------------------------------------
# M1 archive — per-month CSV files in MT5 export format
# ---------------------------------------------------------------------------

# Header matches MT5's "Save As CSV", so archived files are interchangeable
# with manual exports and slot directly into `--charts` for backtests.
_MT5_EXPORT_COLUMNS = [
    "<DATE>", "<TIME>", "<OPEN>", "<HIGH>", "<LOW>",
    "<CLOSE>", "<TICKVOL>", "<VOL>", "<SPREAD>",
]


def _rates_to_export_df(rates, server_offset_hours: int):
    """Convert an MT5 numpy rates array to an MT5-export-format DataFrame
    with times shifted into chart time (GMT+3).
    """
    import pandas as pd
    if rates is None or len(rates) == 0:
        return pd.DataFrame(columns=_MT5_EXPORT_COLUMNS)
    df = pd.DataFrame(rates)
    shift_hours = 3 - server_offset_hours
    times = pd.to_datetime(df["time"], unit="s") + pd.Timedelta(hours=shift_hours)
    return pd.DataFrame({
        "<DATE>":    times.dt.strftime("%Y.%m.%d"),
        "<TIME>":    times.dt.strftime("%H:%M:%S"),
        "<OPEN>":    df["open"],
        "<HIGH>":    df["high"],
        "<LOW>":     df["low"],
        "<CLOSE>":   df["close"],
        "<TICKVOL>": df["tick_volume"],
        "<VOL>":     df["real_volume"],
        "<SPREAD>":  df["spread"].astype(int),
    })


def _merge_with_existing(new_df, existing_path):
    """Union new and existing bars, dedup by (DATE, TIME) keeping new
    on collision, sort, return a fresh DataFrame.
    """
    import pandas as pd
    if not existing_path.exists():
        return new_df
    try:
        existing = pd.read_csv(existing_path, sep="\t")
    except Exception:
        return new_df
    if not set(_MT5_EXPORT_COLUMNS).issubset(existing.columns):
        return new_df
    combined = pd.concat([existing[_MT5_EXPORT_COLUMNS], new_df], ignore_index=True)
    combined = combined.drop_duplicates(subset=["<DATE>", "<TIME>"], keep="last")
    combined = combined.sort_values(["<DATE>", "<TIME>"]).reset_index(drop=True)
    return combined


def _month_iter(until: datetime, months_back: int):
    """(year, month) pairs for the last `months_back` months ending at
    `until`'s year-month, oldest first.
    """
    y, m = until.year, until.month
    pairs = []
    for _ in range(months_back):
        pairs.append((y, m))
        m -= 1
        if m == 0:
            m = 12
            y -= 1
    return list(reversed(pairs))


def _first_of_next_month(year: int, month: int) -> datetime:
    if month == 12:
        return datetime(year + 1, 1, 1)
    return datetime(year, month + 1, 1)


def _archive_filename(symbol: str, year: int, month: int, source: str = "ELEV8") -> str:
    """Canonical chart archive filename.

    Use the normalized project symbol in filenames even if the broker symbol
    has a suffix, so all historical files sort together:
        XAUUSD_M1_202605_ELEV8.csv
        XAUUSD_M1_202401_INTERNET.csv
    """
    return f"XAUUSD_M1_{year:04d}{month:02d}_{source.upper()}.csv"


def archive_m1_by_month(
        connection: Mt5Connection, symbol: str, output_dir,
        months_back: int = 4, until_chart_time: Optional[datetime] = None,
        server_offset_hours: int = 3, overwrite: bool = False,
) -> list[dict]:
    """Pull M1 bars from MT5 and save one CSV per year-month.

    For each year-month in the last `months_back` months (anchored to
    `until_chart_time`, default "now"), this queries MT5, converts to
    chart time and MT5-export-CSV format, and either merges with the
    existing file (default; safe for boundary months) or overwrites it.
    Output: `<output_dir>/XAUUSD_M1_YYYYMM_ELEV8.csv`.

    Returns a list of `{month, path, bars_written, bars_fetched, bars_existing}`.
    """
    from pathlib import Path
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    mt5 = connection.mt5

    if until_chart_time is None:
        rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_M1, 0, 1)
        if rates is None or len(rates) == 0:
            now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
            until_chart_time = now_utc + timedelta(hours=3)
        else:
            broker_naive = datetime.fromtimestamp(int(rates[0]["time"]), timezone.utc).replace(tzinfo=None)
            until_chart_time = broker_naive + timedelta(hours=3 - server_offset_hours)

    summary: list[dict] = []
    for year, month in _month_iter(until_chart_time, months_back):
        chart_start = datetime(year, month, 1)
        # Exclusive month end: last bar is HH:59 of the last day, one minute
        # before the next month begins. Keeps the boundary bar out of M's file.
        chart_end = _first_of_next_month(year, month) - timedelta(minutes=1)
        start_epoch = _chart_time_to_mt5_epoch(chart_start, server_offset_hours)
        end_epoch = _chart_time_to_mt5_epoch(chart_end, server_offset_hours)
        rates = mt5.copy_rates_range(symbol, mt5.TIMEFRAME_M1, start_epoch, end_epoch)
        new_df = _rates_to_export_df(rates, server_offset_hours)

        path = output_dir / _archive_filename(symbol, year, month, "ELEV8")
        existing_count = 0
        if path.exists():
            try:
                import pandas as pd
                existing_count = len(pd.read_csv(path, sep="\t"))
            except Exception:
                existing_count = 0

        if overwrite or not path.exists():
            final_df = new_df
        else:
            final_df = _merge_with_existing(new_df, path)

        if len(final_df) == 0 and not path.exists():
            continue

        final_df.to_csv(path, sep="\t", index=False)
        summary.append({
            "month": f"{year:04d}-{month:02d}",
            "path": str(path),
            "bars_written": int(len(final_df)),
            "bars_fetched": int(len(new_df)),
            "bars_existing": int(existing_count),
        })
    return summary


def render_archive_summary(summary: list[dict]) -> str:
    if not summary:
        return "Archive: nothing to write (MT5 returned no bars)."
    lines = ["Archive:"]
    for row in summary:
        delta = row["bars_written"] - row["bars_existing"]
        sign = f"+{delta}" if delta >= 0 else str(delta)
        lines.append(
            f"  {row['path']}   "
            f"{row['bars_written']:>6} bars  ({sign} vs prior)"
        )
    return "\n".join(lines)