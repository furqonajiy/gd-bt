"""Broker-TP guard for trailing-close strategies.

This is the public MT5 executor layer above ``mt5_executor_trailing``. When the
strategy enables trailing-close, live MT5 must not carry a fixed broker TP3 cap:
TP3 remains a model/reference level, while the executor-owned SL does the exit.
"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from trading.engine.core.config import StrategyConfig

from .mt5_executor import ExecutionLog, signal_to_magic
from .mt5_executor_trailing import Mt5Executor as _TrailingMt5Executor


class Mt5Executor(_TrailingMt5Executor):
    """MT5 executor that omits broker TP whenever trailing-close owns exit."""

    @staticmethod
    def _config_omits_broker_tp(config: StrategyConfig) -> bool:
        # Drop the broker TP only for a trailing-close RUNNER (trails AND runs past
        # the final target). A trailing-close book that still caps at its target
        # keeps its broker TP -- that TP is the only thing that closes the leg at the
        # target live (see trailing_engine._broker_take_profit_price).
        runs_past_target = bool(getattr(config, "runner_no_final_cap", False))
        return runs_past_target and float(getattr(config, "trailing_close_distance", 0.0) or 0.0) > 0

    @staticmethod
    def _plan_omits_broker_tp(plan) -> bool:
        # The decide wrapper stamps broker_take_profit_price = None exactly when the
        # config is a trailing-close runner, so prefer it; the fallback mirrors the
        # runner_no_final_cap + trailing-close gate for a hand-built plan.
        if hasattr(plan, "broker_take_profit_price"):
            return getattr(plan, "broker_take_profit_price") is None
        runs_past_target = bool(getattr(plan, "runner_no_final_cap", False))
        return runs_past_target and float(getattr(plan, "trailing_close_distance", 0.0) or 0.0) > 0

    @staticmethod
    def _format_no_broker_tp_log(log):
        """Render broker ``tp=0`` as operator-facing ``TP=none``."""
        log.actions = [
            action.replace("TP=0.0", "TP=none").replace("TP=0", "TP=none")
            for action in log.actions
        ]
        return log

    @contextmanager
    def _without_broker_take_profit(self) -> Iterator[None]:
        """Temporarily rewrite order_send requests so MT5 receives no TP.

        MT5 represents an absent take-profit as ``tp=0.0`` for pending orders,
        market fills, and SLTP modifications. Mutating before ``order_send`` keeps
        forensic/log records honest because callers log the same request object.
        """
        original_order_send = self.mt5.order_send

        def order_send_without_tp(request):
            if isinstance(request, dict) and "tp" in request:
                request["tp"] = 0.0
            return original_order_send(request)

        self.mt5.order_send = order_send_without_tp
        try:
            yield
        finally:
            self.mt5.order_send = original_order_send

    def _remove_existing_broker_take_profits(self, engine_pos, log: ExecutionLog) -> None:
        """Clear TP from already-open MT5 positions managed by trailing-close."""
        magic = signal_to_magic(engine_pos.signal.signal_key)
        signal_key = engine_pos.signal.signal_key
        sym = self.mt5.symbol_info(self.symbol)
        digits = int(getattr(sym, "digits", 2) if sym is not None else 2)
        tolerance = 10 ** (-digits)
        for p in self.find_positions(magic):
            current_tp = float(getattr(p, "tp", 0.0) or 0.0)
            if abs(current_tp) <= tolerance:
                continue
            req = {
                "action": self.mt5.TRADE_ACTION_SLTP,
                "position": p.ticket,
                "sl": float(getattr(p, "sl", 0.0) or 0.0),
                "tp": 0.0,
            }
            res = self.mt5.order_send(req)
            success = bool(res is not None and res.retcode == self.mt5.TRADE_RETCODE_DONE)
            self._log_order_send(signal_key, "remove_trailing_close_broker_tp", req, res, success=success)
            if success:
                log.modified += 1
                log.actions.append(
                    f"  Removed broker TP on #{p.ticket}; trailing-close will exit by SL only ({signal_key})"
                )
            else:
                reason = str(res.comment if res is not None else self.mt5.last_error())
                log.warnings.append(
                    f"  FAILED to remove broker TP on #{p.ticket} ({signal_key}): {reason}"
                )

    def place_signal(self, signal, plan):
        if not self._plan_omits_broker_tp(plan):
            return super().place_signal(signal, plan)
        with self._without_broker_take_profit():
            log = super().place_signal(signal, plan)
        return self._format_no_broker_tp_log(log)

    def manage_position(self, engine_pos, config, chart_now):
        if not self._config_omits_broker_tp(config):
            return super().manage_position(engine_pos, config, chart_now)
        with self._without_broker_take_profit():
            log = super().manage_position(engine_pos, config, chart_now)
        self._remove_existing_broker_take_profits(engine_pos, log)
        return self._format_no_broker_tp_log(log)

    def replace_missing_pending_entries(self, engine_pos, config, now):
        if not self._config_omits_broker_tp(config):
            return super().replace_missing_pending_entries(engine_pos, config, now)
        with self._without_broker_take_profit():
            log = super().replace_missing_pending_entries(engine_pos, config, now)
        return self._format_no_broker_tp_log(log)

    def reopen_missing_open_positions(self, engine_pos, config):
        if not self._config_omits_broker_tp(config):
            return super().reopen_missing_open_positions(engine_pos, config)
        with self._without_broker_take_profit():
            log = super().reopen_missing_open_positions(engine_pos, config)
        return self._format_no_broker_tp_log(log)
