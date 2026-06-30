"""TSL18 collision policies (research/backtest layer; default = current behavior).

TSL18 is a trend-pullback scalper, so in a fast move it can place signals that
*collide* with positions it already holds:

1. **Opposite-side collision** -- e.g. a BUY at 4750 while an earlier SELL is
   still open (or a SELL at 4950 while a BUY is open). The two sides hedge each
   other; the account carries both directions at once.
2. **Same-side overlap** -- e.g. BUY 4700, then BUY 4699, then BUY 4698 within a
   few minutes: a *cluster* of near-identical entries that stacks risk into one
   spot.

This module is a stateful, deterministic decision layer (mirroring
``DeploymentGate``) that, driven in feed (chronological) order, decides what to
do with each NEW signal given the signals still ACTIVE at its arrival. It only
ever REJECTS, DOWNSIZES, or BANKS/REDUCES an existing side -- it never invents a
trade or moves a stop/target.

It is **default OFF**: the baseline policies (``opposite_signal_policy
="allow_hedge"`` + ``same_side_overlap_policy="allow_all"``) make zero
interventions, so ``CollisionPolicy.maybe`` returns ``None`` and the caller does
zero extra work -- backtest parity is byte-identical. This is a RESEARCH/backtest
layer: the live executor never auto-closes/flips an existing position on a
collision (that irreversible action needs separate, demo-validated wiring); the
old-side banked P&L is modeled in the backtest only. See
``docs/TSL18_COLLISION_POLICIES.md``.

Opposite-side policies (``opposite_signal_policy``):
  * ``allow_hedge``        -- baseline: keep both sides (current behavior).
  * ``reject_opposite``    -- reject the new opposite signal while an opposite
                              signal is active.
  * ``profit_bank_rearm``  -- if the active opposite side is profitable by
                              ``opposite_profit_threshold_r`` R, BANK it (close
                              the profitable side), allow the new signal, and keep
                              the banked side rearmable ONLY at its original
                              planned entry or better (never a chase). If the
                              opposite side is not profitable enough, fall back to
                              ``allow_hedge`` (banking a loss is never forced).
  * ``close_then_flip``    -- close the old side and open the new side.
  * ``reduce_then_hedge``  -- keep both but reduce exposure: close
                              ``1 - hedge_lot_fraction`` of the old side and size
                              the new hedge at ``hedge_lot_fraction``.

Same-side policies (``same_side_overlap_policy``):
  * ``allow_all``                  -- baseline: take every overlap.
  * ``reject_overlap``             -- reject an overlapping same-side signal.
  * ``scale_in_better_entry_only`` -- BUY only if the new entry is LOWER by at
                                      least ``same_side_cluster_entry_gap``; SELL
                                      only if HIGHER by that gap; and only if the
                                      cluster's total risk stays within
                                      ``max_cluster_risk_multiple`` x the cluster
                                      anchor's risk.
  * ``scale_in_fixed_risk``        -- allow the scale-in but DOWNSIZE it so the
                                      cluster's total risk <= anchor risk x
                                      ``max_cluster_risk_multiple``; reject if the
                                      downsized lot would fall below the min lot.

Reporting (per accepted signal row + the run summary): ``collision_type``,
``collision_policy``, ``collision_policy_action``, ``cluster_id``,
``cluster_risk_before/after``, ``opposite_exposure_before/after``; summary
counters ``opposite_collisions_total/allowed/rejected/flipped/
profit_bank_rearmed``, ``same_side_clusters_total/accepted/rejected/downsized``,
``max_same_side_cluster_risk``, ``max_opposite_exposure``, ``collision_policy_pnl``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from ..core.config import CONTRACT_SIZE_OZ, StrategyConfig


OPPOSITE_POLICIES = (
    "allow_hedge", "reject_opposite", "profit_bank_rearm",
    "close_then_flip", "reduce_then_hedge",
)
SAME_SIDE_POLICIES = (
    "allow_all", "reject_overlap", "scale_in_better_entry_only",
    "scale_in_fixed_risk",
)

# Entry statuses that mean the engine/broker FINISHED a leg (an SL/TP hit, a
# locked exit, a time exit, a trailing stop, or a break-even close). A signal
# with any such leg is TERMINAL -- it can never be re-armed/re-opened. Mirrors
# the live "already traded = the backtest/broker finished it" rule and
# ``core.positions.TERMINAL`` (minus NO_FILL, which never traded, and PENDING/
# OPEN, which are still live).
TERMINAL_STATUSES = frozenset({
    "SL", "BEP", "LOCK_HALF_TP1", "LOCK_TP1", "LOCK_TP2",
    "TP1", "TP2", "TP3", "TIME_EXIT", "TRAILING_STOP",
})


def status_is_terminal(status: str | None) -> bool:
    """True when a leg status means the engine/broker finished it (so the signal
    must not be re-armed). NO_FILL / PENDING / OPEN are not terminal."""
    return str(status or "") in TERMINAL_STATUSES


def can_rearm(side: str, original_planned_entry: float, candidate_price: float,
              *, terminal: bool) -> bool:
    """Whether a ``profit_bank_rearm``-banked signal may re-enter at
    ``candidate_price``.

    Two hard rules, identical to the live no-chase / terminal-SL contract:

    1. **No chase.** A BUY re-arms ONLY at its ORIGINAL planned entry or BETTER
       (``candidate_price <= original_planned_entry``); a SELL only at its
       original entry or better (``candidate_price >= original_planned_entry``).
       The comparison is against the ORIGINAL planned entry, never a filled
       price, so a banked side never re-enters chasing a worse price.
    2. **Terminal stays terminal.** Once the signal hit a system terminal (its
       SL/TP, a locked exit, an engine close) OR its original SL was touched, it
       is finished -- ``terminal=True`` => never re-arm.
    """
    if terminal:
        return False
    if side == "BUY":
        return candidate_price <= original_planned_entry
    return candidate_price >= original_planned_entry


@dataclass
class _ActiveSignal:
    """One accepted signal still ACTIVE (open or resting) during the run."""
    signal_key: str
    side: str
    start: datetime
    end: datetime
    # original planned best entry (BUY=range_high / SELL=range_low) -- the
    # rearm floor for profit_bank_rearm (re-enter only here or better).
    planned_entry: float
    sl: float
    risk: float                 # planned full-ladder risk $ at the taken lots
    cluster_id: str             # same-side cluster membership ("" for none yet)
    terminal: bool              # the engine/broker finished it (SL/TP/engine close)
    rearmable: bool = False     # set when profit_bank_rearm banks it
    legs: list[dict] = field(default_factory=list)  # {fill_time, exit_time, entry_price, lot}

    def open_lots_at(self, t: datetime) -> float:
        """Lots this signal holds OPEN at instant ``t`` (filled by t, not yet
        exited at t)."""
        tot = 0.0
        for leg in self.legs:
            ft, xt = leg.get("fill_time"), leg.get("exit_time")
            if ft is not None and ft <= t and (xt is None or xt > t):
                tot += float(leg.get("lot") or 0.0)
        return tot

    def mark_profit(self, t: datetime, price_at) -> float:
        """Realized-to-``t`` P&L (legs closed before t) + mark-to-market of legs
        still open at t (priced at ``price_at(t)``), in dollars. Returns 0 when
        ``price_at`` is None (no price source -> cannot MtM the open legs)."""
        direction = 1.0 if self.side == "BUY" else -1.0
        pnl = 0.0
        price = None if price_at is None else price_at(t)
        for leg in self.legs:
            ft, xt = leg.get("fill_time"), leg.get("exit_time")
            if ft is None or ft > t:
                continue
            lot = float(leg.get("lot") or 0.0)
            if xt is not None and xt <= t:
                # closed before t: use its realized trading P&L
                pnl += float(leg.get("trading_pnl") or 0.0)
            elif price is not None:
                pnl += lot * CONTRACT_SIZE_OZ * (price - float(leg["entry_price"])) * direction
        return pnl


@dataclass
class CollisionDecision:
    """The outcome of evaluating one new signal against the active set."""
    accept: bool = True
    collision_type: str = ""           # "" | "opposite" | "same_side"
    collision_policy: str = ""         # the policy that fired ("" when none)
    action: str = "allow"              # allow | reject_opposite | profit_bank_rearm |
                                       # close_then_flip | reduce_then_hedge |
                                       # reject_overlap | scale_in_allowed |
                                       # scale_in_rejected | scale_in_downsized
    lot_scale: float = 1.0             # multiply the new signal's lots by this
    cluster_id: str = ""
    cluster_risk_before: float = 0.0
    cluster_risk_after: float = 0.0
    opposite_exposure_before: float = 0.0
    opposite_exposure_after: float = 0.0
    old_side_pnl_delta: float = 0.0    # banked/foregone $ from closing/reducing old side
    reason: str = ""                   # exclusion reason when accept is False

    def as_row_fields(self) -> dict:
        """The per-signal reporting fields stamped onto an accepted signal's row
        + entry rows (only present when a collision policy is active)."""
        return {
            "collision_type": self.collision_type,
            "collision_policy": self.collision_policy,
            "collision_policy_action": self.action,
            "cluster_id": self.cluster_id,
            "cluster_risk_before": self.cluster_risk_before,
            "cluster_risk_after": self.cluster_risk_after,
            "opposite_exposure_before": self.opposite_exposure_before,
            "opposite_exposure_after": self.opposite_exposure_after,
        }


@dataclass
class CollisionPolicy:
    """Stateful, deterministic collision-resolution layer. Drive it in feed
    order: ``decide`` before accepting a signal (it reads + mutates the active
    set for old-side actions), then ``register`` after the signal is committed."""

    config: StrategyConfig
    contract_size: float = CONTRACT_SIZE_OZ

    counters: dict[str, float] = field(default_factory=lambda: {
        "opposite_collisions_total": 0, "opposite_collisions_allowed": 0,
        "opposite_collisions_rejected": 0, "opposite_collisions_flipped": 0,
        "opposite_collisions_profit_bank_rearmed": 0,
        "same_side_clusters_total": 0, "same_side_clusters_accepted": 0,
        "same_side_clusters_rejected": 0, "same_side_clusters_downsized": 0,
        "max_same_side_cluster_risk": 0.0, "max_opposite_exposure": 0.0,
        "collision_policy_pnl": 0.0,
    })

    _active: list[_ActiveSignal] = field(default_factory=list)
    _cluster_seq: int = 0

    def __post_init__(self) -> None:
        c = self.config
        self.opposite_policy = str(getattr(c, "opposite_signal_policy", "allow_hedge")
                                   or "allow_hedge")
        self.same_side_policy = str(getattr(c, "same_side_overlap_policy", "allow_all")
                                    or "allow_all")
        self.cluster_window = int(getattr(c, "same_side_cluster_window_minutes", 30) or 0)
        self.entry_gap = float(getattr(c, "same_side_cluster_entry_gap", 0.0) or 0.0)
        self.sl_gap = float(getattr(c, "same_side_cluster_sl_gap", 0.0) or 0.0)
        self.max_cluster_mult = float(getattr(c, "max_cluster_risk_multiple", 1.0) or 0.0)
        self.profit_threshold_r = float(getattr(c, "opposite_profit_threshold_r", 0.0) or 0.0)
        self.hedge_fraction = float(getattr(c, "hedge_lot_fraction", 0.5) or 0.0)
        self.min_lot = float(getattr(c, "minimum_lot", 0.01) or 0.0)
        self.max_hold = int(getattr(c, "max_hold_minutes", 0) or 0)
        self.pending_expiry = int(getattr(c, "pending_expiry_minutes", 0) or 0)

    # -- construction ---------------------------------------------------------
    @property
    def enabled(self) -> bool:
        return (self.opposite_policy != "allow_hedge"
                or self.same_side_policy != "allow_all")

    @classmethod
    def maybe(cls, config: StrategyConfig,
              contract_size: float = CONTRACT_SIZE_OZ) -> "CollisionPolicy | None":
        """Return a policy only when a non-baseline policy is set, else None (so
        the caller skips all collision work and parity is byte-identical)."""
        p = cls(config, contract_size)
        return p if p.enabled else None

    # -- helpers --------------------------------------------------------------
    def _prune(self, t: datetime) -> None:
        # signals are chronological, so a window that ended at/before t can never
        # collide with t or later -- drop it (keeps the active scan O(open)).
        self._active = [a for a in self._active if a.end > t]

    @staticmethod
    def signal_risk(entry_rows: list[dict], contract_size: float = CONTRACT_SIZE_OZ) -> float:
        """Planned full-ladder risk $ of a signal = sum over its planned legs of
        |entry - effective_SL| x lot x contract. Uses the PLANNED levels/lots
        (independent of fill), so it is the pre-trade exposure the cluster cap
        budgets against."""
        risk = 0.0
        for er in entry_rows:
            ep, sl, lot = er.get("entry_price"), er.get("effective_SL"), er.get("lot")
            if ep is None or sl is None or lot is None:
                continue
            risk += abs(float(ep) - float(sl)) * float(lot) * contract_size
        return risk

    @staticmethod
    def _best_entry(side: str, entry_rows: list[dict]) -> float | None:
        """The signal's best planned entry: highest for a BUY, lowest for a SELL
        (the entry closest to where price already is)."""
        prices = [float(er["entry_price"]) for er in entry_rows
                  if er.get("entry_price") is not None]
        if not prices:
            return None
        return max(prices) if side == "BUY" else min(prices)

    @staticmethod
    def _signal_terminal(entry_rows: list[dict], side: str, original_sl: float) -> bool:
        """A signal is terminal once any leg hit a system terminal OR its original
        SL was touched (exit at/through the original SL)."""
        for er in entry_rows:
            if status_is_terminal(er.get("entry_status")):
                return True
            xp = er.get("exit_price")
            if xp is not None and original_sl is not None:
                xp = float(xp)
                if side == "BUY" and xp <= float(original_sl):
                    return True
                if side == "SELL" and xp >= float(original_sl):
                    return True
        return False

    # -- the one-shot decision ------------------------------------------------
    def decide(self, sig, entry_rows: list[dict], *, price_at=None) -> CollisionDecision:
        """Decide what to do with a NEW (already-built) signal given the active
        set. Reads the currently-active signals; on an accepted old-side action
        (flip/bank/reduce) it MUTATES the active set + accumulates the banked P&L.
        ``entry_rows`` are the new signal's built per-entry rows (planned levels +
        lots). ``price_at`` is an optional ``callable(dt) -> price`` used to mark
        the old side for profit / banked-P&L; None disables the P&L model (the
        decision is still made). Call ``register`` afterwards on an accept."""
        t = sig.signal_time_chart
        self._prune(t)
        new_risk = self.signal_risk(entry_rows, self.contract_size)

        opp = [a for a in self._active if a.side != sig.side]
        same = [a for a in self._active if a.side == sig.side
                and (self.cluster_window <= 0
                     or (t - a.start) <= timedelta(minutes=self.cluster_window))]

        dec = CollisionDecision()

        # --- opposite-side collision -----------------------------------------
        old_side_targets: list[tuple[_ActiveSignal, float]] = []  # (signal, close_fraction)
        if opp and self.opposite_policy != "allow_hedge":
            opp_exposure = sum(a.open_lots_at(t) for a in opp)
            dec.collision_type = "opposite"
            dec.collision_policy = self.opposite_policy
            dec.opposite_exposure_before = opp_exposure
            dec.opposite_exposure_after = opp_exposure
            self.counters["opposite_collisions_total"] += 1
            self.counters["max_opposite_exposure"] = max(
                self.counters["max_opposite_exposure"], opp_exposure)

            if self.opposite_policy == "reject_opposite":
                self.counters["opposite_collisions_rejected"] += 1
                dec.accept = False
                dec.action = "reject_opposite"
                dec.reason = "collision_reject_opposite"
                return dec

            if self.opposite_policy == "close_then_flip":
                dec.action = "close_then_flip"
                old_side_targets = [(a, 1.0) for a in opp]
                dec.opposite_exposure_after = 0.0
                self.counters["opposite_collisions_flipped"] += 1

            elif self.opposite_policy == "reduce_then_hedge":
                dec.action = "reduce_then_hedge"
                close_frac = max(0.0, 1.0 - self.hedge_fraction)
                old_side_targets = [(a, close_frac) for a in opp]
                dec.opposite_exposure_after = opp_exposure * self.hedge_fraction
                dec.lot_scale = self.hedge_fraction      # the new hedge is downsized
                self.counters["opposite_collisions_allowed"] += 1

            elif self.opposite_policy == "profit_bank_rearm":
                banked = [a for a in opp
                          if a.risk > 0
                          and (a.mark_profit(t, price_at) / a.risk) >= self.profit_threshold_r]
                if banked:
                    dec.action = "profit_bank_rearm"
                    old_side_targets = [(a, 1.0) for a in banked]
                    dec.opposite_exposure_after = opp_exposure - sum(
                        a.open_lots_at(t) for a in banked)
                    self.counters["opposite_collisions_profit_bank_rearmed"] += 1
                else:
                    # not profitable enough -> hedge (never bank a loss)
                    dec.action = "allow"
                    self.counters["opposite_collisions_allowed"] += 1

        # --- same-side overlap -----------------------------------------------
        if dec.accept and same and self.same_side_policy != "allow_all":
            if not dec.collision_type:
                dec.collision_type = "same_side"
                dec.collision_policy = self.same_side_policy
            cluster_risk_before = sum(a.risk for a in same)
            anchor = min(same, key=lambda a: a.start)        # earliest member
            cap = anchor.risk * self.max_cluster_mult
            dec.cluster_id = anchor.cluster_id
            dec.cluster_risk_before = cluster_risk_before
            dec.cluster_risk_after = cluster_risk_before     # default (reject)
            self.counters["same_side_clusters_total"] += 1

            if self.same_side_policy == "reject_overlap":
                self._reject_same(dec, "reject_overlap", "collision_reject_overlap")
                return dec

            if self.same_side_policy == "scale_in_better_entry_only":
                new_entry = self._best_entry(sig.side, entry_rows)
                best_existing = self._cluster_best_entry(sig.side, same)
                better = self._is_better_entry(sig.side, new_entry, best_existing)
                if not better:
                    self._reject_same(dec, "scale_in_rejected",
                                      "collision_scale_in_entry_not_better")
                    return dec
                if cap > 0 and cluster_risk_before + new_risk > cap:
                    self._reject_same(dec, "scale_in_rejected",
                                      "collision_scale_in_risk_cap")
                    return dec
                dec.action = "scale_in_allowed"
                dec.cluster_risk_after = cluster_risk_before + new_risk
                self.counters["same_side_clusters_accepted"] += 1

            elif self.same_side_policy == "scale_in_fixed_risk":
                allowed = max(0.0, cap - cluster_risk_before)
                if new_risk <= allowed or new_risk <= 0:
                    dec.action = "scale_in_allowed"
                    dec.cluster_risk_after = cluster_risk_before + new_risk
                    self.counters["same_side_clusters_accepted"] += 1
                else:
                    scale = allowed / new_risk
                    if not self._downsize_keeps_min_lot(entry_rows, scale):
                        self._reject_same(dec, "scale_in_rejected",
                                          "collision_scale_in_below_min_lot")
                        return dec
                    dec.lot_scale *= scale
                    dec.action = "scale_in_downsized"
                    dec.cluster_risk_after = cluster_risk_before + new_risk * scale
                    self.counters["same_side_clusters_downsized"] += 1

            self.counters["max_same_side_cluster_risk"] = max(
                self.counters["max_same_side_cluster_risk"], dec.cluster_risk_after)

        # --- apply accepted old-side actions (close/bank/reduce) -------------
        if old_side_targets:
            self._apply_old_side(dec, t, old_side_targets, price_at)
        return dec

    # -- same-side reject bookkeeping -----------------------------------------
    def _reject_same(self, dec: CollisionDecision, action: str, reason: str) -> None:
        dec.accept = False
        dec.action = action
        dec.reason = reason
        dec.cluster_risk_after = dec.cluster_risk_before
        self.counters["same_side_clusters_rejected"] += 1

    @staticmethod
    def _cluster_best_entry(side: str, members: list[_ActiveSignal]) -> float | None:
        entries = [a.planned_entry for a in members if a.planned_entry is not None]
        if not entries:
            return None
        # the cluster's best resting entry so far (closest to price)
        return max(entries) if side == "BUY" else min(entries)

    def _is_better_entry(self, side: str, new_entry: float | None,
                         best_existing: float | None) -> bool:
        """A new BUY entry must be LOWER than the cluster's best by >= entry_gap;
        a new SELL entry must be HIGHER by >= entry_gap."""
        if new_entry is None or best_existing is None:
            return False
        if side == "BUY":
            return new_entry <= best_existing - self.entry_gap
        return new_entry >= best_existing + self.entry_gap

    def _downsize_keeps_min_lot(self, entry_rows: list[dict], scale: float) -> bool:
        """A downsize is feasible only if the SMALLEST planned leg, scaled, is
        still >= the broker min lot (else the leg can't be placed)."""
        lots = [float(er["lot"]) for er in entry_rows if er.get("lot")]
        if not lots or scale <= 0:
            return False
        return min(lots) * scale >= self.min_lot

    def _apply_old_side(self, dec: CollisionDecision, t: datetime,
                        targets: list[tuple[_ActiveSignal, float]], price_at) -> None:
        """Close (or partially close) the old side at ``t``: book the banked vs
        natural P&L delta of the legs still open at t, and shrink/retire the old
        active windows so they no longer collide."""
        direction_for = {"BUY": 1.0, "SELL": -1.0}
        price = None if price_at is None else price_at(t)
        total_delta = 0.0
        for active, frac in targets:
            if frac <= 0:
                continue
            d = direction_for.get(active.side, 0.0)
            for leg in active.legs:
                ft, xt = leg.get("fill_time"), leg.get("exit_time")
                if ft is None or ft > t or (xt is not None and xt <= t):
                    continue   # not open at t -> nothing to close early
                lot = float(leg.get("lot") or 0.0) * frac
                if price is not None:
                    close_now = lot * self.contract_size * (price - float(leg["entry_price"])) * d
                    natural = float(leg.get("trading_pnl") or 0.0) * frac
                    total_delta += close_now - natural
                # shrink the leg's remaining open lot (reduce) or retire it (close)
                leg["lot"] = float(leg.get("lot") or 0.0) * (1.0 - frac)
            active.risk *= (1.0 - frac)
            if frac >= 1.0:
                active.end = t                       # fully closed now
                active.terminal = (dec.action != "profit_bank_rearm")
                active.rearmable = (dec.action == "profit_bank_rearm")
        dec.old_side_pnl_delta = total_delta
        self.counters["collision_policy_pnl"] += total_delta

    # -- register an ACCEPTED signal ------------------------------------------
    def register(self, sig, built: dict, dec: CollisionDecision) -> None:
        """Record an accepted (post-scale) signal in the active set. Call after
        the signal is committed to the equity curve. Assigns the signal a cluster
        id (inherited from any same-side cluster it joined, else a fresh one)."""
        entry_rows = built["entry_rows"]
        t = sig.signal_time_chart
        # window end: latest leg exit (or fill + max_hold), else arrival + expiry
        hold = timedelta(minutes=self.max_hold)
        leg_ends = [er["exit_time"] if er.get("exit_time") else er["fill_time"] + hold
                    for er in entry_rows if er.get("fill_time")]
        end = max(leg_ends) if leg_ends else t + timedelta(minutes=self.pending_expiry)
        end = max(end, t)

        same = [a for a in self._active if a.side == sig.side
                and (self.cluster_window <= 0
                     or (t - a.start) <= timedelta(minutes=self.cluster_window))]
        if same:
            cluster_id = min(same, key=lambda a: a.start).cluster_id
        else:
            self._cluster_seq += 1
            cluster_id = f"C{self._cluster_seq}"

        planned_entry = self._best_entry(sig.side, entry_rows)
        if planned_entry is None:
            planned_entry = (float(sig.range_high) if sig.side == "BUY"
                             else float(sig.range_low))
        legs = [{"fill_time": er.get("fill_time"), "exit_time": er.get("exit_time"),
                 "entry_price": er.get("entry_price"), "lot": er.get("lot"),
                 "trading_pnl": er.get("trading_pnl")}
                for er in entry_rows]
        self._active.append(_ActiveSignal(
            signal_key=sig.signal_key, side=sig.side, start=t, end=end,
            planned_entry=float(planned_entry), sl=float(getattr(sig, "sl", 0.0) or 0.0),
            risk=self.signal_risk(entry_rows, self.contract_size),
            cluster_id=cluster_id,
            terminal=self._signal_terminal(entry_rows, sig.side,
                                           float(getattr(sig, "sl", 0.0) or 0.0)),
            legs=legs))

    # -- reporting ------------------------------------------------------------
    def summary(self) -> dict:
        c = self.counters
        return {
            "opposite_collisions_total": int(c["opposite_collisions_total"]),
            "opposite_collisions_allowed": int(c["opposite_collisions_allowed"]),
            "opposite_collisions_rejected": int(c["opposite_collisions_rejected"]),
            "opposite_collisions_flipped": int(c["opposite_collisions_flipped"]),
            "opposite_collisions_profit_bank_rearmed":
                int(c["opposite_collisions_profit_bank_rearmed"]),
            "same_side_clusters_total": int(c["same_side_clusters_total"]),
            "same_side_clusters_accepted": int(c["same_side_clusters_accepted"]),
            "same_side_clusters_rejected": int(c["same_side_clusters_rejected"]),
            "same_side_clusters_downsized": int(c["same_side_clusters_downsized"]),
            "max_same_side_cluster_risk": float(c["max_same_side_cluster_risk"]),
            "max_opposite_exposure": float(c["max_opposite_exposure"]),
            "collision_policy_pnl": float(c["collision_policy_pnl"]),
            "config": {
                "opposite_signal_policy": self.opposite_policy,
                "same_side_overlap_policy": self.same_side_policy,
                "same_side_cluster_window_minutes": self.cluster_window,
                "same_side_cluster_entry_gap": self.entry_gap,
                "same_side_cluster_sl_gap": self.sl_gap,
                "max_cluster_risk_multiple": self.max_cluster_mult,
                "opposite_profit_threshold_r": self.profit_threshold_r,
                "hedge_lot_fraction": self.hedge_fraction,
            },
        }
