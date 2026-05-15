"""Source adapters — the boundary between the engine and the outside world.

The engine talks only to these abstract interfaces. The CSV impls live
here; the MT5 impl lives in io/mt5_adapter.py with the same contract.
"""
from __future__ import annotations
from abc import ABC, abstractmethod
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional

import pandas as pd

from xauusd_trading import Bar, latest_bar, load_chart, slice_bars, iter_bars
from xauusd_trading import Position


# ---------------------------------------------------------------------------
# chart source
# ---------------------------------------------------------------------------

class ChartSource(ABC):
    """Abstract source of 1-minute bars in chart timezone (GMT+3)."""

    @abstractmethod
    def latest(self, at_or_before: Optional[datetime] = None) -> Optional[Bar]: ...

    @abstractmethod
    def bars_between(self, start: datetime, end: datetime) -> Iterable[Bar]:
        """Bars in [start, end] inclusive, chronologically."""

    @abstractmethod
    def first_time(self) -> Optional[datetime]: ...

    @abstractmethod
    def last_time(self) -> Optional[datetime]: ...


class CsvChartSource(ChartSource):
    """Backed by one or more MT5 M1 CSV files."""

    def __init__(self, paths: Iterable[Path]):
        self._df = load_chart([Path(p) for p in paths])

    @property
    def dataframe(self) -> pd.DataFrame:
        return self._df

    def first_time(self) -> Optional[datetime]:
        if self._df.empty:
            return None
        return self._df["time"].iloc[0].to_pydatetime()

    def last_time(self) -> Optional[datetime]:
        if self._df.empty:
            return None
        return self._df["time"].iloc[-1].to_pydatetime()

    def latest(self, at_or_before: Optional[datetime] = None) -> Optional[Bar]:
        return latest_bar(self._df, at_or_before)

    def bars_between(self, start: datetime, end: datetime) -> Iterable[Bar]:
        return iter_bars(slice_bars(self._df, start, end))


# ---------------------------------------------------------------------------
# position source
# ---------------------------------------------------------------------------

class PositionSource(ABC):
    """Abstract source of currently-open Positions."""

    @abstractmethod
    def open_positions(self) -> list[Position]: ...

    @abstractmethod
    def equity(self) -> float:
        """Current account equity (used to size new signals)."""


class ManualPositionSource(PositionSource):
    """In-memory source. The caller passes positions and equity in directly.

    The backtest runner mutates these between signals. Live forward-mode
    constructs and passes them per call.
    """

    def __init__(self, equity: float, positions: Optional[list[Position]] = None):
        self._equity = equity
        self._positions: list[Position] = list(positions or [])

    def open_positions(self) -> list[Position]:
        return [p for p in self._positions if not p.is_terminal()]

    def equity(self) -> float:
        return self._equity

    def add(self, position: Position) -> None:
        self._positions.append(position)

    def set_equity(self, value: float) -> None:
        self._equity = value

    def all_positions(self) -> list[Position]:
        return list(self._positions)