"""Signal parsing.

Reads the human-format signal file and produces Signal objects with
chart-time (GMT+3) timestamps. Use `compute_entries(signal, config)`
for runtime entry prices under the active StrategyConfig.

Signal file format:

    2026-01-22 GMT+7
    1. BUY XAUUSD 4567 - 4565 SL 4560 TP1 4572 TP2 4579 TP3 4589 10:36 AM
    2. SELL XAUUSD 4600 - 4602 SL 4606 TP1 4597 TP2 4592 TP3 4585 11:04 AM

The legacy 3-entry ladder stored on Signal.entries is for parse-time
validation only; trading uses compute_entries() instead.
"""
from __future__ import annotations
import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from xauusd_trading import CHART_TIMEZONE_OFFSET

if TYPE_CHECKING:
    from xauusd_trading import StrategyConfig


_DATE_RE = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2})\s+GMT\s*(?P<sign>[+-])\s*(?P<offset>\d+)$",
    re.IGNORECASE,
)
_SIGNAL_RE = re.compile(
    r"^\s*(?P<id>\d+)\.\s*"
    r"(?P<side>BUY|SELL)\s+XAUUSD\s+"
    r"(?P<r1>\d+(?:\.\d+)?)\s*-\s*(?P<r2>\d+(?:\.\d+)?)\s+"
    r"SL\s+(?P<sl>\d+(?:\.\d+)?)\s+"
    r"TP1\s+(?P<tp1>\d+(?:\.\d+)?)\s+"
    r"TP2\s+(?P<tp2>\d+(?:\.\d+)?)\s+"
    r"TP3\s+(?P<tp3>\d+(?:\.\d+)?)\s+"
    r"(?P<time>\d{1,2}:\d{2}\s*[AP]M)\s*$",
    re.IGNORECASE,
)


@dataclass
class Signal:
    """One trading signal in chart-time (GMT+3) coordinates."""
    global_id: int
    day_id: int
    source_date: str
    source_tz_offset: int
    source_time_text: str
    signal_time_source: datetime
    signal_time_chart: datetime
    side: str
    r1: float
    r2: float
    sl: float
    tp1: float
    tp2: float
    tp3: float
    entries: list[float]                 # legacy 3-entry ladder; parse-time validation only
    anomalies: list[str] = field(default_factory=list)
    structural_anomaly: bool = False

    @property
    def signal_key(self) -> str:
        return f"{self.source_date}#{self.day_id:02d}"

    @property
    def range_high(self) -> float:
        return max(self.r1, self.r2)

    @property
    def range_low(self) -> float:
        return min(self.r1, self.r2)


# ---------------------------------------------------------------------------
# entry-price generation (used at trade time)
# ---------------------------------------------------------------------------

def _linspace(start: float, stop: float, n: int) -> list[float]:
    """Numpy-free linspace returning Python floats."""
    if n <= 0:
        return []
    if n == 1:
        return [float(start)]
    step = (stop - start) / (n - 1)
    return [float(start + step * i) for i in range(n)]


def _signal_range_3_entries(signal: Signal) -> list[float]:
    """Provider-native ladder. BUY: [H, H-1, L]; SELL: [L, L+1, H]."""
    high, low = signal.range_high, signal.range_low
    return [high, high - 1.0, low] if signal.side == "BUY" else [low, low + 1.0, high]


def compute_entries(signal: Signal, config: "StrategyConfig") -> list[float]:
    """Return entry prices for one signal under the given config's ladder.

    signal_range_3: provider-native entry rule. For 3 entries this is exactly
        BUY [H, H-1, L] / SELL [L, L+1, H]. For 2 entries, use the first two
        provider-native entries; for 1 entry, use the first/best entry. For
        counts above 3, append range-uniform entries while preserving the
        first three provider-native entries.
    range_uniform: spread n entries evenly inside the signal's range.
    range_to_sl: spread n entries from the best price toward the signal SL,
        leaving entry_sl_gap dollars between the deepest entry and the SL.
        If the gap would put the deepest entry outside the range (anomalous
        signal, e.g. SL on wrong side), falls back to range_uniform.
    """
    n = config.entry_count
    if n < 1:
        raise ValueError(f"entry_count must be >= 1, got {n}")
    if n == 1:
        return [float(signal.range_high if signal.side == "BUY" else signal.range_low)]

    ladder = config.entry_ladder
    gap = config.entry_sl_gap

    if ladder == "signal_range_3":
        base = _signal_range_3_entries(signal)
        if n <= 3:
            return base[:n]
        extra = _linspace(signal.range_high, signal.range_low, n) if signal.side == "BUY" else _linspace(signal.range_low, signal.range_high, n)
        out: list[float] = []
        for value in base + extra:
            if not any(math.isclose(value, existing, abs_tol=1e-9) for existing in out):
                out.append(value)
            if len(out) == n:
                return out
        return out

    if ladder == "range_uniform":
        if signal.side == "BUY":
            return _linspace(signal.range_high, signal.range_low, n)
        return _linspace(signal.range_low, signal.range_high, n)

    if ladder == "range_to_sl":
        if signal.side == "BUY":
            far = signal.sl + gap
            if far >= signal.range_high:
                return _linspace(signal.range_high, signal.range_low, n)
            return _linspace(signal.range_high, far, n)
        far = signal.sl - gap
        if far <= signal.range_low:
            return _linspace(signal.range_low, signal.range_high, n)
        return _linspace(signal.range_low, far, n)

    raise ValueError(f"Unknown entry_ladder: {ladder!r}")


# ---------------------------------------------------------------------------
# parsing
# ---------------------------------------------------------------------------

def _gmt_offset(sign: str, offset: str) -> int:
    n = int(offset)
    return n if sign == "+" else -n


def _to_chart_tz(dt: datetime, source_offset: int) -> datetime:
    return dt + timedelta(hours=CHART_TIMEZONE_OFFSET - source_offset)


def _entries_for(side: str, r1: float, r2: float) -> list[float]:
    """Legacy 3-entry rule. BUY: [H, H-1, L]; SELL: [L, L+1, H]."""
    high, low = max(r1, r2), min(r1, r2)
    return [high, high - 1.0, low] if side == "BUY" else [low, low + 1.0, high]


def _validate(side, r1, r2, sl, tp1, tp2, tp3, entries):
    anomalies: list[str] = []
    structural = False
    if not math.isclose(abs(r1 - r2), 2.0, abs_tol=1e-9):
        anomalies.append(f"Range width is {abs(r1 - r2):.2f}, expected 2.00")
    if side == "BUY":
        if sl >= min(entries):
            anomalies.append("BUY SL is not below all entries"); structural = True
        if not (tp1 < tp2 < tp3):
            anomalies.append("BUY TP order is inconsistent"); structural = True
        if tp1 <= max(entries):
            anomalies.append("BUY TP1 is not above all entries"); structural = True
    else:
        if sl <= max(entries):
            anomalies.append("SELL SL is not above all entries"); structural = True
        if not (tp1 > tp2 > tp3):
            anomalies.append("SELL TP order is inconsistent"); structural = True
        if tp1 >= min(entries):
            anomalies.append("SELL TP1 is not below all entries"); structural = True
    return anomalies, structural


def parse_signal_line(
        line: str,
        source_date: str,
        source_offset: int,
        global_id: int,
) -> Optional[Signal]:
    """Parse one signal line. Returns None if the line does not match."""
    m = _SIGNAL_RE.match(line.strip())
    if not m:
        return None
    side = m.group("side").upper()
    r1, r2 = float(m.group("r1")), float(m.group("r2"))
    sl = float(m.group("sl"))
    tp1, tp2, tp3 = float(m.group("tp1")), float(m.group("tp2")), float(m.group("tp3"))
    time_text = m.group("time").upper().replace(" ", "")
    src_dt = datetime.strptime(f"{source_date} {time_text}", "%Y-%m-%d %I:%M%p")
    chart_dt = _to_chart_tz(src_dt, source_offset)
    entries = _entries_for(side, r1, r2)
    anomalies, structural = _validate(side, r1, r2, sl, tp1, tp2, tp3, entries)
    return Signal(
        global_id=global_id, day_id=int(m.group("id")),
        source_date=source_date, source_tz_offset=source_offset,
        source_time_text=m.group("time"),
        signal_time_source=src_dt, signal_time_chart=chart_dt,
        side=side, r1=r1, r2=r2, sl=sl, tp1=tp1, tp2=tp2, tp3=tp3,
        entries=entries, anomalies=anomalies, structural_anomaly=structural,
    )


def parse_signals_file(path: Path) -> list[Signal]:
    """Parse a multi-day signals file. Returns signals in chart-time order."""
    signals: list[Signal] = []
    current_date: Optional[str] = None
    current_offset: Optional[int] = None
    next_id = 1

    for raw in Path(path).read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line:
            continue
        date_match = _DATE_RE.match(line)
        if date_match:
            current_date = date_match.group("date")
            current_offset = _gmt_offset(date_match.group("sign"), date_match.group("offset"))
            continue
        if current_date is None or current_offset is None:
            continue
        sig = parse_signal_line(line, current_date, current_offset, next_id)
        if sig is None:
            continue
        signals.append(sig)
        next_id += 1

    signals.sort(key=lambda s: (s.signal_time_chart, s.global_id))
    return signals


def parse_one_signal(text: str, source_date: str, source_offset: int) -> Signal:
    """Parse a single line provided directly (e.g. from the live decide CLI).

    `source_date` is ISO (YYYY-MM-DD) in the same timezone as the line's
    clock time. `source_offset` is the GMT offset of that clock time.
    """
    sig = parse_signal_line(text, source_date, source_offset, global_id=1)
    if sig is None:
        raise ValueError(f"Could not parse signal: {text!r}")
    return sig
