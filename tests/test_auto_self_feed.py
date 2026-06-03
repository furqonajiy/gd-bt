"""Parity/safety tests for tools/auto_self.py and Mt5ChartSource.recent_closed_bars.

These exercise the pure feed-construction logic (deterministic per-day keys,
guard withholding, byte-compatibility with the real signal parser) and the
forming-bar exclusion in the chart adapter. No MT5 required.
"""
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
from xauusd_trading import parse_signals_file  # noqa: E402
from xauusd_trading.strategy.self_signals import GeneratedSignal  # noqa: E402


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
        signal_time_chart=when, side=side,
        r1=round(r1, 2), r2=round(r2, 2), sl=round(sl, 2),
        tp1=round(tp1, 2), tp2=round(tp2, 2), tp3=round(tp3, 2),
        source_bar_time=when - timedelta(minutes=1),
        entry_anchor=anchor, recent_level=anchor,
        wick_size=1.5, body_size=0.5, bar_range=2.0,
        spread_points=20, reason="test",
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

    p = tmp_path / "feed.txt"
    p.write_text(text, encoding="utf-8")
    parsed = parse_signals_file(p)

    assert sorted(s.signal_key for s in parsed) == [
        "2026-06-03#01", "2026-06-03#02", "2026-06-04#01"
    ]
    buy0 = next(s for s in parsed if s.signal_key == "2026-06-03#01")
    assert buy0.side == "BUY"
    assert abs(buy0.tp1 - 2010.0) < 1e-9
    assert abs(buy0.sl - 1993.0) < 1e-9  # (2000 - 2) - 5


def test_keys_stable_when_new_signal_appended():
    base = [
        _mk("BUY", datetime(2026, 6, 4, 8, 5), 2010.0),
        _mk("SELL", datetime(2026, 6, 4, 9, 0), 2005.0),
    ]
    later = base + [_mk("BUY", datetime(2026, 6, 4, 11, 30), 2020.0)]

    m1 = _key_by_identity(A._keyed_signals(base))
    m2 = _key_by_identity(A._keyed_signals(later))

    for ident, key in m1.items():
        assert m2[ident] == key, "an already-placed signal's key must not shift"
    assert m2[(datetime(2026, 6, 4, 11, 30), "BUY")] == "2026-06-04#03"


def test_cap_withholds_latest_new_and_preserves_numbers(tmp_path):
    sigs = [
        _mk("BUY", datetime(2026, 6, 4, 8, 5), 2010.0),
        _mk("SELL", datetime(2026, 6, 4, 9, 0), 2005.0),
        _mk("BUY", datetime(2026, 6, 4, 11, 30), 2020.0),
    ]
    keyed = A._keyed_signals(sigs)

    # nothing placed yet, cap 2 -> earliest two new admitted, #03 withheld
    allowed = A._select_allowed_keys(
        keyed, placed_keys=set(), cap=2, placed_count=0, block_new=False
    )
    assert allowed == {"2026-06-04#01", "2026-06-04#02"}

    text = A._render_feed(keyed, allowed, source_tz_offset=3, price_digits=2)
    p = tmp_path / "f.txt"
    p.write_text(text, encoding="utf-8")
    assert sorted(s.signal_key for s in parse_signals_file(p)) == [
        "2026-06-04#01", "2026-06-04#02"
    ]

    # #01,#02 now placed and one slot freed -> #03 admitted, placed kept
    allowed2 = A._select_allowed_keys(
        keyed,
        placed_keys={"2026-06-04#01", "2026-06-04#02"},
        cap=2, placed_count=1, block_new=False,
    )
    assert allowed2 == {"2026-06-04#01", "2026-06-04#02", "2026-06-04#03"}

    text2 = A._render_feed(keyed, allowed2, source_tz_offset=3, price_digits=2)
    p2 = tmp_path / "f2.txt"
    p2.write_text(text2, encoding="utf-8")
    assert "2026-06-04#03" in {s.signal_key for s in parse_signals_file(p2)}


def test_block_new_keeps_only_placed():
    sigs = [
        _mk("BUY", datetime(2026, 6, 4, 8, 5), 2010.0),
        _mk("SELL", datetime(2026, 6, 4, 9, 0), 2005.0),
    ]
    keyed = A._keyed_signals(sigs)
    allowed = A._select_allowed_keys(
        keyed, placed_keys={"2026-06-04#01"},
        cap=10, placed_count=1, block_new=True,
    )
    assert allowed == {"2026-06-04#01"}


def test_min_new_time_excludes_stale_new():
    sigs = [
        _mk("BUY", datetime(2026, 6, 4, 1, 0), 2010.0),   # old
        _mk("SELL", datetime(2026, 6, 4, 12, 0), 2005.0),  # fresh
    ]
    keyed = A._keyed_signals(sigs)
    allowed = A._select_allowed_keys(
        keyed, placed_keys=set(), cap=10, placed_count=0,
        block_new=False, min_new_time=datetime(2026, 6, 4, 6, 0),
    )
    assert allowed == {"2026-06-04#02"}


# --- adapter: forming-bar exclusion -----------------------------------------

class _FakeMt5:
    TIMEFRAME_M1 = 1

    def __init__(self, rates):
        self._rates = rates  # oldest -> newest; last element is the forming bar

    def symbol_select(self, *a, **k):
        return True

    def copy_rates_from_pos(self, symbol, timeframe, start_pos, count):
        assert start_pos == 0
        return self._rates[-count:]  # MT5 returns chronological; newest is last


def test_recent_closed_bars_drops_forming_bar():
    from xauusd_trading.io.mt5_adapter import Mt5ChartSource

    base = 1_700_000_000
    rates = [
        {"time": base + i * 60, "open": 2000.0 + i, "high": 2001.0 + i,
         "low": 1999.0 + i, "close": 2000.5 + i, "spread": 20}
        for i in range(10)
    ]
    conn = types.SimpleNamespace(mt5=_FakeMt5(rates))
    cs = Mt5ChartSource(conn, symbol="XAUUSD", server_offset_hours=3, history_bars=100)

    bars = cs.recent_closed_bars(5)
    assert len(bars) == 5                 # asked 5 closed -> fetch 6, drop forming
    assert bars[-1].open == 2008.0        # index 8 (index 9 is forming, dropped)
    assert bars[0].open == 2004.0         # index 4
    assert all(bars[i].time < bars[i + 1].time for i in range(len(bars) - 1))

# --- cumulative backtest archive -------------------------------------------

def test_append_archive_adds_only_new_keys(tmp_path):
    sigs = [
        _mk("BUY", datetime(2026, 6, 4, 8, 5), 2010.0),
        _mk("SELL", datetime(2026, 6, 4, 9, 0), 2005.0),
    ]
    keyed = A._keyed_signals(sigs)
    arch = tmp_path / "archive.txt"
    arch.write_text(
        A._render_feed(keyed, {"2026-06-04#01"}, source_tz_offset=3, price_digits=2),
        encoding="utf-8",
    )
    A._append_to_archive(arch, keyed, 2)
    assert sorted(s.signal_key for s in parse_signals_file(arch)) == [
        "2026-06-04#01", "2026-06-04#02"
    ]
    # idempotent: re-appending the same keyed set adds nothing
    A._append_to_archive(arch, keyed, 2)
    assert sorted(s.signal_key for s in parse_signals_file(arch)) == [
        "2026-06-04#01", "2026-06-04#02"
    ]


def test_append_archive_new_day_writes_header(tmp_path):
    yday = [
        _mk("BUY", datetime(2026, 6, 3, 8, 5), 2010.0),
        _mk("SELL", datetime(2026, 6, 3, 9, 0), 2005.0),
    ]
    arch = tmp_path / "archive.txt"
    kyd = A._keyed_signals(yday)
    arch.write_text(
        A._render_feed(kyd, {k for _, k in kyd}, source_tz_offset=3, price_digits=2),
        encoding="utf-8",
    )
    both = yday + [_mk("BUY", datetime(2026, 6, 4, 8, 5), 2020.0)]
    A._append_to_archive(arch, A._keyed_signals(both), 2)
    parsed = parse_signals_file(arch)
    assert sorted(s.signal_key for s in parsed) == [
        "2026-06-03#01", "2026-06-03#02", "2026-06-04#01"
    ]
    newsig = next(s for s in parsed if s.signal_key == "2026-06-04#01")
    assert newsig.signal_time_chart == datetime(2026, 6, 4, 8, 5)


def test_append_archive_creates_fresh_file(tmp_path):
    arch = tmp_path / "sub" / "archive.txt"  # parent missing
    A._append_to_archive(arch, A._keyed_signals([_mk("BUY", datetime(2026, 6, 4, 8, 5), 2010.0)]), 2)
    assert [s.signal_key for s in parse_signals_file(arch)] == ["2026-06-04#01"]


def _write_export_csv(path, rows):
    header = "<DATE>\t<TIME>\t<OPEN>\t<HIGH>\t<LOW>\t<CLOSE>\t<TICKVOL>\t<VOL>\t<SPREAD>"
    lines = [header]
    for (d, t, o, h, l, c, sp) in rows:
        lines.append(f"{d}\t{t}\t{o:.2f}\t{h:.2f}\t{l:.2f}\t{c:.2f}\t1\t0\t{sp}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_seed_archive_generates_full_history(tmp_path):
    import types
    csv = tmp_path / "XAUUSD_M1_202401_ELEV8.csv"
    rows = [("2024.01.01", f"07:{i:02d}:00", 2000.0, 2000.0, 2000.0, 2000.0, 20) for i in range(20)]
    rows.append(("2024.01.01", "07:20:00", 2000.5, 2001.2, 1999.0, 2001.0, 20))  # bullish rejection
    _write_export_csv(csv, rows)

    rcfg = A.RejectionSignalConfig()
    archive = tmp_path / "gen" / "archive.txt"
    args = types.SimpleNamespace(
        backtest_archive=str(archive),
        seed_archive_charts=[str(csv)],
        seed_archive_start_date="2024-01-01",
    )
    A._seed_archive_if_missing(args, rcfg)
    parsed = parse_signals_file(archive)
    assert len(parsed) == 1
    assert parsed[0].side == "BUY"
    assert parsed[0].signal_key == "2024-01-01#01"
    assert parsed[0].signal_time_chart == datetime(2024, 1, 1, 7, 21)  # rejected_bar + 1min

    # skipped when the archive already exists (idempotent startup)
    before = archive.read_text()
    A._seed_archive_if_missing(args, rcfg)
    assert archive.read_text() == before