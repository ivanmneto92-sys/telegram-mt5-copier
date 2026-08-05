from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
import logging
from pathlib import Path
import re
import time
from typing import Callable

from ..database import connect_database, utc_now
from .account_service import MT5AccountService
from .models import ExecutionProfile, MT5Account
from .ipc_lock import MT5OperationLock
from .pending_order_executor import (
    MT5_MAGIC_NUMBER,
    is_successful_mt5_result,
    mt5_constant,
    mt5_failure_message,
)
from .settlement_monitor import SettlementMonitor

COMMENT_RE = re.compile(r"^tgcp (?P<signal>[0-9a-f]{8}) TP(?P<tp>\d+)$")
LOGGER = logging.getLogger(__name__)


class PositionManager:
    def __init__(
        self,
        database_path: Path,
        accounts: MT5AccountService,
        client_factory: Callable[[], object],
        settlement_monitor: SettlementMonitor | None = None,
    ) -> None:
        self.database_path = database_path
        self.accounts = accounts
        self.client_factory = client_factory
        self.settlement_monitor = settlement_monitor
        self._last_settlement_check: dict[int, float] = {}

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
            managed_positions: list[tuple[object, tuple[int, int, int, str, Decimal, Decimal, str]]] = []
            for position in client.positions_get():
                if int(value(position, "magic", 0) or 0) != MT5_MAGIC_NUMBER:
                    continue
                match = COMMENT_RE.match(str(value(position, "comment", "")))
                if match is None:
                    continue
                order_record = self._find_order(
                    account.id,
                    match.group("signal"),
                    int(match.group("tp")),
                )
                if order_record is None:
                    continue
                order_id, _group_id, _tp_index, _direction, _original_stop, _take_profit, _expiration_at = order_record
                ticket = int(value(position, "ticket", 0) or 0)
                self._mark_filled(order_id, ticket)
                managed_positions.append((position, order_record))

            history_deals: tuple[object, ...] = ()
            if managed_positions:
                now = datetime.now(tz=timezone.utc)
                history_deals = client.history_deals_get(now - timedelta(days=7), now)
            managed_group_ids = {
                order_record[1] for _position, order_record in managed_positions
            }
            tp1_reached_groups = {
                group_id
                for group_id in managed_group_ids
                if self._tp1_reached(client, group_id, history_deals)
            }
            breakeven_changed_groups: set[int] = set()
            for position, order_record in managed_positions:
                _order_id, group_id, tp_index, direction, original_stop, _take_profit, _expiration_at = order_record
                force_breakeven = (
                    profile.tp1_breakeven_enabled
                    and group_id in tp1_reached_groups
                    and tp_index > 1
                )
                if not force_breakeven and not (
                    profile.breakeven_enabled or profile.trailing_enabled
                ):
                    continue
                if self._protect_position(
                    client,
                    position,
                    direction,
                    original_stop,
                    profile,
                    force_breakeven=force_breakeven,
                ):
                    changed += 1
                    if force_breakeven:
                        breakeven_changed_groups.add(group_id)
            for group_id in breakeven_changed_groups:
                self._mark_breakeven_applied(group_id)

            for pending_order in client.orders_get():
                if int(value(pending_order, "magic", 0) or 0) != MT5_MAGIC_NUMBER:
                    continue
                match = COMMENT_RE.match(str(value(pending_order, "comment", "")))
                if match is None:
                    continue
                order_record = self._find_order(
                    account.id,
                    match.group("signal"),
                    int(match.group("tp")),
                )
                if order_record is None:
                    continue
                order_id, group_id, _tp_index, direction, stop_loss, take_profit, expiration_at = order_record
                symbol = str(value(pending_order, "symbol", ""))
                tick = client.symbol_info_tick(symbol)
                if tick is None:
                    continue
                ticket = int(value(pending_order, "ticket", value(pending_order, "order", 0)) or 0)
                if group_id in tp1_reached_groups:
                    result = client.order_send(
                        {
                            "action": mt5_constant(client, "TRADE_ACTION_REMOVE", 8),
                            "order": ticket,
                            "magic": MT5_MAGIC_NUMBER,
                            "comment": "tgcp tp1 reached",
                        }
                    )
                    if is_successful_mt5_result(client, result, check=False):
                        self._mark_cancelled(order_id, group_id)
                        changed += 1
                    continue
                if datetime.fromisoformat(expiration_at) <= datetime.now(tz=timezone.utc):
                    result = client.order_send(
                        {
                            "action": mt5_constant(client, "TRADE_ACTION_REMOVE", 8),
                            "order": ticket,
                            "magic": MT5_MAGIC_NUMBER,
                            "comment": "tgcp expired",
                        }
                    )
                    if is_successful_mt5_result(client, result, check=False):
                        self._mark_expired(order_id, group_id)
                        changed += 1
                    continue
                current_price = tick.bid if direction == "BUY" else tick.ask
                invalidated = (
                    direction == "BUY" and (current_price <= stop_loss or current_price >= take_profit)
                ) or (
                    direction == "SELL" and (current_price >= stop_loss or current_price <= take_profit)
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
                    self._mark_cancelled(order_id, group_id)
                    changed += 1
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

    def _protect_position(
        self,
        client: object,
        position: object,
        direction: str,
        original_stop: Decimal,
        profile: ExecutionProfile,
        *,
        force_breakeven: bool = False,
    ) -> bool:
        symbol = str(value(position, "symbol", ""))
        info = client.symbol_info(symbol)
        tick = client.symbol_info_tick(symbol)
        if info is None or tick is None:
            return False
        entry = Decimal(str(value(position, "price_open", 0)))
        current_sl = Decimal(str(value(position, "sl", 0) or 0))
        take_profit = Decimal(str(value(position, "tp", 0) or 0))
        initial_risk = abs(entry - original_stop)
        if entry <= 0 or initial_risk <= 0:
            return False

        current_price = tick.bid if direction == "BUY" else tick.ask
        favorable_move = current_price - entry if direction == "BUY" else entry - current_price
        if not force_breakeven and favorable_move < initial_risk:
            return False
        if force_breakeven and (
            (direction == "BUY" and current_price <= entry)
            or (direction == "SELL" and current_price >= entry)
        ):
            return False

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
            return False
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
            LOGGER.warning(
                "Protecao MT5 rejeitada. position=%s symbol=%s sl=%s motivo=%s last_error=%s",
                request["position"],
                symbol,
                proposed_sl,
                mt5_failure_message(client, result),
                client.last_error(),
            )
        elif force_breakeven:
            LOGGER.info(
                "BE apos TP1 aplicado. position=%s symbol=%s sl=%s",
                request["position"],
                symbol,
                proposed_sl,
            )
        return successful

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

    def _mark_breakeven_applied(self, group_id: int) -> None:
        now = utc_now()
        with connect_database(self.database_path) as connection:
            connection.execute(
                """
                UPDATE execution_groups
                SET breakeven_applied_at = COALESCE(breakeven_applied_at, ?),
                    updated_at = ?
                WHERE id = ?
                """,
                (now, now, group_id),
            ).close()

    def _find_order(
        self, account_id: int, signal_prefix: str, tp_index: int
    ) -> tuple[int, int, int, str, Decimal, Decimal, str] | None:
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
        return (
            int(row[0]),
            int(row[1]),
            int(row[2]),
            str(row[3]),
            Decimal(str(row[4])),
            Decimal(str(row[5])),
            str(row[6]),
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
        comment_match = COMMENT_RE.match(str(value(deal, "comment", "")))
        belongs_to_tp1 = bool(known_tickets.intersection(deal_tickets)) or bool(
            comment_match
            and comment_match.group("signal") == signal_prefix
            and int(comment_match.group("tp")) == 1
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
