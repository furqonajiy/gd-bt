"""trading.xauusd.cli — XAUUSD pair entry; delegates to the shared engine CLI.

Keeps ``python -m trading.xauusd.cli`` working after the engine moved to
``trading.engine``. The CLI is generic (any ``--mt5-symbol``) with XAUUSD
defaults.
"""
from __future__ import annotations

from trading.engine.cli import main  # noqa: F401

if __name__ == "__main__":
    raise SystemExit(main())
