"""Broker-TP guard for uncapped trailing runners.

This is the public MT5 executor layer above ``mt5_executor_trailing``. The
trailing lifecycle can model a runner that continues beyond TP3 and exits only by
executor-owned trailing SL. Live MT5 must mirror that intent by sending ``tp=0``
(no broker take-profit) for placement and self-heal paths when the active config
uses ``--runner-final-cap none`` with a trailing-close stop.
"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from trading.engine.core.config import StrategyConfig

from .mt5_executor_trailing import Mt5Executor as _TrailingMt5Executor


class Mt5Executor(_TrailingMt5Executor):
    """MT5 executor that can omit broker TP for pure trailing runners."""

    @staticmethod
    def _config_omits_broker_tp(config: StrategyConfig) -> bool:
        has_trailing_close = float(getattr(config, "trailing_close_distance", 0.0) or 0.0) > 0
        return bool(getattr(config, "runner_no_final_cap", False)) and has_trailing_close

    @staticmethod
    def _plan_omits_broker_tp(plan) -> bool:
        if hasattr(plan, "broker_take_profit_price"):
            return getattr(plan, "broker_take_profit_price") is None
        has_trailing_close = float(getattr(plan, "trailing_close_distance", 0.0) or 0.0) > 0
        return bool(getattr(plan, "runner_no_final_cap", False)) and has_trailing_close

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

        MT5 represents an absent take-profit as ``tp=0.0`` for both pending orders
        and SLTP modifications. Mutating the request before calling the real
        ``order_send`` also keeps forensic/log records honest because callers log
        the same request object after the send.
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
