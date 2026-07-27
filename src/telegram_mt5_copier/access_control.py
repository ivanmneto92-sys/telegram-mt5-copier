from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .database import connect_database, initialize_database


ACCESS_ALLOWED = "allowed"
ACCESS_AWAITING_APPROVAL = "awaiting_admin_approval"
ACCESS_PAYMENT_PENDING = "payment_pending"
ACCESS_EXPIRATION_MISSING = "expiration_missing"
ACCESS_EXPIRED = "access_expired"


@dataclass(frozen=True)
class AccessDecision:
    allowed: bool
    reason: str
    expires_on: str | None
    amount_paid: Decimal | None


def paid_access_decision(
    database_path: Path,
    user_id: int,
    *,
    today: date | None = None,
) -> AccessDecision:
    initialize_database(database_path)
    with connect_database(database_path) as connection:
        row = connection.execute(
            """
            SELECT b.billing_status, b.due_date, p.amount
            FROM customer_billing b
            LEFT JOIN customer_payments p ON p.id = (
                SELECT p2.id
                FROM customer_payments p2
                WHERE p2.user_id = b.user_id AND p2.status = 'paid'
                ORDER BY p2.paid_at DESC, p2.id DESC
                LIMIT 1
            )
            WHERE b.user_id = ?
            """,
            (user_id,),
        ).fetchone()
    if row is None or row[2] is None:
        return AccessDecision(False, ACCESS_AWAITING_APPROVAL, None, None)

    amount = decimal_or_none(row[2])
    expires_on = str(row[1]) if row[1] else None
    if amount is None or amount <= 0:
        return AccessDecision(False, ACCESS_PAYMENT_PENDING, expires_on, amount)
    if str(row[0]) != "paid":
        return AccessDecision(False, ACCESS_PAYMENT_PENDING, expires_on, amount)
    if not expires_on:
        return AccessDecision(False, ACCESS_EXPIRATION_MISSING, None, amount)
    try:
        expiration = date.fromisoformat(expires_on)
    except ValueError:
        return AccessDecision(False, ACCESS_EXPIRATION_MISSING, expires_on, amount)
    if expiration < (today or date.today()):
        return AccessDecision(False, ACCESS_EXPIRED, expires_on, amount)
    return AccessDecision(True, ACCESS_ALLOWED, expires_on, amount)


def decimal_or_none(value: object) -> Decimal | None:
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
