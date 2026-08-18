from __future__ import annotations

from datetime import datetime, timedelta, timezone
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
import logging
from pathlib import Path
import time
from typing import Callable

from ..database import connect_database, utc_now
from ..daily_schedule import (
    SAO_PAULO_TIMEZONE,
    current_daily_signal_session_start_at,
    next_daily_signal_resume_at,
)
from .account_service import MT5AccountService
from .models import ExecutionProfile, MT5Account
from .ipc_lock import MT5OperationLock
from .pending_order_executor import (
    MT5_MAGIC_NUMBER,
    is_successful_mt5_result,
    market_filling_constant,
    mt5_constant,
    mt5_failure_message,
    pending_order_type_constant,
)
from .settlement_monitor import SettlementMonitor
from .trade_comment import parse_trade_comment
from ..telegram_notifier import TelegramUserNotifier

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class ManagedOrder:
    order_id: int
    group_id: int
    tp_index: int
    direction: str
    original_stop: Decimal
    take_profit: Decimal
    expiration_at: str


@dataclass(frozen=True)
class ProtectionOutcome:
    state: str
    changed: bool = False
    target_sl: Decimal | None = None
    confirmed_sl: Decimal | None = None
    error: str | None = None

    @property
    def protected(self) -> bool:
        return self.state in {"applied", "already_protected", "closed_at_market"}


class PositionManager:
    def __init__(
        self,
        database_path: Path,
        accounts: MT5AccountService,
        client_factory: Callable[[], object],
        settlement_monitor: SettlementMonitor | None = None,
        notifier: TelegramUserNotifier | None = None,
    ) -> None:
        self.database_path = database_path
        self.accounts = accounts
        self.client_factory = client_factory
        self.settlement_monitor = settlement_monitor
        self.notifier = notifier
        self._last_settlement_check: dict[int, float] = {}
        self._daily_limit_notifications: set[tuple[int, str, str]] = set()

    def manage_account(self, account: MT5Account, profile: ExecutionProfile) -> int:
        if account.terminal_path is None:
            return 0
        client = self.client_factory()
        operation_lock = MT5OperationLock(account.terminal_path.parent)
        password: str | None = None
        changed = 0
        try:
            operation_lock.acquire()
            password = self.accounts.decrypted_password_for_account(account)
            if not client.initialize(account.terminal_path, int(account.login), password, account.server_name):
                return 0
            positions = tuple(client.positions_get())
            pending_orders = tuple(client.orders_get())
            copier_positions = tuple(
                position
                for position in positions
                if int(value(position, "magic", 0) or 0) == MT5_MAGIC_NUMBER
            )
            copier_pending_orders = tuple(
                order
                for order in pending_orders
                if int(value(order, "magic", 0) or 0) == MT5_MAGIC_NUMBER
            )
            daily_limit_changes = self._enforce_daily_result_limits(
                client,
                account,
                profile,
                copier_positions,
                copier_pending_orders,
            )
            if daily_limit_changes is not None:
                if self.settlement_monitor is not None:
                    self.settlement_monitor.reconcile(client, account)
                return daily_limit_changes

            managed_positions: list[tuple[object, ManagedOrder]] = []
            for position in positions:
                if int(value(position, "magic", 0) or 0) != MT5_MAGIC_NUMBER:
                    continue
                order_record = self._find_order_for_item(account.id, position)
                if order_record is None:
                    LOGGER.warning(
                        "Posicao do copiador sem vinculo no banco. account=%s ticket=%s comment=%r",
                        account.id,
                        value(position, "ticket", 0),
                        value(position, "comment", ""),
                    )
                    continue
                ticket = int(value(position, "ticket", 0) or 0)
                self._mark_filled(order_record.order_id, ticket)
                managed_positions.append((position, order_record))

            history_deals: tuple[object, ...] = ()
            if managed_positions:
                now = datetime.now(tz=timezone.utc)
                history_deals = client.history_deals_get(now - timedelta(days=30), now)
            managed_group_ids = {
                order_record.group_id
                for _position, order_record in managed_positions
                if order_record.tp_index > 1
            }
            tp1_reached_groups = {
                group_id
                for group_id in managed_group_ids
                if self._tp1_reached(client, group_id, history_deals)
            }
            group_protection: dict[int, list[ProtectionOutcome]] = {}
            for position, order_record in managed_positions:
                force_breakeven = (
                    profile.tp1_breakeven_enabled
                    and order_record.group_id in tp1_reached_groups
                    and order_record.tp_index > 1
                )
                if not force_breakeven and not (
                    profile.breakeven_enabled or profile.trailing_enabled
                ):
                    continue
                outcome = self._protect_position(
                    client,
                    position,
                    order_record.direction,
                    order_record.original_stop,
                    profile,
                    force_breakeven=force_breakeven,
                )
                if outcome.changed:
                    changed += 1
                if force_breakeven:
                    self._record_be_outcome(order_record, outcome)
                    group_protection.setdefault(order_record.group_id, []).append(outcome)
            for pending_order in pending_orders:
                if int(value(pending_order, "magic", 0) or 0) != MT5_MAGIC_NUMBER:
                    continue
                order_record = self._find_order_for_item(account.id, pending_order)
                if order_record is None:
                    continue
                symbol = str(value(pending_order, "symbol", ""))
                tick = client.symbol_info_tick(symbol)
                if tick is None:
                    continue
                ticket = int(value(pending_order, "ticket", value(pending_order, "order", 0)) or 0)
                if order_record.group_id in tp1_reached_groups:
                    result = client.order_send(
                        {
                            "action": mt5_constant(client, "TRADE_ACTION_REMOVE", 8),
                            "order": ticket,
                            "magic": MT5_MAGIC_NUMBER,
                            "comment": "tgcp tp1 reached",
                        }
                    )
                    if is_successful_mt5_result(client, result, check=False):
                        self._mark_cancelled(order_record.order_id, order_record.group_id)
                        changed += 1
                    continue
                if datetime.fromisoformat(order_record.expiration_at) <= datetime.now(tz=timezone.utc):
                    result = client.order_send(
                        {
                            "action": mt5_constant(client, "TRADE_ACTION_REMOVE", 8),
                            "order": ticket,
                            "magic": MT5_MAGIC_NUMBER,
                            "comment": "tgcp expired",
                        }
                    )
                    if is_successful_mt5_result(client, result, check=False):
                        self._mark_expired(order_record.order_id, order_record.group_id)
                        changed += 1
                    continue
                current_price = tick.bid if order_record.direction == "BUY" else tick.ask
                invalidated = (
                    order_record.direction == "BUY"
                    and (current_price <= order_record.original_stop or current_price >= order_record.take_profit)
                ) or (
                    order_record.direction == "SELL"
                    and (current_price >= order_record.original_stop or current_price <= order_record.take_profit)
                )
                if not invalidated:
                    continue
                result = client.order_send(
                    {
                        "action": mt5_constant(client, "TRADE_ACTION_REMOVE", 8),
                        "order": ticket,
                        "magic": MT5_MAGIC_NUMBER,
                        "comment": "tgcp invalidated",
                    }
                )
                if is_successful_mt5_result(client, result, check=False):
                    self._mark_cancelled(order_record.order_id, order_record.group_id)
                    changed += 1
            for group_id, outcomes in group_protection.items():
                if outcomes and all(outcome.protected for outcome in outcomes):
                    if self._all_open_targets_protected(group_id):
                        if self._mark_breakeven_applied(group_id):
                            self._notify_group_be(account, group_id, outcomes)
            if self.settlement_monitor is not None:
                last_check = self._last_settlement_check.get(account.id, 0.0)
                if time.monotonic() - last_check >= 3:
                    changed += self.settlement_monitor.reconcile(client, account)
                    self._last_settlement_check[account.id] = time.monotonic()
            return changed
        finally:
            password = None
            client.shutdown()
            operation_lock.close()

    def _enforce_daily_result_limits(
        self,
        client: object,
        account: MT5Account,
        profile: ExecutionProfile,
        positions: tuple[object, ...],
        pending_orders: tuple[object, ...],
    ) -> int | None:
        """Enforce account-wide daily profit/loss thresholds as a live kill switch.

        The entry validator prevents *new* signals after the realized result reaches
        a threshold. This guard also includes floating P/L and removes existing
        copier exposure as soon as the configured threshold is observed.

        Detection and pausing must run even with zero open exposure: a target
        commonly gets reached by the very deal that closes the account's last
        open position, leaving nothing to close on this same pass. Skipping
        detection in that case used to leave `daily_signal_pause_until` unset
        and the warning/notification unfired, relying entirely on the entry
        validator to catch the next signal reactively instead of pausing the
        moment the threshold is observed.
        """
        if profile.daily_profit_target <= 0 and profile.daily_loss_limit <= 0:
            return None

        now = datetime.now(tz=timezone.utc)
        session_start = current_daily_signal_session_start_at(now)
        realized_result = Decimal("0")
        for deal in client.history_deals_get(session_start, now):
            if int(value(deal, "magic", 0) or 0) != MT5_MAGIC_NUMBER:
                continue
            for field_name in ("profit", "commission", "swap", "fee"):
                realized_result += Decimal(str(value(deal, field_name, 0) or 0))

        floating_result = Decimal("0")
        for position in positions:
            floating_result += Decimal(str(value(position, "profit", 0) or 0))
            floating_result += Decimal(str(value(position, "swap", 0) or 0))

        observed_result = realized_result + floating_result
        trigger_kind: str | None = None
        configured_limit = Decimal("0")
        if (
            profile.daily_profit_target > 0
            and observed_result >= profile.daily_profit_target
        ):
            trigger_kind = "profit_target"
            configured_limit = profile.daily_profit_target
        elif (
            profile.daily_loss_limit > 0
            and observed_result <= -profile.daily_loss_limit
        ):
            trigger_kind = "loss_limit"
            configured_limit = profile.daily_loss_limit
        if trigger_kind is None:
            return None

        pause_until = next_daily_signal_resume_at(now)
        with connect_database(self.database_path) as connection:
            connection.execute(
                """
                UPDATE users
                SET daily_signal_pause_until = ?, updated_at = ?
                WHERE id = ?
                """,
                (pause_until.isoformat(), utc_now(), account.user_id),
            ).close()

        changed = 0
        failures: list[str] = []
        trade_comment = (
            "tgcp daily target" if trigger_kind == "profit_target" else "tgcp daily stop"
        )
        for pending_order in pending_orders:
            ticket = int(value(pending_order, "ticket", value(pending_order, "order", 0)) or 0)
            result = client.order_send(
                {
                    "action": mt5_constant(client, "TRADE_ACTION_REMOVE", 8),
                    "order": ticket,
                    "magic": MT5_MAGIC_NUMBER,
                    "comment": trade_comment,
                }
            )
            if is_successful_mt5_result(client, result, check=False):
                order_record = self._find_order_for_item(account.id, pending_order)
                if order_record is not None:
                    self._mark_cancelled(order_record.order_id, order_record.group_id)
                changed += 1
            else:
                failures.append(
                    f"ordem pendente {ticket}: {mt5_failure_message(client, result)}"
                )

        for position in positions:
            ticket = int(value(position, "ticket", 0) or 0)
            symbol = str(value(position, "symbol", ""))
            info = client.symbol_info(symbol)
            tick = client.symbol_info_tick(symbol)
            if info is None or tick is None:
                failures.append(f"posição {ticket}: cotação indisponível")
                continue
            position_type = int(value(position, "type", 0) or 0)
            buy_type = mt5_constant(client, "POSITION_TYPE_BUY", 0)
            direction = "BUY" if position_type == buy_type else "SELL"
            close_direction = "SELL" if direction == "BUY" else "BUY"
            close_price = tick.bid if direction == "BUY" else tick.ask
            result = client.order_send(
                {
                    "action": mt5_constant(client, "TRADE_ACTION_DEAL", 1),
                    "position": ticket,
                    "symbol": symbol,
                    "volume": float(value(position, "volume", 0) or 0),
                    "type": pending_order_type_constant(client, close_direction),
                    "price": float(close_price),
                    "deviation": int(profile.max_slippage_points),
                    "magic": MT5_MAGIC_NUMBER,
                    "comment": trade_comment,
                    "type_time": mt5_constant(client, "ORDER_TIME_GTC", 0),
                    "type_filling": market_filling_constant(client, info),
                }
            )
            if is_successful_mt5_result(client, result, check=False):
                changed += 1
            else:
                failures.append(f"posição {ticket}: {mt5_failure_message(client, result)}")

        self._notify_daily_result_limit(
            account,
            trigger_kind,
            configured_limit,
            observed_result,
            pause_until,
            failures,
            session_start,
        )
        LOGGER.warning(
            "Trava financeira diária acionada. account=%s tipo=%s limite=%s "
            "resultado=%s alteracoes=%s falhas=%s",
            account.id,
            trigger_kind,
            configured_limit,
            observed_result,
            changed,
            len(failures),
        )
        return changed

    def _notify_daily_result_limit(
        self,
        account: MT5Account,
        trigger_kind: str,
        configured_limit: Decimal,
        observed_result: Decimal,
        pause_until: datetime,
        failures: list[str],
        session_start: datetime,
    ) -> None:
        if self.notifier is None:
            return
        notification_key = (account.id, session_start.isoformat(), trigger_kind)
        if notification_key in self._daily_limit_notifications:
            return
        self._daily_limit_notifications.add(notification_key)
        with connect_database(self.database_path) as connection:
            row = connection.execute(
                "SELECT telegram_user_id FROM users WHERE id = ?",
                (account.user_id,),
            ).fetchone()
        if row is None:
            return
        local_resume = pause_until.astimezone(SAO_PAULO_TIMEZONE)
        status = (
            "As posições do copiador foram encerradas e as ordens pendentes canceladas."
            if not failures
            else "A proteção foi acionada, mas algumas exposições não puderam ser encerradas. "
            "Verifique imediatamente o MetaTrader 5."
        )
        is_profit_target = trigger_kind == "profit_target"
        title = "🎯 META DIÁRIA ATINGIDA" if is_profit_target else "🛑 LIMITE DE PERDA DIÁRIA ACIONADO"
        limit_label = "Meta configurada" if is_profit_target else "Limite configurado"
        message = (
            f"{title}\n\n"
            f"Conta: {account.masked_login}\n"
            f"{limit_label}: US$ {configured_limit:.2f}\n"
            f"Resultado observado: US$ {observed_result:.2f}\n\n"
            f"{status}\n"
            f"Novos sinais ficam bloqueados até "
            f"{local_resume.strftime('%d/%m às %H:%M')} (Brasília).\n\n"
            "O fechamento é executado a mercado e pode variar por spread e slippage."
        )
        if failures:
            message += "\n\nFalhas: " + "; ".join(failures[:3])
        self.notifier.send(int(row[0]), message)

    def _protect_position(
        self,
        client: object,
        position: object,
        direction: str,
        original_stop: Decimal,
        profile: ExecutionProfile,
        *,
        force_breakeven: bool = False,
    ) -> ProtectionOutcome:
        symbol = str(value(position, "symbol", ""))
        info = client.symbol_info(symbol)
        tick = client.symbol_info_tick(symbol)
        if info is None or tick is None:
            return ProtectionOutcome("failed", error="symbol_or_tick_unavailable")
        entry = Decimal(str(value(position, "price_open", 0)))
        current_sl = Decimal(str(value(position, "sl", 0) or 0))
        take_profit = Decimal(str(value(position, "tp", 0) or 0))
        initial_risk = abs(entry - original_stop)
        if entry <= 0 or initial_risk <= 0:
            return ProtectionOutcome("failed", error="invalid_entry_or_initial_risk")

        current_price = tick.bid if direction == "BUY" else tick.ask
        favorable_move = current_price - entry if direction == "BUY" else entry - current_price
        if not force_breakeven and favorable_move < initial_risk:
            return ProtectionOutcome("not_triggered")
        already_protected = (
            direction == "BUY" and current_sl >= entry
        ) or (
            direction == "SELL" and current_sl > 0 and current_sl <= entry
        )
        if force_breakeven and already_protected:
            return ProtectionOutcome(
                "already_protected",
                target_sl=entry,
                confirmed_sl=current_sl,
            )
        if force_breakeven and (
            (direction == "BUY" and current_price <= entry)
            or (direction == "SELL" and current_price >= entry)
        ):
            return self._close_after_missed_breakeven(
                client,
                position,
                direction,
                info,
                tick,
                profile.max_slippage_points,
                entry,
            )

        proposed_sl = entry
        if profile.trailing_enabled and favorable_move >= initial_risk:
            trailing_sl = current_price - initial_risk if direction == "BUY" else current_price + initial_risk
            proposed_sl = max(entry, trailing_sl) if direction == "BUY" else min(entry, trailing_sl)
        proposed_sl = normalize_price(proposed_sl, info.trade_tick_size, info.digits)

        improves = (
            direction == "BUY" and proposed_sl > current_sl
        ) or (
            direction == "SELL" and (current_sl == 0 or proposed_sl < current_sl)
        )
        if not improves:
            return ProtectionOutcome("not_improved")
        request = {
            "action": mt5_constant(client, "TRADE_ACTION_SLTP", 6),
            "position": int(value(position, "ticket", 0)),
            "symbol": symbol,
            "sl": float(proposed_sl),
            "tp": float(take_profit),
            "magic": MT5_MAGIC_NUMBER,
            "comment": "tgcp protection",
        }
        result = client.order_send(request)
        successful = is_successful_mt5_result(client, result, check=False)
        if not successful:
            error = mt5_failure_message(client, result)
            LOGGER.warning(
                "Protecao MT5 rejeitada. position=%s symbol=%s sl=%s motivo=%s last_error=%s",
                request["position"],
                symbol,
                proposed_sl,
                error,
                client.last_error(),
            )
            return ProtectionOutcome("failed", target_sl=proposed_sl, error=error)

        confirmed_sl = self._confirmed_position_sl(
            client,
            int(request["position"]),
            direction,
            proposed_sl,
            Decimal(str(info.trade_tick_size)),
        )
        if confirmed_sl is None:
            LOGGER.warning(
                "Protecao MT5 aceita, mas nao confirmada. position=%s symbol=%s sl=%s",
                request["position"],
                symbol,
                proposed_sl,
            )
            return ProtectionOutcome(
                "failed",
                changed=True,
                target_sl=proposed_sl,
                error="broker_sl_not_confirmed",
            )
        if force_breakeven:
            LOGGER.info(
                "BE apos TP1 confirmado. position=%s symbol=%s sl=%s",
                request["position"],
                symbol,
                confirmed_sl,
            )
        return ProtectionOutcome(
            "applied",
            changed=True,
            target_sl=proposed_sl,
            confirmed_sl=confirmed_sl,
        )

    def _confirmed_position_sl(
        self,
        client: object,
        ticket: int,
        direction: str,
        target_sl: Decimal,
        tolerance: Decimal,
    ) -> Decimal | None:
        for attempt in range(3):
            positions = tuple(client.positions_get())
            position = next(
                (
                    item
                    for item in positions
                    if int(value(item, "ticket", 0) or 0) == ticket
                ),
                None,
            )
            if position is None:
                return target_sl
            actual_sl = Decimal(str(value(position, "sl", 0) or 0))
            confirmed = (
                direction == "BUY" and actual_sl >= target_sl - tolerance
            ) or (
                direction == "SELL" and actual_sl > 0 and actual_sl <= target_sl + tolerance
            )
            if confirmed:
                return actual_sl
            if attempt < 2:
                time.sleep(0.05)
        return None

    def _close_after_missed_breakeven(
        self,
        client: object,
        position: object,
        direction: str,
        info: object,
        tick: object,
        max_slippage_points: int,
        target_sl: Decimal,
    ) -> ProtectionOutcome:
        ticket = int(value(position, "ticket", 0) or 0)
        symbol = str(value(position, "symbol", ""))
        close_direction = "SELL" if direction == "BUY" else "BUY"
        close_price = tick.bid if direction == "BUY" else tick.ask
        request = {
            "action": mt5_constant(client, "TRADE_ACTION_DEAL", 1),
            "position": ticket,
            "symbol": symbol,
            "volume": float(value(position, "volume", 0) or 0),
            "type": pending_order_type_constant(client, close_direction),
            "price": float(close_price),
            "deviation": int(max_slippage_points),
            "magic": MT5_MAGIC_NUMBER,
            "comment": "tgcp be emergency",
            "type_time": mt5_constant(client, "ORDER_TIME_GTC", 0),
            "type_filling": market_filling_constant(client, info),
        }
        result = client.order_send(request)
        if not is_successful_mt5_result(client, result, check=False):
            error = f"emergency_close_failed:{mt5_failure_message(client, result)}"
            LOGGER.error(
                "BE perdido e fechamento emergencial rejeitado. position=%s symbol=%s motivo=%s",
                ticket,
                symbol,
                error,
            )
            return ProtectionOutcome("failed", target_sl=target_sl, error=error)
        LOGGER.warning(
            "Preco retornou antes do BE; posicao fechada a mercado. position=%s symbol=%s price=%s",
            ticket,
            symbol,
            close_price,
        )
        return ProtectionOutcome(
            "closed_at_market",
            changed=True,
            target_sl=target_sl,
        )

    def _tp1_reached(
        self,
        client: object,
        group_id: int,
        history_deals: tuple[object, ...],
    ) -> bool:
        with connect_database(self.database_path) as connection:
            row = connection.execute(
                """
                SELECT g.direction, g.symbol, g.signal_id, g.tp1_reached_at,
                       o.take_profit, o.mt5_order_ticket, o.mt5_position_ticket
                FROM execution_groups g
                JOIN execution_orders o
                  ON o.execution_group_id = g.id AND o.tp_index = 1
                WHERE g.id = ?
                """,
                (group_id,),
            ).fetchone()
        if row is None:
            return False
        if row[3]:
            return True

        direction = str(row[0])
        symbol = str(row[1])
        signal_prefix = str(row[2])[:8]
        take_profit = Decimal(str(row[4]))
        info = client.symbol_info(symbol)
        tolerance = info.trade_tick_size if info is not None else Decimal("0")

        tick = client.symbol_info_tick(symbol)
        reached = False
        if tick is not None:
            current_price = tick.bid if direction == "BUY" else tick.ask
            reached = price_reached_target(
                direction,
                current_price,
                take_profit,
                tolerance,
            )

        known_tickets = {
            str(ticket)
            for ticket in (row[5], row[6])
            if ticket is not None and str(ticket)
        }
        if not reached:
            reached = tp1_reached_in_history(
                client=client,
                history_deals=history_deals,
                direction=direction,
                signal_prefix=signal_prefix,
                take_profit=take_profit,
                tolerance=tolerance,
                known_tickets=known_tickets,
            )

        if reached:
            self._mark_tp1_reached(group_id)
        return reached

    def _mark_tp1_reached(self, group_id: int) -> None:
        now = utc_now()
        with connect_database(self.database_path) as connection:
            connection.execute(
                """
                UPDATE execution_groups
                SET tp1_reached_at = COALESCE(tp1_reached_at, ?), updated_at = ?
                WHERE id = ?
                """,
                (now, now, group_id),
            ).close()

    def _mark_breakeven_applied(self, group_id: int) -> bool:
        now = utc_now()
        with connect_database(self.database_path) as connection:
            cursor = connection.execute(
                """
                UPDATE execution_groups
                SET breakeven_applied_at = ?, updated_at = ?
                WHERE id = ? AND breakeven_applied_at IS NULL
                """,
                (now, now, group_id),
            )
            try:
                return cursor.rowcount > 0
            finally:
                cursor.close()

    def _record_be_outcome(
        self,
        order: ManagedOrder,
        outcome: ProtectionOutcome,
    ) -> None:
        now = utc_now()
        status = outcome.state
        with connect_database(self.database_path) as connection:
            connection.execute(
                """
                UPDATE execution_orders
                SET be_status = ?,
                    be_requested_at = COALESCE(be_requested_at, ?),
                    be_applied_at = CASE WHEN ? IN (
                        'applied', 'already_protected', 'closed_at_market'
                    ) THEN COALESCE(be_applied_at, ?) ELSE be_applied_at END,
                    be_target_sl = ?,
                    be_confirmed_sl = COALESCE(?, be_confirmed_sl),
                    be_last_error = ?,
                    be_attempts = be_attempts + CASE WHEN ? IN (
                        'applied', 'closed_at_market', 'failed'
                    ) THEN 1 ELSE 0 END,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    status,
                    now,
                    status,
                    now,
                    str(outcome.target_sl) if outcome.target_sl is not None else None,
                    str(outcome.confirmed_sl) if outcome.confirmed_sl is not None else None,
                    outcome.error,
                    status,
                    now,
                    order.order_id,
                ),
            ).close()
        if outcome.state == "failed":
            self._notify_be_failure(order, outcome)
        elif outcome.state == "closed_at_market":
            self._notify_be_emergency(order)

    def _all_open_targets_protected(self, group_id: int) -> bool:
        with connect_database(self.database_path) as connection:
            row = connection.execute(
                """
                SELECT COUNT(*),
                       SUM(CASE WHEN be_status IN (
                           'applied', 'already_protected', 'closed_at_market'
                       ) THEN 1 ELSE 0 END)
                FROM execution_orders
                WHERE execution_group_id = ? AND tp_index > 1
                  AND status IN (
                      'pending_submission', 'pending_active', 'submitted', 'filled'
                  )
                """,
                (group_id,),
            ).fetchone()
        total = int(row[0] or 0)
        protected = int(row[1] or 0)
        return total > 0 and total == protected

    def _notify_group_be(
        self,
        account: MT5Account,
        group_id: int,
        outcomes: list[ProtectionOutcome],
    ) -> None:
        emergency = any(outcome.state == "closed_at_market" for outcome in outcomes)
        if emergency:
            message = (
                "⚠️ PROTEÇÃO APÓS TP1\n\n"
                f"Conta: {account.masked_login}\n"
                "O preço voltou à entrada antes de o Stop ser movido. "
                "As posições restantes foram fechadas a mercado para limitar a exposição.\n\n"
                "O resultado pode variar alguns centavos por spread, comissão e slippage."
            )
            event_type = "breakeven_emergency_close"
        else:
            message = (
                "🛡️ BREAKEVEN CONFIRMADO\n\n"
                f"Conta: {account.masked_login}\n"
                "O TP1 foi atingido e o MetaTrader 5 confirmou a proteção "
                "das posições restantes na entrada."
            )
            event_type = "breakeven_confirmed"
        self._notify_once(group_id, None, event_type, message)

    def _notify_be_failure(
        self,
        order: ManagedOrder,
        outcome: ProtectionOutcome,
    ) -> None:
        message = (
            "🚨 FALHA NA PROTEÇÃO DE BREAKEVEN\n\n"
            f"Ordem TP{order.tp_index}\n"
            f"Motivo técnico: {outcome.error or 'nao_confirmado'}\n\n"
            "Recomendação: abra o MetaTrader 5 e proteja ou encerre a posição manualmente. "
            "O sistema continuará tentando enquanto a posição permanecer aberta."
        )
        self._notify_once(
            order.group_id,
            order.order_id,
            "breakeven_failed",
            message,
        )

    def _notify_be_emergency(self, order: ManagedOrder) -> None:
        self._notify_once(
            order.group_id,
            order.order_id,
            "breakeven_emergency_close",
            (
                "⚠️ PROTEÇÃO APÓS TP1\n\n"
                f"Ordem TP{order.tp_index}\n"
                "O preço voltou à entrada antes de o Stop ser confirmado. "
                "A posição foi fechada a mercado para limitar a exposição.\n\n"
                "O resultado pode variar por spread, comissão e slippage."
            ),
        )

    def _notify_once(
        self,
        group_id: int,
        order_id: int | None,
        event_type: str,
        message: str,
    ) -> None:
        if self.notifier is None:
            return
        with connect_database(self.database_path) as connection:
            row = connection.execute(
                "SELECT telegram_user_id FROM users WHERE id = "
                "(SELECT user_id FROM execution_groups WHERE id = ?)",
                (group_id,),
            ).fetchone()
            if row is None:
                return
            exists = connection.execute(
                """
                SELECT 1 FROM execution_notifications
                WHERE event_type = ? AND execution_group_id = ?
                  AND execution_order_id IS ?
                LIMIT 1
                """,
                (event_type, group_id, order_id),
            ).fetchone()
            if exists is not None:
                return
            cursor = connection.execute(
                """
                INSERT INTO execution_notifications (
                    event_type, execution_group_id, execution_order_id, created_at
                ) VALUES (?, ?, ?, ?)
                """,
                (event_type, group_id, order_id, utc_now()),
            )
            inserted = cursor.rowcount > 0
            cursor.close()
        if not inserted:
            return
        if not self.notifier.send(int(row[0]), message):
            with connect_database(self.database_path) as connection:
                connection.execute(
                    """
                    DELETE FROM execution_notifications
                    WHERE event_type = ? AND execution_group_id = ?
                      AND execution_order_id IS ?
                    """,
                    (event_type, group_id, order_id),
                ).close()

    def _find_order(
        self, account_id: int, signal_prefix: str, tp_index: int
    ) -> ManagedOrder | None:
        with connect_database(self.database_path) as connection:
            cursor = connection.execute(
                """
                SELECT o.id, g.id, o.tp_index, g.direction, o.stop_loss,
                       o.take_profit, g.expiration_at
                FROM execution_orders o
                JOIN execution_groups g ON g.id = o.execution_group_id
                WHERE g.mt5_account_id = ?
                  AND substr(g.signal_id, 1, 8) = ?
                  AND o.tp_index = ?
                ORDER BY o.id DESC LIMIT 1
                """,
                (account_id, signal_prefix, tp_index),
            )
            try:
                row = cursor.fetchone()
            finally:
                cursor.close()
        if row is None:
            return None
        return ManagedOrder(
            order_id=int(row[0]),
            group_id=int(row[1]),
            tp_index=int(row[2]),
            direction=str(row[3]),
            original_stop=Decimal(str(row[4])),
            take_profit=Decimal(str(row[5])),
            expiration_at=str(row[6]),
        )

    def _find_order_for_item(
        self,
        account_id: int,
        item: object,
    ) -> ManagedOrder | None:
        tickets = {
            str(candidate)
            for candidate in (
                value(item, "ticket", ""),
                value(item, "order", ""),
                value(item, "identifier", ""),
            )
            if candidate not in (None, "", 0)
        }
        if tickets:
            placeholders = ",".join("?" for _ in tickets)
            parameters: list[object] = [account_id, *tickets, *tickets]
            with connect_database(self.database_path) as connection:
                row = connection.execute(
                    f"""
                    SELECT o.id, g.id, o.tp_index, g.direction, o.stop_loss,
                           o.take_profit, g.expiration_at
                    FROM execution_orders o
                    JOIN execution_groups g ON g.id = o.execution_group_id
                    WHERE g.mt5_account_id = ? AND (
                        o.mt5_position_ticket IN ({placeholders}) OR
                        o.mt5_order_ticket IN ({placeholders})
                    )
                    ORDER BY o.id DESC LIMIT 1
                    """,
                    parameters,
                ).fetchone()
            if row is not None:
                return ManagedOrder(
                    order_id=int(row[0]),
                    group_id=int(row[1]),
                    tp_index=int(row[2]),
                    direction=str(row[3]),
                    original_stop=Decimal(str(row[4])),
                    take_profit=Decimal(str(row[5])),
                    expiration_at=str(row[6]),
                )
        parsed_comment = parse_trade_comment(str(value(item, "comment", "")))
        if parsed_comment is None:
            return None
        return self._find_order(
            account_id,
            parsed_comment.signal_prefix,
            parsed_comment.tp_index,
        )

    def _mark_filled(self, order_id: int, position_ticket: int) -> None:
        with connect_database(self.database_path) as connection:
            cursor = connection.execute(
                """
                UPDATE execution_orders
                SET status = 'filled', mt5_position_ticket = ?, filled_at = COALESCE(filled_at, ?), updated_at = ?
                WHERE id = ?
                """,
                (str(position_ticket), utc_now(), utc_now(), order_id),
            )
            cursor.close()

    def _mark_cancelled(self, order_id: int, group_id: int) -> None:
        with connect_database(self.database_path) as connection:
            cursor = connection.execute(
                "UPDATE execution_orders SET status = 'cancelled', updated_at = ? WHERE id = ?",
                (utc_now(), order_id),
            )
            cursor.close()
            cursor = connection.execute(
                """
                UPDATE execution_groups SET status = 'cancelled', updated_at = ?
                WHERE id = ? AND NOT EXISTS (
                    SELECT 1 FROM execution_orders
                    WHERE execution_group_id = ? AND status NOT IN ('cancelled', 'expired', 'failed')
                )
                """,
                (utc_now(), group_id, group_id),
            )
            cursor.close()

    def _mark_expired(self, order_id: int, group_id: int) -> None:
        with connect_database(self.database_path) as connection:
            cursor = connection.execute(
                "UPDATE execution_orders SET status = 'expired', updated_at = ? WHERE id = ?",
                (utc_now(), order_id),
            )
            cursor.close()
            cursor = connection.execute(
                """
                UPDATE execution_groups SET status = 'expired', updated_at = ?
                WHERE id = ? AND NOT EXISTS (
                    SELECT 1 FROM execution_orders
                    WHERE execution_group_id = ? AND status NOT IN ('cancelled', 'expired', 'failed')
                )
                """,
                (utc_now(), group_id, group_id),
            )
            cursor.close()


def value(item: object, field: str, default: object) -> object:
    if isinstance(item, dict):
        return item.get(field, default)
    return getattr(item, field, default)


def normalize_price(price: Decimal, tick_size: Decimal, digits: int) -> Decimal:
    ticks = (price / tick_size).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return (ticks * tick_size).quantize(Decimal("1").scaleb(-digits))


def price_reached_target(
    direction: str,
    price: Decimal,
    target: Decimal,
    tolerance: Decimal = Decimal("0"),
) -> bool:
    if direction == "BUY":
        return price >= target - tolerance
    return price <= target + tolerance


def tp1_reached_in_history(
    *,
    client: object,
    history_deals: tuple[object, ...],
    direction: str,
    signal_prefix: str,
    take_profit: Decimal,
    tolerance: Decimal,
    known_tickets: set[str],
) -> bool:
    tp1_position_ids: set[str] = set()
    identified_deals: list[object] = []

    for deal in history_deals:
        deal_tickets = ticket_values(deal)
        comment_match = parse_trade_comment(str(value(deal, "comment", "")))
        belongs_to_tp1 = bool(known_tickets.intersection(deal_tickets)) or bool(
            comment_match
            and comment_match.signal_prefix == signal_prefix
            and comment_match.tp_index == 1
        )
        if not belongs_to_tp1:
            continue
        identified_deals.append(deal)
        tp1_position_ids.update(position_values(deal))
        if deal_reached_target(deal, direction, take_profit, tolerance):
            return True

    take_profit_reason = mt5_constant(client, "DEAL_REASON_TP", 5)
    for deal in history_deals:
        shares_position = bool(tp1_position_ids.intersection(position_values(deal)))
        if not shares_position and deal not in identified_deals:
            continue
        try:
            reason = int(value(deal, "reason", -1))
        except (TypeError, ValueError):
            reason = -1
        if reason == take_profit_reason:
            return True
        if deal_reached_target(deal, direction, take_profit, tolerance):
            return True
    return False


def ticket_values(item: object) -> set[str]:
    return {
        str(candidate)
        for field in ("position_id", "position", "order", "ticket")
        if (candidate := value(item, field, "")) not in ("", None, 0)
    }


def position_values(item: object) -> set[str]:
    return {
        str(candidate)
        for field in ("position_id", "position")
        if (candidate := value(item, field, "")) not in ("", None, 0)
    }


def deal_reached_target(
    deal: object,
    direction: str,
    take_profit: Decimal,
    tolerance: Decimal,
) -> bool:
    try:
        deal_price = Decimal(str(value(deal, "price", "0")))
    except Exception:
        return False
    return price_reached_target(
        direction,
        deal_price,
        take_profit,
        tolerance,
    )
