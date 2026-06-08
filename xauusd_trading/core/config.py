"""Strategy configuration.

Default strategy: bonus-aware provider execution contract.

The defaults in this branch are the validated DD40-compatible provider contract.
Optional trailing-open, trailing-close, and trend-runner settings are available
for research/live parity but default to disabled, so existing backtests and Auto
runs keep their current behaviour unless explicitly enabled.

Research toggles are set explicitly per run via CLI flags (e.g.
``--trailing-open-distance``, ``--trailing-close-distance``, ``--trend-runner``;
see ``_add_strategy_overrides`` in ``cli_orig.py``). They are deliberately NOT read
from the environment, so ``DEFAULT_CONFIG`` is always the DD40 contract regardless
of shell state.
"""
from __future__ import annotations

from dataclasses import dataclass


CONTRACT_SIZE_OZ = 100.0       # 1.0 lot XAUUSD = 100 oz; 0.5 lot = $50 per $1 move
POINT_VALUE = 0.01             # 1 spread point = $0.01
CHART_TIMEZONE_OFFSET = 3      # MT5 CSV is GMT+3


@dataclass(frozen=True)
class StrategyConfig:
    initial_capital: float = 1_000.0

    sizing_mode: str = "risk"              # "fixed" | "risk"
    lot_per_entry: float = 0.5
    risk_per_signal: float = 0.05575
    minimum_lot: float = 0.01
    lot_step: float = 0.01

    # Bonus/rebate. Broker bonus is modeled as cash received for every lot that
    # closes. Set to 0.0 to reproduce pure trading P&L.
    bonus_per_closed_lot: float = 3.0

    # Entry plan.
    entry_count: int = 3
    entry_ladder: str = "range_to_sl"      # "signal_range_3" | "range_uniform" | "range_to_sl"
    entry_sl_gap: float = 2.0               # only used when entry_ladder="range_to_sl"

    # Execution timing.
    activation_delay_minutes: int = 3
    pending_expiry_minutes: int = 630
    max_hold_minutes: int = 90

    # Stop/target management.
    sl_multiplier: float = 1.61
    final_target: str = "TP3"
    lock_after_tp1: bool = True
    lock_after_tp2: bool = False

    # Stop-loss placement across the entry ladder. Default (False) gives each
    # entry its own stop `base_stop_distance` from its own price. When True the
    # whole ladder shares ONE stop level, anchored on the first (reference)
    # entry; risk-sizing accounts for each leg's real distance to that level.
    shared_sl: bool = False

    # Per-entry targets (research). Empty = legacy single `final_target` for the
    # whole position. When set it holds one token per entry, each in
    # {"TP1","TP2","TP3","RUN"}; RUN = hold at TP3 then trail by
    # trailing_close_distance. Gated so all other behaviour is untouched when off.
    per_entry_targets: tuple[str, ...] = ()

    # Per-leg break-even+ lock: once a filled leg is this many price units in
    # favour, move its SL to entry +/- bep_buffer. 0.0 = off. Only active in the
    # per_entry_targets mode.
    bep_after_move: float = 0.0

    # Multi-entry scale-out exit (research; all default off so DEFAULT_CONFIG and
    # the validated TRAILING-0.5 contract are byte-identical). When ANY of these is
    # set, the scale-out stop ladder (initial SL -> BEP+buffer -> trailing) replaces
    # the legacy lock_after_tp1/2 stop levels for that run.
    scale_out_at_tp1: bool = False          # at TP1 touch, close the worst open leg (furthest from signal SL)
    scale_out_at_tp2: bool = False          # at TP2 touch, close the worst remaining open leg
    bep_after_tp1: bool = False             # at TP1, move remaining legs' stop to entry +/- bep_buffer
    bep_buffer: float = 0.0                 # profit locked beyond entry (price units); use >=0.40 for live placement
    trailing_close_after_stage: int = 0     # trailing-close engages only at/after this stage (0=from open)
    runner_no_final_cap: bool = False       # True => trailing remainder pure-trails (no force-close at final target)

    # Optional trailing behaviour. 0.0 disables each feature. Set per run via the
    # --trailing-open-distance / --trailing-close-distance CLI flags.
    trailing_open_distance: float = 0.0
    trailing_close_distance: float = 0.0

    # Optional trend-following runner. When enabled and a TP3 trade is already
    # profitable, the strategy can keep it open while EMA trend agrees and protect
    # it with an ATR trailing stop. Disabled by default; enable per run with
    # --trend-runner.
    trend_runner_enabled: bool = False
    trend_runner_ema_fast: int = 21
    trend_runner_ema_slow: int = 55
    trend_runner_atr_period: int = 14
    trend_runner_atr_multiplier: float = 3.0
    trend_runner_override_max_hold: bool = True

    # Delayed stop-lock timing. 0 keeps the standard behavior: TP1/TP2 lock is
    # applied right after the target-touch candle is processed.
    tp1_lock_delay_minutes: int = 0
    tp2_lock_delay_minutes: int = 0

    # Profit-lock model:
    # - "tp_levels": after TP1/TP2 lock stops to the configured TP levels.
    # - "bep_plus_half_tp1": research rule for BEP and partial TP1 locking.
    profit_lock_mode: str = "tp_levels"
    bep_trigger_distance: float = 3.0
    tp1_lock_fraction: float = 0.5
    tp2_lock_target: str = "TP1"            # "TP1" | "TP2"
    runner_after_tp3: bool = False
    tp3_lock_target: str = "TP2"


DEFAULT_CONFIG = StrategyConfig()