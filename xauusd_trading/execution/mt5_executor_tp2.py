"""TP2-aware MT5 executor wrapper.

The historical engine moves the effective stop to TP2 once TP2 is touched
(`Position.stage >= 2`).  The original MT5 executor already places orders,
reconciles fills, cancels expired pendings, locks to TP1, and handles time
exit.  This wrapper adds the missing live-management step: after the base
management pass, any still-open broker positions are protected at TP2 when the
engine has reached stage 2.
"""
from __future__ import annotations

from xauusd_trading import Position, StrategyConfig

from .mt5_executor import ExecutionLog, Mt5Executor as _BaseMt5Executor, signal_to_magic


class Mt5Executor(_BaseMt5Executor):
    """MT5 executor with TP2 stop-lock parity against the replay engine."""

    def manage_position(self, engine_pos: Position, config: StrategyConfig, chart_now):
        """Manage one tracked signal, including TP2 SL-lock parity.

        The base executor handles expiry, reconciliation-dependent TP1 lock,
        late TP1 catch-up, and time exit.  After that pass, this method applies
        the same stage-2 protection the backtest uses: when TP2 has been touched
        and ``lock_after_tp2`` is enabled, all remaining live positions should
        have broker SL at TP2.
        """
        log = super().manage_position(engine_pos, config, chart_now)

        if not config.lock_after_tp2 or engine_pos.stage < 2:
            return log

        magic = signal_to_magic(engine_pos.signal.signal_key)
        signal_key = engine_pos.signal.signal_key
        digits = self.mt5.symbol_info(self.symbol).digits
        tp2_sl = round(engine_pos.signal.tp2, digits)
        tolerance = 10 ** (-digits)

        # If the backtest/replay says an entry already exited at LOCK_TP2 but
        # MT5 still shows an open position, close it at market as a catch-up.
        # This mirrors the existing late TP1 catch-up safety behavior.
        if any(e.status == "LOCK_TP2" for e in engine_pos.entries):
            unlocked = [
                p for p in self.find_positions(magic)
                if abs(p.sl - tp2_sl) > tolerance
            ]
            backtest_lock_pnl = sum(
                e.pnl or 0.0
                for e in engine_pos.entries
                if e.status == "LOCK_TP2"
            )
            for p in unlocked:
                tick = self.mt5.symbol_info_tick(self.symbol)
                if tick is None:
                    log.actions.append(
                        f"  Late TP2 catch-up on #{p.ticket}: no tick available, skipping"
                    )
                    continue
                if p.type == self.mt5.POSITION_TYPE_BUY:
                    close_type, price = self.mt5.ORDER_TYPE_SELL, tick.bid
                else:
                    close_type, price = self.mt5.ORDER_TYPE_BUY, tick.ask
                req = {
                    "action":       self.mt5.TRADE_ACTION_DEAL,
                    "position":     p.ticket,
                    "symbol":       self.symbol,
                    "volume":       p.volume,
                    "type":         close_type,
                    "price":        price,
                    "magic":        magic,
                    "comment":      f"{signal_key}/late-tp2"[:31],
                    "type_filling": self.mt5.ORDER_FILLING_RETURN,
                }
                res = self.mt5.order_send(req)
                success = bool(
                    res is not None and res.retcode == self.mt5.TRADE_RETCODE_DONE
                )
                self._log_order_send(signal_key, "close_catchup_tp2", req, res, success=success)
                if success:
                    log.closed += 1
                    log.actions.append(
                        f"  Late TP2 catch-up closed #{p.ticket} @ {price:g} "
                        f"({signal_key}; backtest LOCK_TP2 would have realized "
                        f"${backtest_lock_pnl:+.2f} -- actual close at current market)"
                    )
                else:
                    reason = str(res.comment if res else self.mt5.last_error())
                    log.actions.append(
                        f"  FAILED late TP2 catch-up close on #{p.ticket}: {reason}"
                    )

        # For remaining open positions, move broker SL to TP2.  This is the
        # direct live equivalent of Position.effective_stop_for(... stage >= 2).
        locked_tickets: list[int] = []
        lock_failures: list[tuple[int, str]] = []
        for p in self.find_positions(magic):
            if abs(p.sl - tp2_sl) <= tolerance:
                continue
            req = {
                "action":   self.mt5.TRADE_ACTION_SLTP,
                "position": p.ticket,
                "sl":       tp2_sl,
                "tp":       p.tp,
            }
            res = self.mt5.order_send(req)
            success = bool(res is not None and res.retcode == self.mt5.TRADE_RETCODE_DONE)
            self._log_order_send(signal_key, "modify_sl_to_tp2", req, res, success=success)
            if success:
                log.modified += 1
                locked_tickets.append(p.ticket)
                log.actions.append(
                    f"  Locked SL on #{p.ticket} to TP2 {tp2_sl:g} ({signal_key})"
                )
            else:
                reason = str(res.comment if res else self.mt5.last_error())
                lock_failures.append((p.ticket, reason))
                log.actions.append(
                    f"  FAILED TP2 SL-lock on #{p.ticket}: {reason}"
                )

        if self.notifier is not None and (locked_tickets or lock_failures):
            # Reuse the generic tp1_lock notification shape if the notifier has
            # not grown a dedicated TP2 method yet; the action text above keeps
            # the execution log explicit.
            notify = getattr(self.notifier, "tp2_lock", None)
            if callable(notify):
                notify(
                    signal_key=signal_key,
                    side=engine_pos.signal.side,
                    locked=locked_tickets,
                    failed=lock_failures,
                    sl=tp2_sl,
                )

        return log
