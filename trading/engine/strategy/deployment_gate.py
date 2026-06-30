"""Small-account deployment-safety gates (shared by backtest + live).

These exist for ONE reason: the broker 0.01-lot floor. A small account cannot
scale risk below a single minimum-lot leg, so an 8-entry ladder into one zone
can risk far more than the intended per-day budget when the stop is wide. The
gates REJECT or PAUSE signals that an under-capitalized account cannot safely
take. They never change a signal's geometry, lot sizing, SL/TP, or trailing --
the only effect is to drop/defer signals, exactly like a feed filter, but keyed
off live account state (equity, day P&L, open concurrency).

All three gates default OFF (see ``StrategyConfig``); when none is enabled
``DeploymentGate.maybe`` returns ``None`` and the caller does zero extra work, so
backtest parity is byte-identical. The SAME object is used by ``run_backtest``,
the hybrid tick backtest, and the live executor so the gate decision is
identical across all three (the live/backtest parity contract).

Reject reasons (recorded on the excluded signal):
``risk_budget_single`` | ``risk_budget_zone`` | ``daily_loss_breaker`` |
``max_open_signals``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from ..core.config import CONTRACT_SIZE_OZ, StrategyConfig


@dataclass
class _OpenWindow:
    start: datetime
    end: datetime | None  # None => still open at end of data / now


@dataclass
class DeploymentGate:
    """Stateful, deterministic signal-acceptance gate. Drive it in feed
    (chronological) order: ``pre_check`` before replaying/placing a signal,
    ``register`` after it is accepted and (in backtest) its lifecycle is known."""

    config: StrategyConfig
    contract_size: float = CONTRACT_SIZE_OZ

    rejected: dict[str, int] = field(
        default_factory=lambda: {
            "risk_budget_single": 0, "risk_budget_zone": 0,
            "daily_loss_breaker": 0, "max_open_signals": 0,
        })

    _windows: list[_OpenWindow] = field(default_factory=list)
    _day: date | None = None
    _day_start_equity: float | None = None
    _day_pnl: float = 0.0
    _day_blocked: bool = False
    _peak_concurrency: int = 0

    def __post_init__(self) -> None:
        c = self.config
        self.min_lot = float(c.minimum_lot)
        self.rb = bool(getattr(c, "risk_budget_gate", False))
        self.max_single = float(getattr(c, "max_single_entry_risk_pct", 0.0) or 0.0)
        self.max_zone = float(getattr(c, "max_zone_risk_pct", 0.0) or 0.0)
        self.daily_limit = float(getattr(c, "daily_loss_limit_pct", 0.0) or 0.0)
        self.max_open = int(getattr(c, "max_open_signals", 0) or 0)
        self.pending_expiry = int(getattr(c, "pending_expiry_minutes", 0) or 0)

    # -- construction ---------------------------------------------------------
    @property
    def enabled(self) -> bool:
        return self.rb or self.daily_limit > 0 or self.max_open > 0

    @classmethod
    def maybe(cls, config: StrategyConfig,
              contract_size: float = CONTRACT_SIZE_OZ) -> "DeploymentGate | None":
        """Return a gate only if at least one gate is enabled, else None (so the
        caller skips all gate work and parity is preserved)."""
        g = cls(config, contract_size)
        return g if g.enabled else None

    # -- day bookkeeping ------------------------------------------------------
    @staticmethod
    def _signal_day(sig) -> date:
        # The feed-zone (source) date -- the same day key the report's Daily
        # breakdown groups by (rows[*].signal_time_source), so the breaker lines
        # up with the reported max_daily_loss / daily_win_rate.
        src = getattr(sig, "signal_time_source", None)
        if isinstance(src, datetime):
            return src.date()
        return sig.signal_time_chart.date()

    def _roll_day(self, day: date, equity: float) -> None:
        if day != self._day:
            self._day = day
            self._day_start_equity = equity
            self._day_pnl = 0.0
            self._day_blocked = False

    # -- gate 1+2+3: pre-replay/pre-placement check ---------------------------
    def pre_check(self, sig, equity: float) -> str | None:
        """Cheap gates that need no lifecycle: daily-loss breaker + concurrency.
        Returns a reject reason, or None to proceed. Call AFTER screen_signal."""
        self._roll_day(self._signal_day(sig), equity)

        if self.daily_limit > 0 and self._day_blocked:
            self.rejected["daily_loss_breaker"] += 1
            return "daily_loss_breaker"

        if self.max_open > 0:
            t = sig.signal_time_chart
            n_open = sum(1 for w in self._windows
                         if w.start <= t and (w.end is None or t < w.end))
            self._peak_concurrency = max(self._peak_concurrency, n_open)
            if n_open >= self.max_open:
                self.rejected["max_open_signals"] += 1
                return "max_open_signals"
        return None

    # -- gate computing worst-case min-lot risk -------------------------------
    def worst_case_risk(self, entry_rows: list[dict]) -> tuple[float, float]:
        """(single, zone) worst-case dollar risk if every planned ladder leg is
        taken at the MINIMUM lot and stopped out. single = largest single leg,
        zone = whole ladder. Uses the PLANNED entry/effective-SL of each leg
        (independent of whether it filled), so it is the true pre-trade budget."""
        per = []
        for er in entry_rows:
            ep, sl = er.get("entry_price"), er.get("effective_SL")
            if ep is None or sl is None:
                continue
            per.append(abs(float(ep) - float(sl)) * self.min_lot * self.contract_size)
        if not per:
            return 0.0, 0.0
        return max(per), sum(per)

    def risk_budget_check(self, entry_rows: list[dict], equity: float) -> str | None:
        """Reject when the min-lot worst case exceeds the configured budget.
        Call after the signal is built (entry rows carry planned levels)."""
        if not self.rb:
            return None
        single, zone = self.worst_case_risk(entry_rows)
        if self.max_single > 0 and single > equity * self.max_single:
            self.rejected["risk_budget_single"] += 1
            return "risk_budget_single"
        if self.max_zone > 0 and zone > equity * self.max_zone:
            self.rejected["risk_budget_zone"] += 1
            return "risk_budget_zone"
        return None

    # -- register an ACCEPTED signal ------------------------------------------
    def register(self, sig, built: dict) -> None:
        """Record an accepted signal's realized day P&L + open window. Call only
        after the signal is committed to the equity curve."""
        if self.daily_limit > 0:
            row = built["row"]
            if built["status"] != "OPEN":
                realized = float(built["equity_after"]) - float(row["equity_before"])
                self._day_pnl += realized
                if (self._day_start_equity
                        and self._day_pnl <= -self.daily_limit * self._day_start_equity):
                    self._day_blocked = True

        if self.max_open > 0:
            # A signal occupies the one slot from PLACEMENT (its arrival) until it
            # is fully closed OR its pending orders expire -- a laddered signal
            # rests as pending LIMITs before any leg fills, so concurrency is
            # measured from arrival, not first fill (else a second signal slips in
            # during the pending window and the cap is breached). End:
            #   * still-open filled leg  -> None (open to end of data / now)
            #   * any filled leg         -> last exit
            #   * all NO_FILL            -> arrival + pending_expiry (orders rested
            #                               then cancelled; the slot was held)
            start = sig.signal_time_chart
            ers = built["entry_rows"]
            open_leg = any(er.get("fill_time") and not er.get("exit_time") for er in ers)
            exits = [er["exit_time"] for er in ers if er.get("fill_time") and er.get("exit_time")]
            if open_leg:
                end = None
            elif exits:
                end = max(exits)
            else:
                end = start + timedelta(minutes=self.pending_expiry) if self.pending_expiry else start
            self._windows.append(_OpenWindow(start, end))

    # -- reporting ------------------------------------------------------------
    def summary(self) -> dict:
        return {
            "rejected": dict(self.rejected),
            "peak_concurrency_seen": self._peak_concurrency,
            "config": {
                "risk_budget_gate": self.rb,
                "max_single_entry_risk_pct": self.max_single,
                "max_zone_risk_pct": self.max_zone,
                "daily_loss_limit_pct": self.daily_limit,
                "max_open_signals": self.max_open,
            },
        }
