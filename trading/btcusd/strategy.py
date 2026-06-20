"""BTC strategy definition.

BTC_SPEC fields are facts from mt5-info (ELEV8 BTCUSD, 2026-06-06). The distances
in BTC_REJECTION_CONFIG / BTC_STRATEGY_CONFIG are a FIRST-PRINCIPLES starting
geometry, anchored to the broker's hard constraints (stops_level $62, spread ~$28)
and BTC's ~$60.7k price -- NOT tuned to a backtest number. The first backtest's
job is viability (signal count, fill rate, stop-out vs target-hit), not P&L
bragging; adjust geometry for what's structurally broken, never to chase profit.

BTC_SPEC_CONFIGURED = True enables the (non-trading) backtest only. There is no
live runner yet, and this geometry is unvalidated -- do NOT trade it live until a
BTC backtest clears the bar (fixed-lot net profit positive, combined gold+BTC
DD <= 40%) and the live runner (batch 2b) is built.
"""
from __future__ import annotations

from dataclasses import replace

from trading.engine import DEFAULT_CONFIG, MomentumSignalConfig, RejectionSignalConfig, SymbolSpec

BTC_SPEC_CONFIGURED = True


# --- 1. Symbol constants (facts: mt5-info ELEV8 BTCUSD) ----------------------
BTC_SPEC = SymbolSpec(
    symbol="BTCUSD",
    point_value=0.01,     # point / trade_tick_size
    digits=2,
    contract_size=1.0,    # 1 lot = 1 BTC
    min_lot=0.01,
    lot_step=0.01,
)


# --- 2. Self-rejection signal generation (BTC magnitude; price units = $) ----
# Distances respect stops_level ($62) and clear spread (~$28). session None = 24/7.
# min_wick / min_bar_range gate how many candles qualify -- calibrate frequency
# off the first backtest's signal count, not its P&L.
BTC_REJECTION_CONFIG = RejectionSignalConfig(
    lookback_bars=20,
    min_wick=50.0,
    min_bar_range=50.0,
    wick_body_ratio=1.2,
    zone_buffer=30.0,
    zone_size=50.0,
    cooldown_minutes=20,
    same_zone_cooldown_minutes=120,
    max_spread_points=5000,    # $50 cap; normal ~$28, skips spread spikes
    session_start_hour=None,   # 24/7
    session_end_hour=None,     # 24/7
    entry_range_width=40.0,
    sl_distance=120.0,         # effective SL ($120) clears the $62 floor
    tp1_distance=120.0,        # 1:1
    tp2_distance=240.0,        # 2:1
    tp3_distance=480.0,        # 4:1
    price_digits=2,
)


# --- 2b. Breakout-continuation (momentum) signal -- the inverse hypothesis ---
# The edge gate found rejection anti-predictive on BTC (price continued past the
# level), so the next signal to measure is its mirror: trade WITH a strong bar
# that closes beyond a recent extreme. First-principles geometry (same scale as
# rejection); unvalidated -- this exists for the edge gate, not for live trading.
BTC_MOMENTUM_CONFIG = MomentumSignalConfig(
    lookback_bars=20,
    min_body=50.0,             # strong directional body
    min_bar_range=80.0,        # range expansion: a real impulse, not drift
    close_position=0.6,        # close in the top/bottom 40% of the bar
    breakout_buffer=0.0,       # close strictly beyond the recent extreme
    cooldown_minutes=20,
    same_zone_cooldown_minutes=120,
    zone_size=50.0,
    max_spread_points=5000,
    session_start_hour=None,   # 24/7
    session_end_hour=None,     # 24/7
    entry_range_width=40.0,
    sl_distance=120.0,
    tp1_distance=120.0,
    tp2_distance=240.0,
    tp3_distance=480.0,
    price_digits=2,
)


# --- 2c. Momentum on M15 -- higher timeframe, where the spread is a small ----
# fraction of the move and trends carry structure M1 lacks. bar_minutes=15 so the
# signal fires at the M15 close (no look-ahead). Thresholds scaled to M15
# magnitude; first-principles, unvalidated -- for the edge gate only.
BTC_MOMENTUM_M15_CONFIG = MomentumSignalConfig(
    bar_minutes=15,
    lookback_bars=20,          # 20 x M15 = a 5-hour breakout window
    min_body=150.0,
    min_bar_range=250.0,
    close_position=0.6,
    breakout_buffer=0.0,
    cooldown_minutes=60,
    same_zone_cooldown_minutes=240,
    zone_size=100.0,
    max_spread_points=5000,
    session_start_hour=None,   # 24/7
    session_end_hour=None,     # 24/7
    entry_range_width=100.0,
    sl_distance=300.0,
    tp1_distance=300.0,
    tp2_distance=600.0,
    tp3_distance=1200.0,
    price_digits=2,
)


# --- 3. Executor params: FIRST backtest = raw-edge read --------------------
# Fixed-lot (judge per-trade edge on fixed lot, per the mission), trailing OFF,
# no locks, exit at TP1 or SL -- a clean binary to see if entering on rejections
# beats spread on BTC. Risk-sizing + trailing + combined-DD come AFTER raw edge
# is confirmed. DEFAULT_CONFIG (gold DD40 anchor) is untouched; this is a copy.
BTC_STRATEGY_CONFIG = replace(
    DEFAULT_CONFIG,
    initial_capital=10_000.0,     # account ballpark (shared with gold)
    sizing_mode="fixed",
    lot_per_entry=0.01,
    minimum_lot=0.01,
    lot_step=0.01,
    entry_count=1,
    entry_ladder="range_uniform",
    entry_sl_gap=20.0,            # unused with range_uniform
    activation_delay_minutes=0,
    pending_expiry_minutes=630,
    max_hold_minutes=90,
    sl_multiplier=1.0,            # effective SL = signal SL distance
    final_target="TP1",
    lock_after_tp1=False,
    lock_after_tp2=False,
    trailing_open_distance=0.0,   # OFF for the raw-edge read
    trailing_close_distance=0.0,  # OFF
    bonus_per_closed_lot=0.0,     # no-bonus edge ($3/lot is negligible at 0.01 lot anyway)
    bep_trigger_distance=30.0,    # unused (no locks)
)


def assert_configured() -> None:
    """Raise unless BTC_SPEC has been filled from mt5-info and verified."""
    if not BTC_SPEC_CONFIGURED:
        raise RuntimeError(
            "trading.btcusd.strategy is an unconfigured template. Paste verified "
            "mt5-info values into BTC_SPEC + BTC_*_CONFIG, validate on a BTC "
            "backtest, then set BTC_SPEC_CONFIGURED = True."
        )