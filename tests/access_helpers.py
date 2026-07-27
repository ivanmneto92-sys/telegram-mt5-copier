from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from telegram_mt5_copier.database import connect_database
from telegram_mt5_copier.users import USER_STATUS_ACTIVE, UserRepository


def grant_paid_access(database_path: Path, user_id: int, *, days: int = 30) -> None:
    today = date.today()
    expires_on = (today + timedelta(days=days)).isoformat()
    now = datetime.now(tz=timezone.utc).isoformat()
    with connect_database(database_path) as connection:
        connection.execute(
            """
            INSERT INTO customer_billing (
                user_id, plan_name, monthly_amount, due_date,
                billing_status, last_paid_at, created_at, updated_at
            )
            VALUES (?, 'Teste', '100.00', ?, 'paid', ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                monthly_amount = '100.00',
                due_date = excluded.due_date,
                billing_status = 'paid',
                last_paid_at = excluded.last_paid_at,
                updated_at = excluded.updated_at
            """,
            (user_id, expires_on, today.isoformat(), now, now),
        )
        connection.execute(
            """
            INSERT INTO customer_payments (
                user_id, amount, paid_at, method, status,
                admin_telegram_user_id, created_at
            )
            VALUES (?, '100.00', ?, 'test', 'paid', 9001, ?)
            """,
            (user_id, today.isoformat(), now),
        )
    users = UserRepository(database_path)
    try:
        users.set_status(user_id, USER_STATUS_ACTIVE)
    finally:
        users.close()
