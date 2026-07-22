from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
import re
from typing import Callable

from ..database import connect_database, utc_now
from .account_service import MT5AccountService
from .models import ExecutionProfile, MT5Account
from .ipc_lock import MT5OperationLock
from .pending_order_executor import MT5_MAGIC_NUMBER, is_successful_mt5_result, mt5_constant

COMMENT_RE = re.compile(r"^tgcp (?P<signal>[0-9a-f]{8}) TP(?P<tp>\d+)$")


class PositionManager:
    def __init__(
        self,
        database_path: Path,
        accounts: MT5AccountService,
        client_factory: Callable[[], object],
    ) -> None:
        self.database_path = database_path
        self.accounts = accounts
        self.client_factory = client_factory

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
                order_id, _group_id, direction, original_stop, _take_profit = order_record
                ticket = int(value(position, "ticket", 0) or 0)
                self._mark_filled(order_id, ticket)
                if not (profile.breakeven_enabled or profile.trailing_enabled):
                    continue
                if self._protect_position(client, position, direction, original_stop, profile):
                    changed += 1
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
                order_id, group_id, direction, stop_loss, take_profit = order_record
                symbol = str(value(pending_order, "symbol", ""))
                tick = client.symbol_info_tick(symbol)
                if tick is None:
                    continue
                current_price = tick.bid if direction == "BUY" else tick.ask
                invalidated = (
                    direction == "BUY" and (current_price <= stop_loss or current_price >= take_profit)
                ) or (
                    direction == "SELL" and (current_price >= stop_loss or current_price <= take_profit)
                )
                if not invalidated:
                    continue
                ticket = int(value(pending_order, "ticket", value(pending_order, "order", 0)) or 0)
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
        if favorable_move < initial_risk:
            return False

        proposed_sl = entry
        if profile.trailing_enabled:
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
        result = client.order_send(
            {
                "action": mt5_constant(client, "TRADE_ACTION_SLTP", 6),
                "position": int(value(position, "ticket", 0)),
                "symbol": symbol,
                "sl": float(proposed_sl),
                "tp": float(take_profit),
                "magic": MT5_MAGIC_NUMBER,
                "comment": "tgcp protection",
            }
        )
        return is_successful_mt5_result(client, result, check=False)

    def _find_order(
        self, account_id: int, signal_prefix: str, tp_index: int
    ) -> tuple[int, int, str, Decimal, Decimal] | None:
        with connect_database(self.database_path) as connection:
            cursor = connection.execute(
                """
                SELECT o.id, g.id, g.direction, o.stop_loss, o.take_profit
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
        return int(row[0]), int(row[1]), str(row[2]), Decimal(str(row[3])), Decimal(str(row[4]))

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


def value(item: object, field: str, default: object) -> object:
    if isinstance(item, dict):
        return item.get(field, default)
    return getattr(item, field, default)


def normalize_price(price: Decimal, tick_size: Decimal, digits: int) -> Decimal:
    ticks = (price / tick_size).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return (ticks * tick_size).quantize(Decimal("1").scaleb(-digits))
