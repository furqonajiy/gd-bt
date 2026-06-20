"""trading.xauusd — XAUUSD pair package (thin facade over trading.engine).

The shared, pair-agnostic engine now lives in ``trading.engine``. This package
re-exports its full public surface so ``from trading.xauusd import X`` keeps
working, and is the home for any XAUUSD-specific configuration. Other pairs
(e.g. ``trading.btcusd``) import ``trading.engine`` the same way.
"""
from __future__ import annotations

from trading.engine import *  # noqa: F401,F403
from trading.engine import __all__  # re-export the public surface list
