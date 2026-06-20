"""Parity/safety tests for tools/auto_self.py and Mt5ChartSource.recent_closed_bars.

These exercise the pure feed-construction logic, guard withholding, parser
roundtrips, archive append behavior, and forming-bar exclusion. No MT5 required.
"""
from __future__ import annotations

import sys
import types
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"
for _p in (str(ROOT), str(TOOLS)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import auto_self as A  # noqa: E402
from trading.xauusd import parse_signals_file  # noqa: E402
from trading.xauusd.strategy.self_signals import GeneratedSignal  # noqa: E402


def _mk(side: str, when: datetime, anchor: float, width: float = 2.0, sld: float = 5.0):
    if side == "BUY":
        r1, r2 = anchor, anchor - width
        sl = r2 - sld
        tp1, tp2, tp3 = anchor + 10, anchor + 20, anchor + 40
    else:
        r1, r2 = anchor, anchor + width
        sl = r2 + sld
        tp1, tp2, tp3 = anchor - 10, anchor - 20, anchor - 40
    return GeneratedSignal(
        signal_time_chart=when,
        side=side,
        r1=round(r1, 2),
        r2=round(r2, 2),
        sl=round(sl, 2),
        tp1=round(tp1, 2),
        tp2=round(tp2, 2),
        tp3=round(tp3, 2),
        source_bar_time=when - timedelta(minutes=1),
        entry_anchor=anchor,
        recent_level=anchor,
        wick_size=1.5,
        body_size=0.5,
        bar_range=2.0,
        spread_points=20,
        reason="test",
    )


def _key_by_identity(keyed):
    return {(s.signal_time_chart, s.side): k for s, k in keyed}


def test_keyed_numbering_matches_per_day_order():
    sigs = [
        _mk("BUY", datetime(2026, 6, 3, 9, 0), 2000.0),
        _mk("SELL", datetime(2026, 6, 3, 14, 30), 1990.0),
        _mk("BUY", datetime(2026, 6, 4, 8, 5), 2010.0),
    ]
    keys = [k for _, k in A._keyed_signals(sigs)]
    assert keys == ["2026-06-03#01", "2026-06-03#02", "2026-06-04#01"]


def test_feed_roundtrips_through_real_parser(tmp_path):
    sigs = [
        _mk("BUY", datetime(2026, 6, 3, 9, 0), 2000.0),
        _mk("SELL", datetime(2026, 6, 3, 14, 30), 1990.0),
        _mk("BUY", datetime(2026, 6, 4, 8, 5), 2010.0),
    ]
    keyed = A._keyed_signals(sigs)
    allowed = {k for _, k in keyed}
    text = A._render_feed(keyed, allowed, source_tz_offset=3, price_digits=2)

    path = tmp_path / "feed.txt"
    path.write_text(text, encoding="utf-8")
    parsed = parse_signals_file(path)

    assert sorted(s.signal_key for s in parsed) == [
        "2026-06-03#01", "2026-06-03#02", "2026-06-04#01"
    ]
    buy0 = next(s for s in parsed if s.signal_key == "2026-06-03#01")
    assert buy0.side == "BUY"
    assert abs(buy0.tp1 - 2010.0) < 1e-9
    assert abs(buy0.sl - 1993.0) < 1e-9


def test_keys_stable_when_new_signal_appended():
    base = [
        _mk("BUY", datetime(2026, 6, 4, 8, 5), 2010.0),
        _mk("SELL", datetime(2026, 6, 4, 9, 0), 2005.0),
    ]
    later = base + [_mk("BUY", datetime(2026, 6, 4, 11, 30), 2020.0)]

    m1 = _key_by_identity(A._keyed_signals(base))
    m2 = _key_by_identity(A._keyed_signals(later))

    for ident, key in m1.items():
        assert m2[ident] == key
    assert m2[(datetime(2026, 6, 4, 11, 30), "BUY")] == "2026-06-04#03"


def test_cap_admits_newest_new_first(tmp_path):
    sigs = [
        _mk("BUY", datetime(2026, 6, 4, 8, 5), 2010.0),
        _mk("SELL", datetime(2026, 6, 4, 9, 0), 2005.0),
        _mk("BUY", datetime(2026, 6, 4, 11, 30), 2020.0),
    ]
    keyed = A._keyed_signals(sigs)

    allowed = A._select_allowed_keys(
        keyed, placed_keys=set(), cap=2, placed_count=0, block_new=False
    )
    assert allowed == {"2026-06-04#02", "2026-06-04#03"}

    text = A._render_feed(keyed, allowed, source_tz_offset=3, price_digits=2)
    path = tmp_path / "f.txt"
    path.write_text(text, encoding="utf-8")
    assert sorted(s.signal_key for s in parse_signals_file(path)) == [
        "2026-06-04#02", "2026-06-04#03"
    ]

    allowed2 = A._select_allowed_keys(
        keyed, placed_keys={"2026-06-04#03"}, cap=2, placed_count=1, block_new=False
    )
    assert allowed2 == {"2026-06-04#02", "2026-06-04#03"}

    allowed3 = A._select_allowed_keys(
        keyed, placed_keys={"2026-06-04#02", "2026-06-04#03"},
        cap=2, placed_count=2, block_new=False,
    )
    assert allowed3 == {"2026-06-04#02", "2026-06-04#03"}


def test_block_new_keeps_only_placed():
    sigs = [
        _mk("BUY", datetime(2026, 6, 4, 8, 5), 2010.0),
        _mk("SELL", datetime(2026, 6, 4, 9, 0), 2005.0),
    ]
    keyed = A._keyed_signals(sigs)
    allowed = A._select_allowed_keys(
        keyed, placed_keys={"2026-06-04#01"}, cap=10, placed_count=1, block_new=True
    )
    assert allowed == {"2026-06-04#01"}


def test_min_new_time_excludes_stale_new():
    sigs = [
        _mk("BUY", datetime(2026, 6, 4, 1, 0), 2010.0),
        _mk("SELL", datetime(2026, 6, 4, 12, 0), 2005.0),
    ]
    keyed = A._keyed_signals(sigs)
    allowed = A._select_allowed_keys(
        keyed,
        placed_keys=set(),
        cap=10,
        placed_count=0,
        block_new=False,
        min_new_time=datetime(2026, 6, 4, 6, 0),
    )
    assert allowed == {"2026-06-04#02"}


class _FakeMt5:
    TIMEFRAME_M1 = 1

    def __init__(self, rates):
        self._rates = rates

    def symbol_select(self, *a, **k):
        return True

    def copy_rates_from_pos(self, symbol, timeframe, start_pos, count):
        assert start_pos == 0
        return self._rates[-count:]


def test_recent_closed_bars_drops_forming_bar():
    from trading.xauusd.io.mt5_adapter import Mt5ChartSource

    base = 1_700_000_000
    rates = [
        {"time": base + i * 60, "open": 2000.0 + i, "high": 2001.0 + i,
         "low": 1999.0 + i, "close": 2000.5 + i, "spread": 20}
        for i in range(10)
    ]
    conn = types.SimpleNamespace(mt5=_FakeMt5(rates))
    cs = Mt5ChartSource(conn, symbol="XAUUSD", server_offset_hours=3, history_bars=100)

    bars = cs.recent_closed_bars(5)
    assert len(bars) == 5
    assert bars[-1].open == 2008.0
    assert bars[0].open == 2004.0
    assert all(bars[i].time < bars[i + 1].time for i in range(len(bars) - 1))


def test_append_archive_adds_only_new_keys(tmp_path):
    sigs = [
        _mk("BUY", datetime(2026, 6, 4, 8, 5), 2010.0),
        _mk("SELL", datetime(2026, 6, 4, 9, 0), 2005.0),
    ]
    keyed = A._keyed_signals(sigs)
    archive = tmp_path / "archive.txt"
    archive.write_text(
        A._render_feed(keyed, {"2026-06-04#01"}, source_tz_offset=3, price_digits=2),
        encoding="utf-8",
    )
    A._append_to_archive(archive, keyed, 2)
    assert sorted(s.signal_key for s in parse_signals_file(archive)) == [
        "2026-06-04#01", "2026-06-04#02"
    ]
    A._append_to_archive(archive, keyed, 2)
    assert sorted(s.signal_key for s in parse_signals_file(archive)) == [
        "2026-06-04#01", "2026-06-04#02"
    ]


def test_append_archive_new_day_writes_header(tmp_path):
    yday = [
        _mk("BUY", datetime(2026, 6, 3, 8, 5), 2010.0),
        _mk("SELL", datetime(2026, 6, 3, 9, 0), 2005.0),
    ]
    archive = tmp_path / "archive.txt"
    keyed_yday = A._keyed_signals(yday)
    archive.write_text(
        A._render_feed(keyed_yday, {k for _, k in keyed_yday}, source_tz_offset=3, price_digits=2),
        encoding="utf-8",
    )
    both = yday + [_mk("BUY", datetime(2026, 6, 4, 8, 5), 2020.0)]
    A._append_to_archive(archive, A._keyed_signals(both), 2)
    parsed = parse_signals_file(archive)
    assert sorted(s.signal_key for s in parsed) == [
        "2026-06-03#01", "2026-06-03#02", "2026-06-04#01"
    ]
    newsig = next(s for s in parsed if s.signal_key == "2026-06-04#01")
    assert newsig.signal_time_chart == datetime(2026, 6, 4, 8, 5)


def test_append_archive_creates_fresh_file(tmp_path):
    archive = tmp_path / "sub" / "archive.txt"
    A._append_to_archive(
        archive,
        A._keyed_signals([_mk("BUY", datetime(2026, 6, 4, 8, 5), 2010.0)]),
        2,
    )
    assert [s.signal_key for s in parse_signals_file(archive)] == ["2026-06-04#01"]


def _write_m15_chart(path: Path) -> None:
    rows = ["<DATE>\t<TIME>\t<OPEN>\t<HIGH>\t<LOW>\t<CLOSE>\t<TICKVOL>\t<VOL>\t<SPREAD>"]
    t = datetime(2025, 1, 1, 0, 0)
    price = 2600.0
    for i in range(90):
        open_price = price + i * 0.35
        close = open_price + 0.25
        high = close + 0.55
        low = open_price - 0.55
        rows.append(
            f"{t:%Y.%m.%d}\t{t:%H:%M:%S}\t{open_price:.2f}\t{high:.2f}\t{low:.2f}\t{close:.2f}\t1\t0\t25"
        )
        t += timedelta(minutes=15)
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")


def test_seed_archive_generates_full_history(tmp_path):
    csv = tmp_path / "XAUUSD_M15_202501_ELEV8.csv"
    _write_m15_chart(csv)
    archive = tmp_path / "gen" / "archive.txt"
    args = types.SimpleNamespace(
        backtest_archive=str(archive),
        seed_archive_charts=None,
        seed_archive_m1_charts=None,
        seed_archive_m15_charts=[str(csv)],
        seed_archive_start_date="2025-01-01",
        price_digits=2,
        ema_fast=21,
        ema_slow=55,
        atr_period=14,
        min_atr=0.30,
        max_atr=80.0,
        same_side_spacing_minutes=30,
        max_signals_per_day=40,
        entry_offset=1.0,
        range_width=2.0,
        sl_gap_from_range=3.5,
        tp1_distance=4.0,
        tp2_distance=7.0,
        tp3_distance=12.0,
    )

    A._seed_archive_if_missing(args)
    parsed = parse_signals_file(archive)
    assert len(parsed) > 0
    assert parsed[0].side == "BUY"
    assert parsed[0].signal_key == "2025-01-01#01"

    before = archive.read_text(encoding="utf-8")
    A._seed_archive_if_missing(args)
    assert archive.read_text(encoding="utf-8") == before
