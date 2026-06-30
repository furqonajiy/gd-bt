"""Strategy configuration.

Default strategy: bonus-aware provider execution contract.

The defaults in this branch are the validated DD40-compatible provider contract.
Optional trailing-open, trailing-close, and trend-runner settings are available
for research/live parity but default to disabled, so existing backtests and Auto
runs keep their current behaviour unless explicitly enabled.

Research toggles are set explicitly per run via CLI flags (e.g.
``--trailing-open-distance``, ``--trailing-close-distance``, ``--trend-runner``;
see ``_add_strategy_overrides`` in ``cli_impl.py``). They are deliberately NOT read
from the environment, so ``DEFAULT_CONFIG`` is always the DD40 contract regardless
of shell state.
"""
from __future__ import annotations

from dataclasses import dataclass


CONTRACT_SIZE_OZ = 100.0       # 1.0 lot XAUUSD = 100 oz; 0.5 lot = $50 per $1 move
POINT_VALUE = 0.01             # 1 spread point = $0.01
CHART_TIMEZONE_OFFSET = 3      # SUMMER reference only; chart is EET/EEST (see core/chart_tz.py)


@dataclass(frozen=True)
class StrategyConfig:
    initial_capital: float = 50_000.0

    sizing_mode: str = "risk"              # "fixed" | "risk"
    lot_per_entry: float = 0.5
    risk_per_signal: float = 0.05575
    minimum_lot: float = 0.01
    maximum_lot: float = 0.0              # 0.0 = no cap; >0 clamps per-entry lot
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

    # For RUN legs (per_entry_targets mode): the TP level whose touch ENGAGES the
    # trailing stop, which then trails by trailing_close_distance. The trail never
    # runs from entry -- only from this level onward. "TP1" | "TP2" | "TP3".
    runner_trail_from: str = "TP3"

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

    # Live-execution throttle for the executor-owned trailing-close stop: send
    # the MT5 SLTP modify only once the recomputed stop improves on the broker's
    # current SL by at least this many price units (the first protective set
    # always goes out). 0.0 = legacy, every improvement is sent. Backtests
    # ignore it -- the engine still trails continuously -- so the live SL can
    # lag the modeled stop by up to this amount; that lag is the accepted cost
    # of far fewer order_send calls on dense trailing configs. MT5's own
    # terminal-side Trailing Stop is NOT an alternative: the Python API cannot
    # set it, and it would fight the executor's SLTP ownership.
    trailing_close_min_step: float = 0.0

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

    # Backtest realism: a *locked* protective stop (LOCK_TP1/LOCK_TP2) is a stop
    # order that fills at market when price retraces through it, so live gives back
    # a point or two past the lock level. The backtest idealizes the fill AT the
    # level; set this >0 (price units) to model that give-back so the backtest's
    # locked-winner P&L matches live. 0 keeps the idealized fill (parity default).
    # Backtest-only: live already realizes real slippage at the broker, so this
    # never changes live order placement. Reconciliation on 2026-06-16 measured
    # ~1-2.5 pts on LOCK_TP1 and ~1 pt on LOCK_TP2.
    #
    # `lock_exit_slippage_points` is the UNIFORM knob (same give-back on every
    # lock). The two stage fields below model the measured ASYMMETRY (LOCK_TP1
    # retraces are choppier than LOCK_TP2) and, when EITHER is >0, OVERRIDE the
    # uniform value per stage; when both are 0 the uniform value applies to all
    # locks. All three default 0 (idealized fill, parity preserved). These are the
    # values the SWEEP scores against so parameters are decided on REAL fills
    # (see tools/sweep.base_config_dict); live/decide/DEFAULT_CONFIG stay at 0.
    lock_exit_slippage_points: float = 0.0
    lock_tp1_exit_slippage_points: float = 0.0
    lock_tp2_exit_slippage_points: float = 0.0

    # Signal risk:reward POLICY (backtest/sweep signal preprocessing; default OFF
    # so parity + DEFAULT_CONFIG behavior are unchanged). For provider feeds whose
    # posted TP/SL vary in quality (e.g. Victor), the sweep can FILTER weak setups
    # and/or REWRITE the targets to our own R:R geometry, and decide on real fills.
    # entry_edge = range_high (BUY) / range_low (SELL); base risk = |entry_edge - sl|.
    # `signal_rr_reference` picks whether R:R is measured against that NOMINAL risk
    # or the EFFECTIVE stop the engine uses (nominal x sl_multiplier).
    signal_rr_reference: str = "nominal"   # "nominal" | "effective"
    # FILTER: skip a signal whose TP1 reward:risk is below this (0 = keep all).
    signal_min_rr: float = 0.0
    # REWRITE: when rewrite_tp3_rr > 0, replace TP1/TP2/TP3 with
    # entry_edge +/- rr_k * risk (0 = keep the signal's own TPs).
    rewrite_tp1_rr: float = 0.0
    rewrite_tp2_rr: float = 0.0
    rewrite_tp3_rr: float = 0.0
    # SL SOURCE: "signal" uses the provider's posted SL as the raw risk;
    # "atr" replaces it with OUR generator's geometry -- raw risk = ATR (at the
    # signal's bar, no lookahead) x atr_sl_mult, SL = entry_edge -/+ that. The
    # engine still scales the raw risk by sl_multiplier (a swept dim), and any TP
    # rewrite then uses this ATR risk. Default "signal" -> ATR off (parity).
    sl_source: str = "signal"              # "signal" | "atr"
    atr_period: int = 14
    atr_sl_mult: float = 1.5

    # SMALL-ACCOUNT DEPLOYMENT-SAFETY GATES (backtest + live; all default OFF/0 so
    # parity + DEFAULT_CONFIG behavior are byte-identical). These never change a
    # signal's geometry, lot sizing, SL/TP, or trailing -- they only REJECT/PAUSE
    # signals an under-capitalized account cannot safely take. See
    # docs/SMALL_ACCOUNT_DEPLOYMENT.md. A single shared DeploymentGate
    # (strategy.deployment_gate) enforces them identically in run_backtest, the
    # hybrid tick backtest, and the live executor.
    #
    # RISK-BUDGET gate: reject a signal whose worst-case MIN-LOT risk is too large
    # for current equity. single = max over planned ladder legs of
    # |entry-effective_SL| x minimum_lot x contract; zone = sum over legs. The 0.01
    # lot floor means a small account cannot scale risk down past one min-lot leg,
    # so a wide-stop signal can over-risk -- this gate refuses it. Caps are fractions
    # of equity (0.04 = 4%); 0 disables that cap.
    risk_budget_gate: bool = False
    max_single_entry_risk_pct: float = 0.0   # reject if single-leg min-lot risk > equity x this
    max_zone_risk_pct: float = 0.0           # reject if full-ladder min-lot risk > equity x this
    # DAILY-LOSS circuit breaker: once realized P&L for a feed-zone (source) day
    # reaches -daily_loss_limit_pct x start-of-day equity, stop ACCEPTING new
    # signals for the rest of that day. Already-open positions are NOT force-closed
    # (the engine keeps managing them). Fraction of equity; 0 disables.
    daily_loss_limit_pct: float = 0.0
    # MAX CONCURRENT OPEN SIGNALS: cap how many signal GROUPS (not entries) may be
    # open at once. A signal with up to entry_count legs still counts as ONE. A new
    # signal arriving while >= this many groups are open is rejected. 0 = unlimited.
    max_open_signals: int = 0
    # MAX CONCURRENT OPEN LOTS: cap the TOTAL open volume across ALL positions
    # (every BUY + SELL leg of every open signal) at once -- the ELEV8 broker
    # ceiling is 100 lots total (e.g. 5 open signals share it, ~20 lots each). A
    # new signal whose filled ladder would push total open lots over this is
    # rejected. 0.0 = unlimited (parity). Companion to the per-order maximum_lot.
    max_open_lots: float = 0.0

    # TSL18 COLLISION POLICIES (research/backtest layer; the two policy fields
    # default to the BASELINE so behavior + parity are byte-identical -- a
    # CollisionPolicy is built only when a non-baseline policy is set). TSL18 can
    # place a signal that collides with one it already holds: an OPPOSITE-side
    # hedge (BUY 4750 while a SELL is open) or a SAME-side overlap/cluster
    # (BUY 4700, 4699, 4698). These resolve such collisions; they only REJECT,
    # DOWNSIZE, or BANK/REDUCE an existing side -- never invent a trade or move a
    # stop/target. See strategy.collision_policy + docs/TSL18_COLLISION_POLICIES.md.
    # opposite_signal_policy: allow_hedge (baseline) | reject_opposite |
    #   profit_bank_rearm | close_then_flip | reduce_then_hedge.
    opposite_signal_policy: str = "allow_hedge"
    # same_side_overlap_policy: allow_all (baseline) | reject_overlap |
    #   scale_in_better_entry_only | scale_in_fixed_risk.
    same_side_overlap_policy: str = "allow_all"
    # Same-side cluster definition + caps (only consulted by the same-side policies).
    same_side_cluster_window_minutes: int = 30   # signals within this window cluster
    same_side_cluster_entry_gap: float = 5.0     # min price improvement to scale in
    same_side_cluster_sl_gap: float = 10.0       # reserved: min SL separation in a cluster
    max_cluster_risk_multiple: float = 1.0       # cluster risk <= anchor risk x this
    # Opposite-side knobs.
    opposite_profit_threshold_r: float = 0.5     # bank old side only if >= this R in profit
    hedge_lot_fraction: float = 0.5              # reduce_then_hedge: kept fraction of exposure


DEFAULT_CONFIG = StrategyConfig()


def lock_slippage_points(status: str, config: "StrategyConfig") -> float:
    """Per-stage locked-exit slippage (price units) for a triggered stop whose
    terminal ``status`` is a lock (``LOCK_*``). LOCK_TP2 uses
    ``lock_tp2_exit_slippage_points``; every other lock (LOCK_TP1, LOCK_HALF_TP1)
    uses ``lock_tp1_exit_slippage_points``. When BOTH stage fields are 0 the
    uniform ``lock_exit_slippage_points`` applies to all locks (single-knob /
    back-compat). 0 everywhere -> 0 (idealized fill, parity). Shared by the real
    lifecycle (``core.trailing_positions``) and its diagnostic mirror
    (``strategy.path_analysis``)."""
    s1 = float(getattr(config, "lock_tp1_exit_slippage_points", 0.0) or 0.0)
    s2 = float(getattr(config, "lock_tp2_exit_slippage_points", 0.0) or 0.0)
    if s1 <= 0 and s2 <= 0:
        return float(getattr(config, "lock_exit_slippage_points", 0.0) or 0.0)
    return s2 if str(status).startswith("LOCK_TP2") else s1