"""Per-symbol broker/contract constants.

The engine, executor, parser, and chart loader are symbol-agnostic. Everything
that differs between instruments -- point/tick size, contract size, lot
granularity, price digits -- lives here, so adding an instrument is a data
change, not an engine fork.

`point_value` is the tick size (smallest price increment = 10**-digits). It
converts an integer SPREAD column (in points) into a price-units spread. For a
2-digit symbol that is 0.01; for a symbol quoted to more/fewer digits it differs,
which is why chart loading takes it as a parameter rather than a global constant.
"""
from __future__ import annotations

from dataclasses import dataclass

from trading.xauusd.core.config import CONTRACT_SIZE_OZ, POINT_VALUE


@dataclass(frozen=True)
class SymbolSpec:
    symbol: str            # exact MT5 Market Watch name (may carry a broker suffix)
    point_value: float     # tick size: 10**-digits; spread points -> price units
    digits: int            # price decimal places
    contract_size: float   # units per 1.00 lot (XAUUSD: 100 oz; most crypto: 1 coin)
    min_lot: float
    lot_step: float


# Gold replicates the constants the codebase has always used, so every gold
# path stays byte-identical to pre-SymbolSpec behaviour.
XAU_SPEC = SymbolSpec(
    symbol="XAUUSD",
    point_value=POINT_VALUE,         # 0.01
    digits=2,
    contract_size=CONTRACT_SIZE_OZ,  # 100.0 oz
    min_lot=0.01,
    lot_step=0.01,
)