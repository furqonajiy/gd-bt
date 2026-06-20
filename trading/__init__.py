"""Multi-pair trading namespace.

Per-pair packages live under here: ``trading.engine`` (the engine + XAUUSD
config) and ``trading.btcusd`` (the BTC self-rejection backtest, which reuses
the XAUUSD engine path). tools/ and tests/ stay at the repo root and import
these via ``from trading.engine import X`` / ``from trading.btcusd import X``.
"""
