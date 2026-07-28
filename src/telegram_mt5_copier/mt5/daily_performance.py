from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any


BRAZIL_UTC_OFFSET_HOURS = -3


@dataclass(frozen=True)
class DailyPerformance:
    performance_date: str
    realized_profit: Decimal
    starting_balance: Decimal | None
    return_percent: Decimal | None
    updated_at: str


def calculate_daily_performance(
    client: object,
    balance: Decimal | None,
    *,
    now: datetime | None = None,
    utc_offset_hours: int = BRAZIL_UTC_OFFSET_HOURS,
) -> DailyPerformance:
    current = now or datetime.now(tz=timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    current = current.astimezone(timezone.utc)
    report_timezone = timezone(timedelta(hours=utc_offset_hours))
    local_now = current.astimezone(report_timezone)
    local_day_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    day_start = local_day_start.astimezone(timezone.utc)

    buy_type = client_constant(client, "DEAL_TYPE_BUY", 0)
    sell_type = client_constant(client, "DEAL_TYPE_SELL", 1)
    realized_profit = Decimal("0")
    for deal in client.history_deals_get(day_start, current):
        deal_type = field_value(deal, "type", None)
        if deal_type is not None and int(deal_type) not in {buy_type, sell_type}:
            continue
        for field_name in ("profit", "commission", "swap", "fee"):
            realized_profit += Decimal(str(field_value(deal, field_name, 0) or 0))

    starting_balance = balance - realized_profit if balance is not None else None
    return_percent = None
    if starting_balance is not None and starting_balance > 0:
        return_percent = realized_profit * Decimal("100") / starting_balance

    return DailyPerformance(
        performance_date=local_now.date().isoformat(),
        realized_profit=realized_profit,
        starting_balance=starting_balance,
        return_percent=return_percent,
        updated_at=current.isoformat(),
    )


def current_performance_date(
    *,
    now: datetime | None = None,
    utc_offset_hours: int = BRAZIL_UTC_OFFSET_HOURS,
) -> str:
    current = now or datetime.now(tz=timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    report_timezone = timezone(timedelta(hours=utc_offset_hours))
    return current.astimezone(report_timezone).date().isoformat()


def field_value(item: object, name: str, default: Any = None) -> Any:
    if isinstance(item, dict):
        return item.get(name, default)
    return getattr(item, name, default)


def client_constant(client: object, name: str, default: int) -> int:
    constant = getattr(client, "constant", None)
    if callable(constant):
        return int(constant(name, default))
    return default
