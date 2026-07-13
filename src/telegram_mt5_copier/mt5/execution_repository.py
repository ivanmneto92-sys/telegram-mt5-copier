from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from ..database import connect_database, initialize_database, utc_now
from ..models import decimal_to_text
from .models import (
    ExecutionGroup,
    ExecutionOrder,
    GROUP_STATUS_SIMULATED,
    LatencyMetrics,
    ORDER_STATUS_SIMULATED,
    PendingOrderPlan,
)


class ExecutionRepository:
    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path
        self._closed = False
        initialize_database(database_path)

    def close(self) -> None:
        self._closed = True

    def __enter__(self) -> "ExecutionRepository":
        return self

    def __exit__(self, _exc_type: object, _exc: object, _traceback: object) -> None:
        self.close()

    def has_execution_group(self, signal_id: str, user_id: int, account_id: int) -> bool:
        with connect_database(self.database_path) as connection:
            cursor = connection.execute(
                """
                SELECT 1 FROM execution_groups
                WHERE signal_id = ? AND user_id = ? AND mt5_account_id = ?
                LIMIT 1
                """,
                (signal_id, user_id, account_id),
            )
            try:
                return cursor.fetchone() is not None
            finally:
                cursor.close()

    def create_group_with_orders(
        self,
        plan: PendingOrderPlan,
        *,
        status: str = GROUP_STATUS_SIMULATED,
        order_status: str = ORDER_STATUS_SIMULATED,
        metrics: LatencyMetrics | None = None,
    ) -> tuple[ExecutionGroup, tuple[ExecutionOrder, ...]]:
        metrics = metrics or LatencyMetrics()
        now = utc_now()
        with connect_database(self.database_path) as connection:
            cursor = connection.execute(
                """
                INSERT INTO execution_groups (
                    signal_id, user_id, mt5_account_id, status, direction, symbol,
                    entry_low, entry_high, selected_entry_price, order_type,
                    total_volume, stop_loss, expiration_at, execution_mode,
                    signal_received_at, pending_created_at,
                    telegram_message_at, listener_received_at, signal_parsed_at,
                    execution_planned_at, validation_started_at, validation_finished_at,
                    mt5_order_sent_at, broker_response_at,
                    telegram_to_listener_ms, parsing_ms, planning_ms, validation_ms,
                    mt5_submission_ms, broker_response_ms, total_ms,
                    created_at, updated_at, error_code, error_message
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    plan.signal_id,
                    plan.user_id,
                    plan.mt5_account_id,
                    status,
                    plan.direction,
                    plan.symbol,
                    decimal_to_text(plan.entry_low),
                    decimal_to_text(plan.entry_high),
                    decimal_to_text(plan.selected_entry_price),
                    plan.order_type.value,
                    decimal_to_text(plan.total_volume),
                    decimal_to_text(plan.stop_loss),
                    plan.expiration_at,
                    plan.execution_mode,
                    plan.signal_received_at,
                    plan.pending_created_at,
                    metrics.telegram_message_at,
                    metrics.listener_received_at,
                    metrics.signal_parsed_at,
                    metrics.execution_planned_at,
                    metrics.validation_started_at,
                    metrics.validation_finished_at,
                    metrics.mt5_order_sent_at,
                    metrics.broker_response_at,
                    metrics.telegram_to_listener_ms,
                    metrics.parsing_ms,
                    metrics.planning_ms,
                    metrics.validation_ms,
                    metrics.mt5_submission_ms,
                    metrics.broker_response_ms,
                    metrics.total_ms,
                    now,
                    now,
                    None,
                    None,
                ),
            )
            try:
                group_id = int(cursor.lastrowid)
            finally:
                cursor.close()

            orders: list[ExecutionOrder] = []
            for order in plan.orders:
                cursor = connection.execute(
                    """
                    INSERT INTO execution_orders (
                        execution_group_id, tp_index, requested_volume, normalized_volume,
                        entry_price, stop_loss, take_profit, order_type, status,
                        mt5_order_ticket, mt5_position_ticket, broker_retcode, broker_message,
                        submitted_at, filled_at, closed_at, created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        group_id,
                        order.tp_index,
                        decimal_to_text(order.requested_volume),
                        decimal_to_text(order.normalized_volume),
                        decimal_to_text(order.entry_price),
                        decimal_to_text(order.stop_loss),
                        decimal_to_text(order.take_profit),
                        order.order_type.value,
                        order_status,
                        None,
                        None,
                        None,
                        None,
                        now if order_status == ORDER_STATUS_SIMULATED else None,
                        None,
                        None,
                        now,
                        now,
                    ),
                )
                try:
                    order_id = int(cursor.lastrowid)
                finally:
                    cursor.close()
                orders.append(
                    ExecutionOrder(
                        id=order_id,
                        execution_group_id=group_id,
                        tp_index=order.tp_index,
                        requested_volume=order.requested_volume,
                        normalized_volume=order.normalized_volume,
                        entry_price=order.entry_price,
                        stop_loss=order.stop_loss,
                        take_profit=order.take_profit,
                        order_type=order.order_type.value,
                        status=order_status,
                    )
                )

        return (
            ExecutionGroup(
                id=group_id,
                signal_id=plan.signal_id,
                user_id=plan.user_id,
                mt5_account_id=plan.mt5_account_id,
                status=status,
                direction=plan.direction,
                symbol=plan.symbol,
                entry_low=plan.entry_low,
                entry_high=plan.entry_high,
                selected_entry_price=plan.selected_entry_price,
                order_type=plan.order_type.value,
                total_volume=plan.total_volume,
                stop_loss=plan.stop_loss,
                expiration_at=plan.expiration_at,
                execution_mode=plan.execution_mode,
                error_code=None,
                error_message=None,
            ),
            tuple(orders),
        )

    def mark_group_rejected(
        self,
        plan: PendingOrderPlan,
        *,
        error_code: str,
        error_message: str,
    ) -> ExecutionGroup:
        now = utc_now()
        with connect_database(self.database_path) as connection:
            cursor = connection.execute(
                """
                INSERT INTO execution_groups (
                    signal_id, user_id, mt5_account_id, status, direction, symbol,
                    entry_low, entry_high, selected_entry_price, order_type,
                    total_volume, stop_loss, expiration_at, execution_mode,
                    signal_received_at, pending_created_at, created_at, updated_at,
                    error_code, error_message
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    plan.signal_id,
                    plan.user_id,
                    plan.mt5_account_id,
                    "rejected",
                    plan.direction,
                    plan.symbol,
                    decimal_to_text(plan.entry_low),
                    decimal_to_text(plan.entry_high),
                    decimal_to_text(plan.selected_entry_price),
                    plan.order_type.value,
                    decimal_to_text(plan.total_volume),
                    decimal_to_text(plan.stop_loss),
                    plan.expiration_at,
                    plan.execution_mode,
                    plan.signal_received_at,
                    plan.pending_created_at,
                    now,
                    now,
                    error_code,
                    error_message,
                ),
            )
            try:
                group_id = int(cursor.lastrowid)
            finally:
                cursor.close()
        return ExecutionGroup(
            id=group_id,
            signal_id=plan.signal_id,
            user_id=plan.user_id,
            mt5_account_id=plan.mt5_account_id,
            status="rejected",
            direction=plan.direction,
            symbol=plan.symbol,
            entry_low=plan.entry_low,
            entry_high=plan.entry_high,
            selected_entry_price=plan.selected_entry_price,
            order_type=plan.order_type.value,
            total_volume=plan.total_volume,
            stop_loss=plan.stop_loss,
            expiration_at=plan.expiration_at,
            execution_mode=plan.execution_mode,
            error_code=error_code,
            error_message=error_message,
        )

    def mark_expired_groups(self, now_iso: str) -> int:
        with connect_database(self.database_path) as connection:
            cursor = connection.execute(
                """
                UPDATE execution_groups
                SET status = 'expired', updated_at = ?
                WHERE expiration_at <= ?
                  AND status IN ('planned', 'simulated', 'pending_active', 'pending_submission')
                """,
                (now_iso, now_iso),
            )
            try:
                changed = cursor.rowcount
            finally:
                cursor.close()
            cursor = connection.execute(
                """
                UPDATE execution_orders
                SET status = 'expired', updated_at = ?
                WHERE execution_group_id IN (
                    SELECT id FROM execution_groups WHERE status = 'expired'
                )
                  AND status IN ('planned', 'simulated', 'pending_active', 'pending_submission')
                """,
                (now_iso,),
            )
            cursor.close()
        return int(changed)

    def record_notification_once(
        self,
        *,
        event_type: str,
        execution_group_id: int,
        execution_order_id: int | None = None,
    ) -> bool:
        with connect_database(self.database_path) as connection:
            cursor = connection.execute(
                """
                SELECT 1 FROM execution_notifications
                WHERE event_type = ?
                  AND execution_group_id = ?
                  AND (
                    (? IS NULL AND execution_order_id IS NULL)
                    OR execution_order_id = ?
                  )
                LIMIT 1
                """,
                (event_type, execution_group_id, execution_order_id, execution_order_id),
            )
            try:
                exists = cursor.fetchone() is not None
            finally:
                cursor.close()
            if exists:
                return False
            cursor = connection.execute(
                """
                INSERT INTO execution_notifications (
                    event_type, execution_group_id, execution_order_id, created_at
                )
                VALUES (?, ?, ?, ?)
                """,
                (event_type, execution_group_id, execution_order_id, utc_now()),
            )
            cursor.close()
            return True

    def count_orders_for_signal(self, signal_id: str, user_id: int, account_id: int) -> int:
        with connect_database(self.database_path) as connection:
            cursor = connection.execute(
                """
                SELECT COUNT(*)
                FROM execution_orders o
                JOIN execution_groups g ON g.id = o.execution_group_id
                WHERE g.signal_id = ? AND g.user_id = ? AND g.mt5_account_id = ?
                """,
                (signal_id, user_id, account_id),
            )
            try:
                return int(cursor.fetchone()[0])
            finally:
                cursor.close()


def decimal_from_text(value: object) -> Decimal:
    return Decimal(str(value))
