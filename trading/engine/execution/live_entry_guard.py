"""Live entry-validation guard for the `auto` executor (LIVE-ONLY).

This exists because a fast trailing-open strategy restarted late can revive STALE
signals: the executor places a trailing-open STOP off a signal whose original
market context is long gone, the broker force-fills it at the collapsed live
price, and the tight ``trailing-close`` immediately stops it out -- a structurally
losing open-then-instant-close micro-trade (the 2026-07-01 TSL18 incident: 39 BUY
legs from ~00:59-02:40 signals revived at 03:31 when price had fallen from ~4030
to ~4012, all closed within ~15s by the 0.5 trailing-close for -$3,062).

The guard answers ONE question before any live placement: *is this signal still a
valid entry right now?* -- separate from exit management, which only runs after a
valid trade is open. It NEVER modifies geometry/lot/SL/TP; it only returns a
skip reason (or None to place).

LIVE-ONLY and DEFAULT-OFF: it is built from `auto`/`auto_explicit` runtime flags,
not from ``StrategyConfig``, so backtests (`run_backtest`, the tick backtest) and
the live/backtest parity contract are completely untouched -- ``maybe`` returns
``None`` unless a flag is set, and the caller then does zero extra work.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class LiveEntryGuard:
    """Stateless validator. All thresholds default 0/off; build via ``maybe``."""

    max_age_minutes: int = 0
    min_rr: float = 0.0
    min_reward_distance: float = 0.0
    max_spread_fraction_of_risk: float = 0.0
    # Immediate-close guard inputs (live broker friction):
    trailing_close_distance: float = 0.0
    freeze_distance: float = 0.0  # broker stops/freeze level in price units

    @property
    def enabled(self) -> bool:
        return (self.max_age_minutes > 0 or self.min_rr > 0.0
                or self.min_reward_distance > 0.0
                or self.max_spread_fraction_of_risk > 0.0)

    @classmethod
    def maybe(cls, *, max_age_minutes: int = 0, min_rr: float = 0.0,
              min_reward_distance: float = 0.0,
              max_spread_fraction_of_risk: float = 0.0,
              trailing_close_distance: float = 0.0,
              freeze_distance: float = 0.0) -> "LiveEntryGuard | None":
        """Return a guard only if at least one threshold is enabled, else None
        (so the caller skips all guard work and the live path is unchanged)."""
        g = cls(int(max_age_minutes or 0), float(min_rr or 0.0),
                float(min_reward_distance or 0.0),
                float(max_spread_fraction_of_risk or 0.0),
                float(trailing_close_distance or 0.0), float(freeze_distance or 0.0))
        return g if g.enabled else None

    # -- the one decision ------------------------------------------------------
    def check(self, *, side: str, planned_entry: float, effective_sl: float,
              original_sl: float, tp1: float, final_target: float,
              age_minutes: float, bid: float, ask: float,
              sl_hit_after: bool = False, target_hit_after: bool = False) -> str | None:
        """Return a skip reason (or None to place).

        ``side`` is "BUY"/"SELL". ``planned_entry`` is the price the order would
        actually fill at (for a trailing-live-entry that is the current live
        price, since the STOP force-fills at market once its trigger is crossed).
        ``original_sl`` is the SIGNAL's posted SL (the invalidation level used for
        the price-context guard); ``effective_sl`` is the per-leg stop used for
        risk. ``sl_hit_after`` / ``target_hit_after`` are precomputed from a chart
        scan of (signal_time, now] by the caller (kept out of here so the guard is
        pure + trivially testable). Order: resolved -> age -> price-context -> RR
        -> friction -> immediate-close."""
        buy = side.upper() == "BUY"
        # current marketable price for this side (BUY fills at ask, SELL at bid)
        cur = ask if buy else bid
        spread = max(0.0, ask - bid)

        # 1. Already resolved historically (SL or target touched after the signal).
        if sl_hit_after:
            return "skipped stale signal: original SL already touched before live placement"
        if target_hit_after:
            return "skipped resolved signal: TP/final target already reached before live placement"

        # 2. Live age.
        if self.max_age_minutes > 0 and age_minutes > self.max_age_minutes:
            return (f"skipped stale signal: live age {age_minutes:.0f}min exceeds "
                    f"--max-live-signal-age-minutes {self.max_age_minutes}")

        # 3. Price context: current price already through the original SL, or
        #    already past the final target -> the entry context is gone.
        if buy:
            if cur <= original_sl:
                return ("skipped stale trailing-live-entry: current Ask is at/through "
                        f"the original SL ({cur:.2f} <= {original_sl:.2f})")
            if cur >= final_target:
                return ("skipped resolved signal: current price already past the final "
                        f"target ({cur:.2f} >= {final_target:.2f})")
        else:
            if cur >= original_sl:
                return ("skipped stale trailing-live-entry: current Bid is at/through "
                        f"the original SL ({cur:.2f} >= {original_sl:.2f})")
            if cur <= final_target:
                return ("skipped resolved signal: current price already past the final "
                        f"target ({cur:.2f} <= {final_target:.2f})")

        # 4. Risk/reward at the price the order would actually fill.
        if buy:
            risk = planned_entry - effective_sl
            reward_final = final_target - planned_entry
            reward_tp1 = tp1 - planned_entry
        else:
            risk = effective_sl - planned_entry
            reward_final = planned_entry - final_target
            reward_tp1 = planned_entry - tp1
        if risk <= 0:
            return f"skipped invalid entry: planned entry already beyond SL (risk {risk:.2f} <= 0)"
        if reward_final <= 0:
            return f"skipped invalid entry: planned entry already beyond final target (reward {reward_final:.2f} <= 0)"
        if self.min_rr > 0.0 and (reward_final / risk) < self.min_rr:
            return (f"skipped low live RR: reward/risk {reward_final / risk:.2f} < "
                    f"--min-live-entry-rr {self.min_rr:.2f}")
        if self.min_reward_distance > 0.0 and reward_tp1 < self.min_reward_distance:
            return (f"skipped thin live reward: TP1 reward {reward_tp1:.2f} < "
                    f"--min-live-entry-reward-distance {self.min_reward_distance:.2f}")

        # 5. Spread/friction vs risk.
        if self.max_spread_fraction_of_risk > 0.0 and spread > self.max_spread_fraction_of_risk * risk:
            return (f"skipped friction: spread {spread:.2f} > "
                    f"{self.max_spread_fraction_of_risk:.2f} x risk {risk:.2f}")

        # 6. Immediate-close guard: a trailing-close stop sitting inside normal
        #    spread + broker freeze distance would fire on the first adverse tick,
        #    closing the trade seconds after it opens (the structural micro-trade).
        if self.trailing_close_distance > 0.0 and self.trailing_close_distance <= spread + self.freeze_distance:
            return (f"skipped immediate-close risk: trailing-close {self.trailing_close_distance:.2f} "
                    f"<= spread {spread:.2f} + freeze {self.freeze_distance:.2f}")
        return None
