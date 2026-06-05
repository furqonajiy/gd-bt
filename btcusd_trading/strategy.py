"""BTC strategy definition (TEMPLATE).

This is the ONLY file holding BTC-specific values. It is a template: the
numbers below are placeholder sentinels, NOT tradeable values. The runner calls
assert_configured() and refuses to run until you:

  1. Run mt5-info for BTCUSD and paste the verified values into BTC_SPEC.
  2. Choose BTC-magnitude distances for BTC_REJECTION_CONFIG / BTC_STRATEGY_CONFIG
     (gold's $1-$40 distances are noise at ~$100k) and validate them on a BTC
     backtest first -- do NOT tune to chase a number.
  3. Set BTC_SPEC_CONFIGURED = True.

Nothing here trades on placeholders: the sentinels are zeros and the guard
raises until you flip the flag.
"""
from __future__ import annotations

from dataclasses import replace

from xauusd_trading import DEFAULT_CONFIG, RejectionSignalConfig, SymbolSpec

# Flip to True only after every TODO below is filled with verified values.
BTC_SPEC_CONFIGURED = False


# --- 1. Symbol constants: paste from `mt5-info` for BTCUSD -------------------
# Confirm the exact Market Watch name first (BTCUSD vs BTCUSD.r vs a .Daily
# variant) -- pick the continuously-traded one with no daily expiry.
BTC_SPEC = SymbolSpec(
    symbol="BTCUSD",     # TODO: exact Market Watch symbol
    point_value=0.0,     # TODO: tick size (mt5-info trade_tick_size / point); = 10**-digits
    digits=0,            # TODO: mt5-info digits
    contract_size=0.0,   # TODO: mt5-info trade_contract_size (units per 1.00 lot)
    min_lot=0.0,         # TODO: mt5-info volume_min
    lot_step=0.0,        # TODO: mt5-info volume_step
)


# --- 2. Self-rejection signal generation (BTC magnitude) ---------------------
# Distances are in PRICE units. session hours None -> BTC trades ~24/7.
# All distance TODOs must be re-derived for BTC's price scale + validated on a
# BTC backtest before going live.
BTC_REJECTION_CONFIG = RejectionSignalConfig(
    lookback_bars=20,
    min_wick=0.0,             # TODO: BTC-scale wick threshold
    min_bar_range=0.0,        # TODO
    wick_body_ratio=1.2,
    zone_buffer=0.0,          # TODO
    zone_size=0.0,            # TODO
    cooldown_minutes=20,
    same_zone_cooldown_minutes=120,
    max_spread_points=None,   # TODO: BTC spread cap in points (mt5-info-relative)
    session_start_hour=None,  # 24/7
    session_end_hour=None,    # 24/7
    entry_range_width=0.0,    # TODO
    sl_distance=0.0,          # TODO
    tp1_distance=0.0,         # TODO  (tp2 > tp1, tp3 > tp2 required)
    tp2_distance=0.0,         # TODO
    tp3_distance=0.0,         # TODO
    price_digits=2,           # TODO: match BTC_SPEC.digits
)


# --- 3. Executor params (shape mirrors the validated trailing self-strategy) -
# Structural fields are set; magnitude/sizing fields are TODO (spec + backtest).
# DEFAULT_CONFIG stays the gold DD40 anchor -- this is a derived copy, never a
# mutation of it.
BTC_STRATEGY_CONFIG = replace(
    DEFAULT_CONFIG,
    initial_capital=0.0,          # TODO: BTC sub-account / allocation
    sizing_mode="risk",
    risk_per_signal=0.0,          # TODO: small -- shares the account DD budget with gold
    minimum_lot=0.0,              # TODO: = BTC_SPEC.min_lot
    lot_step=0.0,                 # TODO: = BTC_SPEC.lot_step
    entry_count=1,
    entry_ladder="range_uniform",
    entry_sl_gap=0.0,             # TODO: BTC-scale
    activation_delay_minutes=0,
    pending_expiry_minutes=630,
    max_hold_minutes=15,
    sl_multiplier=0.0,            # TODO: BTC-scale
    final_target="TP1",
    lock_after_tp1=True,
    lock_after_tp2=True,
    profit_lock_mode="tp_levels",
    bep_trigger_distance=0.0,     # TODO: BTC-scale
    tp1_lock_fraction=0.5,
    trailing_open_distance=0.0,   # TODO: >= broker stops_level, BTC-scale
    trailing_close_distance=0.0,  # TODO: >= broker stops_level, BTC-scale
    bonus_per_closed_lot=3.0,
)


def assert_configured() -> None:
    """Raise unless BTC_SPEC has been filled from mt5-info and verified.

    The runner calls this before doing anything, so the template can never place
    an order on placeholder numbers.
    """
    if not BTC_SPEC_CONFIGURED:
        raise RuntimeError(
            "btcusd_trading.strategy is an unconfigured template. Paste verified "
            "mt5-info values into BTC_SPEC + BTC_*_CONFIG, validate on a BTC "
            "backtest, then set BTC_SPEC_CONFIGURED = True."
        )