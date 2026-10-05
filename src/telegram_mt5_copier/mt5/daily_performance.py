from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


DEFAULT_BROKER_TIMEZONE = "Europe/Athens"


@dataclass(frozen=True)
class DailyPerformance:
    performance_date: str
    realized_profit: Decimal
    starting_balance: Decimal | None
    return_percent: Decimal | None
    updated_at: str
    gross_profit: Decimal | None = None
    trading_costs: Decimal | None = None


def calculate_daily_performance(
    client: object,
    balance: Decimal | None,
    *,
    now: datetime | None = None,
    timezone_name: str = DEFAULT_BROKER_TIMEZONE,
    utc_offset_hours: int | None = None,
) -> DailyPerformance:
    current = now or datetime.now(tz=timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    current = current.astimezone(timezone.utc)
    report_timezone = performance_timezone(timezone_name, utc_offset_hours)
    local_now = current.astimezone(report_timezone)
    local_day_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    day_start = local_day_start.astimezone(timezone.utc)

    buy_type = client_constant(client, "DEAL_TYPE_BUY", 0)
    sell_type = client_constant(client, "DEAL_TYPE_SELL", 1)
    gross_profit = Decimal("0")
    trading_costs = Decimal("0")
    for deal in client.history_deals_get(day_start, current):
        deal_type = field_value(deal, "type", None)
        if deal_type is not None and int(deal_type) not in {buy_type, sell_type}:
            continue
        gross_profit += Decimal(str(field_value(deal, "profit", 0) or 0))
        for field_name in ("commission", "swap", "fee"):
            trading_costs += Decimal(str(field_value(deal, field_name, 0) or 0))

    realized_profit = gross_profit + trading_costs

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
        gross_profit=gross_profit,
        trading_costs=trading_costs,
    )


def calculate_history_performance(
    client: object,
    balance: Decimal | None,
    *,
    days: int = 180,
    now: datetime | None = None,
    timezone_name: str = DEFAULT_BROKER_TIMEZONE,
    utc_offset_hours: int | None = None,
) -> list[DailyPerformance]:
    """Resultado por dia ja encerrado (antes de hoje) a partir do historico de
    deals do MT5 -- usado uma vez ao conectar a conta, pra o calendario ja
    mostrar o passado. So devolve dias com negociacao.

    O saldo inicial de cada dia e reconstruido de tras pra frente a partir do
    saldo atual: saldo_inicio(D) = saldo_fim(D) - resultado(D) - depositos/
    saques(D), e saldo_fim(D) = saldo_inicio(D+1).
    """
    current = now or datetime.now(tz=timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    current = current.astimezone(timezone.utc)
    report_timezone = performance_timezone(timezone_name, utc_offset_hours)
    local_now = current.astimezone(report_timezone)
    today = local_now.date()
    range_start = (
        local_now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=days)
    ).astimezone(timezone.utc)

    buy_type = client_constant(client, "DEAL_TYPE_BUY", 0)
    sell_type = client_constant(client, "DEAL_TYPE_SELL", 1)
    balance_type = client_constant(client, "DEAL_TYPE_BALANCE", 2)
    gross: dict[str, Decimal] = {}
    costs: dict[str, Decimal] = {}
    cash_flow: dict[str, Decimal] = {}
    for deal in client.history_deals_get(range_start, current) or ():
        deal_time = field_value(deal, "time", None)
        if deal_time is None:
            continue
        day = (
            datetime.fromtimestamp(int(deal_time), tz=timezone.utc)
            .astimezone(report_timezone)
            .date()
            .isoformat()
        )
        deal_type = field_value(deal, "type", None)
        profit = Decimal(str(field_value(deal, "profit", 0) or 0))
        if deal_type is not None and int(deal_type) == balance_type:
            cash_flow[day] = cash_flow.get(day, Decimal("0")) + profit
            continue
        if deal_type is not None and int(deal_type) not in {buy_type, sell_type}:
            continue
        gross[day] = gross.get(day, Decimal("0")) + profit
        day_costs = costs.get(day, Decimal("0"))
        for field_name in ("commission", "swap", "fee"):
            day_costs += Decimal(str(field_value(deal, field_name, 0) or 0))
        costs[day] = day_costs

    results: list[DailyPerformance] = []
    end_balance = balance
    for offset in range(0, days + 1):
        day = (today - timedelta(days=offset)).isoformat()
        realized = gross.get(day, Decimal("0")) + costs.get(day, Decimal("0"))
        start_balance = (
            end_balance - realized - cash_flow.get(day, Decimal("0"))
            if end_balance is not None
            else None
        )
        if offset > 0 and day in gross:
            return_percent = None
            if start_balance is not None and start_balance > 0:
                return_percent = realized * Decimal("100") / start_balance
            results.append(
                DailyPerformance(
                    performance_date=day,
                    realized_profit=realized,
                    starting_balance=start_balance,
                    return_percent=return_percent,
                    updated_at=current.isoformat(),
                    gross_profit=gross[day],
                    trading_costs=costs.get(day, Decimal("0")),
                )
            )
        end_balance = start_balance
    results.reverse()
    return results


def current_performance_date(
    *,
    now: datetime | None = None,
    timezone_name: str = DEFAULT_BROKER_TIMEZONE,
    utc_offset_hours: int | None = None,
) -> str:
    current = now or datetime.now(tz=timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    report_timezone = performance_timezone(timezone_name, utc_offset_hours)
    return current.astimezone(report_timezone).date().isoformat()


def performance_timezone(
    timezone_name: str,
    utc_offset_hours: int | None = None,
) -> timezone | ZoneInfo:
    if utc_offset_hours is not None:
        return timezone(timedelta(hours=utc_offset_hours))
    try:
        return ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"Fuso horario de desempenho invalido: {timezone_name}") from exc


def field_value(item: object, name: str, default: Any = None) -> Any:
    if isinstance(item, dict):
        return item.get(name, default)
    return getattr(item, name, default)


def client_constant(client: object, name: str, default: int) -> int:
    constant = getattr(client, "constant", None)
    if callable(constant):
        return int(constant(name, default))
    return default
