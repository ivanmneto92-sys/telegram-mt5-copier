"""Fixtures compartilhadas entre test_central_sync.py (unitario) e
test_central_sync_integration.py (contra Postgres local) pro mirror de
execution_jobs/execution_job_orders -- mesmo padrao de tests/access_helpers.py."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from telegram_mt5_copier.database import connect_database
from telegram_mt5_copier.mt5.models import (
    ExecutionGroup,
    ExecutionOrder,
    MT5Account,
    PendingOrderPlan,
    PendingOrderType,
    PlannedOrder,
)
from telegram_mt5_copier.models import TradeSignal


def seed_customer_and_account(
    database_path: Path,
    *,
    telegram_user_id: int = 555001,
    login: str = "9988776655",
) -> tuple[int, int]:
    """Insere users/customer_billing/mt5_accounts minimos localmente, mesmo
    padrao usado em tests/test_client_portal.py -- devolve (user_id, account_id)."""
    now = datetime.now(tz=timezone.utc).isoformat()
    with connect_database(database_path) as connection:
        user_id = int(
            connection.execute(
                """
                INSERT INTO users (telegram_user_id, telegram_username, status, created_at, updated_at)
                VALUES (?, 'cliente_teste', 'active', ?, ?)
                """,
                (telegram_user_id, now, now),
            ).lastrowid
        )
        connection.execute(
            """
            INSERT INTO customer_billing (
                user_id, customer_name, email, phone, plan_name, monthly_amount,
                due_date, billing_status, last_paid_at, created_at, updated_at
            ) VALUES (?, 'Cliente Teste', 'cliente@example.com', '+551199999999', 'Mensal',
                      '150.00', '2099-01-01', 'paid', '2026-01-01', ?, ?)
            """,
            (user_id, now, now),
        ).close()
        account_id = int(
            connection.execute(
                """
                INSERT INTO mt5_accounts (
                    user_id, account_alias, broker_name, terminal_path, server_name,
                    login, encrypted_password, account_type, account_mode,
                    connection_status, created_at, updated_at
                ) VALUES (?, 'Conta Demo', 'XM', 'terminal64.exe', 'XM-Demo',
                          ?, 'encrypted', 'demo', 'hedging', 'connected', ?, ?)
                """,
                (user_id, login, now, now),
            ).lastrowid
        )
    return user_id, account_id


def make_mt5_account(account_id: int, user_id: int, *, login: str = "9988776655") -> MT5Account:
    return MT5Account(
        id=account_id,
        user_id=user_id,
        broker_name="XM",
        server_name="XM-Demo",
        login=login,
        encrypted_password="encrypted",
        account_alias="Conta Demo",
        terminal_path=None,
        account_type="demo",
        account_mode="hedging",
        connection_status="connected",
        last_error=None,
        last_connected_at=None,
    )


def make_execution_group(group_id: int, account: MT5Account, signal: TradeSignal) -> ExecutionGroup:
    return ExecutionGroup(
        id=group_id,
        signal_id=signal.signature,
        user_id=account.user_id,
        mt5_account_id=account.id,
        status="pending_active",
        direction=signal.direction.value,
        symbol=signal.symbol,
        entry_low=signal.entry_low,
        entry_high=signal.entry_high,
        selected_entry_price=signal.entry_low,
        order_type="BUY",
        total_volume=Decimal("0.02"),
        stop_loss=signal.stop_loss,
        expiration_at="2099-01-01T00:00:00+00:00",
        execution_mode="demo_execution",
        error_code=None,
        error_message=None,
    )


def make_pending_order_plan(
    account: MT5Account, signal: TradeSignal, *, tp_indices: tuple[int, ...] = (1, 2)
) -> PendingOrderPlan:
    orders = tuple(
        PlannedOrder(
            tp_index=tp_index,
            requested_volume=Decimal("0.01"),
            normalized_volume=Decimal("0.01"),
            entry_price=signal.entry_low,
            stop_loss=signal.stop_loss,
            take_profit=signal.take_profits[0],
            order_type=PendingOrderType.BUY,
        )
        for tp_index in tp_indices
    )
    return PendingOrderPlan(
        signal_id=signal.signature,
        user_id=account.user_id,
        mt5_account_id=account.id,
        account_mode="hedging",
        direction=signal.direction.value,
        symbol=signal.symbol,
        entry_low=signal.entry_low,
        entry_high=signal.entry_high,
        selected_entry_price=signal.entry_low,
        order_type=PendingOrderType.BUY,
        total_volume=Decimal("0.02"),
        stop_loss=signal.stop_loss,
        expiration_at="2099-01-01T00:00:00+00:00",
        execution_mode="demo_execution",
        orders=orders,
        signal_received_at="2026-01-01T00:00:00+00:00",
        pending_created_at="2026-01-01T00:00:00+00:00",
    )


def make_execution_order(
    group_id: int,
    tp_index: int,
    *,
    status: str = "pending_active",
    mt5_order_ticket: str | None = "123456",
    broker_retcode: str | None = "10009",
    broker_message: str | None = "Request completed",
) -> ExecutionOrder:
    return ExecutionOrder(
        id=tp_index,
        execution_group_id=group_id,
        tp_index=tp_index,
        requested_volume=Decimal("0.01"),
        normalized_volume=Decimal("0.01"),
        entry_price=Decimal("4104"),
        stop_loss=Decimal("4090"),
        take_profit=Decimal("4110"),
        order_type="BUY",
        status=status,
        mt5_order_ticket=mt5_order_ticket,
        mt5_position_ticket=mt5_order_ticket,
        broker_retcode=broker_retcode,
        broker_message=broker_message,
    )
