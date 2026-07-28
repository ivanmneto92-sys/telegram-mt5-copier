from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP

from ..models import Direction, TradeSignal
from ..database import utc_now
from .models import (
    ENTRY_PRICE_DISTRIBUTED,
    ENTRY_PRICE_FIRST_TOUCH,
    ENTRY_PRICE_MIDDLE,
    ExecutionProfile,
    MT5Account,
    PendingOrderType,
    PendingOrderPlan,
    PlannedOrder,
    SymbolInfo,
    TickInfo,
)
from .order_type_resolver import resolve_order_type
from .volume_allocator import VolumeAllocationError, allocate_volume, validate_volume, volume_for_risk


class PendingOrderPlanningError(ValueError):
    def __init__(self, reason: str, plan: PendingOrderPlan | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.plan = plan


class PendingOrderPlanner:
    def plan(
        self,
        *,
        signal: TradeSignal,
        account: MT5Account,
        profile: ExecutionProfile,
        symbol_info: SymbolInfo,
        tick: TickInfo,
        execution_mode: str,
        now: datetime | None = None,
    ) -> PendingOrderPlan:
        planned_at = now or datetime.now(tz=timezone.utc)
        current_price = price_for_direction(signal.direction, tick)
        selected_take_profits = limit_take_profits(
            signal.take_profits,
            profile.take_profit_limit,
        )
        take_profits = (
            selected_take_profits
            if profile.split_tps
            else (selected_take_profits[-1],)
        )
        market_in_zone = (
            signal.entry_low <= current_price <= signal.entry_high
            and profile.entry_execution_mode == "market_on_zone"
        )
        entry_prices = (
            tuple(normalize_price(current_price, symbol_info) for _ in take_profits)
            if market_in_zone
            else select_entry_prices(
                direction=signal.direction,
                entry_low=signal.entry_low,
                entry_high=signal.entry_high,
                current_price=current_price,
                mode=profile.entry_price_mode,
                count=len(take_profits),
                symbol_info=symbol_info,
            )
        )
        order_types: list[PendingOrderType] = []
        for entry_price in entry_prices:
            resolution = resolve_order_type(
                direction=signal.direction,
                entry_price=entry_price,
                current_price=current_price,
                entry_low=signal.entry_low,
                entry_high=signal.entry_high,
                entry_execution_mode=profile.entry_execution_mode,
            )
            if resolution.market_required:
                order_types.append(
                    PendingOrderType.BUY if signal.direction == Direction.BUY else PendingOrderType.SELL
                )
                continue
            if resolution.order_type is None:
                raise PendingOrderPlanningError("Tipo de ordem pendente nao resolvido.")
            order_types.append(resolution.order_type)

        total_volume = profile.fixed_lot
        try:
            if profile.risk_mode == "risk_percent":
                risk_entry_price = max(
                    entry_prices,
                    key=lambda price: abs(price - signal.stop_loss),
                )
                total_volume = volume_for_risk(
                    equity=account.equity,
                    risk_percent=profile.risk_percent,
                    entry_price=risk_entry_price,
                    stop_loss=signal.stop_loss,
                    symbol_info=symbol_info,
                )
            elif profile.risk_mode != "fixed_lot":
                raise VolumeAllocationError("Modo de risco invalido.")
            volumes = (
                allocate_volume(total_volume, len(take_profits), symbol_info)
                if profile.split_tps
                else (total_volume,)
            )
            if not profile.split_tps:
                validate_volume(total_volume, symbol_info)
        except VolumeAllocationError as exc:
            raise PendingOrderPlanningError(
                str(exc),
                plan=build_plan(
                    signal=signal,
                    account=account,
                    symbol_info=symbol_info,
                    execution_mode=execution_mode,
                    planned_at=planned_at,
                    expiration_minutes=profile.pending_expiration_minutes,
                    selected_entry_price=entry_prices[0],
                    order_type=order_types[0],
                    total_volume=total_volume,
                    orders=(),
                ),
            ) from exc

        orders: list[PlannedOrder] = []
        for index, (take_profit, volume, entry_price, order_type) in enumerate(
            zip(take_profits, volumes, entry_prices, order_types),
            start=1,
        ):
            orders.append(
                PlannedOrder(
                    tp_index=index,
                    requested_volume=volume,
                    normalized_volume=volume,
                    entry_price=entry_price,
                    stop_loss=signal.stop_loss,
                    take_profit=take_profit,
                    order_type=order_type,
                )
            )

        first_order = orders[0]
        return build_plan(
            signal=signal,
            account=account,
            symbol_info=symbol_info,
            execution_mode=execution_mode,
            planned_at=planned_at,
            expiration_minutes=profile.pending_expiration_minutes,
            selected_entry_price=first_order.entry_price,
            order_type=first_order.order_type,
            total_volume=sum((order.normalized_volume for order in orders), Decimal("0")),
            orders=tuple(orders),
        )


def price_for_direction(direction: Direction, tick: TickInfo) -> Decimal:
    return tick.ask if direction == Direction.BUY else tick.bid


def limit_take_profits(
    take_profits: tuple[Decimal, ...],
    limit: int,
) -> tuple[Decimal, ...]:
    if not take_profits:
        raise PendingOrderPlanningError("Sinal sem Take Profit.")
    if limit <= 0:
        return take_profits
    return take_profits[:limit]


def select_entry_prices(
    *,
    direction: Direction,
    entry_low: Decimal,
    entry_high: Decimal,
    current_price: Decimal,
    mode: str,
    count: int,
    symbol_info: SymbolInfo,
) -> tuple[Decimal, ...]:
    if mode == ENTRY_PRICE_MIDDLE:
        price = normalize_price((entry_low + entry_high) / Decimal("2"), symbol_info)
        return tuple(price for _ in range(count))

    if mode == ENTRY_PRICE_DISTRIBUTED:
        return distributed_prices(entry_low, entry_high, count, symbol_info)

    if mode != ENTRY_PRICE_FIRST_TOUCH:
        raise PendingOrderPlanningError("Modo de preco de entrada invalido.")

    if direction == Direction.BUY:
        price = entry_high if current_price > entry_high else entry_low
    else:
        price = entry_low if current_price < entry_low else entry_high
    return tuple(normalize_price(price, symbol_info) for _ in range(count))


def distributed_prices(
    entry_low: Decimal,
    entry_high: Decimal,
    count: int,
    symbol_info: SymbolInfo,
) -> tuple[Decimal, ...]:
    if count < 1:
        raise PendingOrderPlanningError("Quantidade de ordens invalida.")
    if count == 1:
        return (normalize_price((entry_low + entry_high) / Decimal("2"), symbol_info),)

    distance = entry_high - entry_low
    return tuple(
        normalize_price(entry_low + (distance * Decimal(index) / Decimal(count - 1)), symbol_info)
        for index in range(count)
    )


def normalize_price(price: Decimal, symbol_info: SymbolInfo) -> Decimal:
    tick_size = symbol_info.trade_tick_size
    ticks = (price / tick_size).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    normalized = ticks * tick_size
    quantizer = Decimal("1").scaleb(-symbol_info.digits)
    return normalized.quantize(quantizer)


def build_plan(
    *,
    signal: TradeSignal,
    account: MT5Account,
    symbol_info: SymbolInfo,
    execution_mode: str,
    planned_at: datetime,
    expiration_minutes: int,
    selected_entry_price: Decimal,
    order_type: PendingOrderType,
    total_volume: Decimal,
    orders: tuple[PlannedOrder, ...],
) -> PendingOrderPlan:
    expiration_at = planned_at + timedelta(minutes=expiration_minutes)
    return PendingOrderPlan(
        signal_id=signal.signature,
        user_id=account.user_id,
        mt5_account_id=account.id,
        account_mode=account.account_mode,
        direction=signal.direction.value,
        symbol=symbol_info.name,
        entry_low=signal.entry_low,
        entry_high=signal.entry_high,
        selected_entry_price=selected_entry_price,
        order_type=order_type,
        total_volume=total_volume,
        stop_loss=signal.stop_loss,
        expiration_at=expiration_at.isoformat(),
        execution_mode=execution_mode,
        orders=orders,
        signal_received_at=utc_now(),
        pending_created_at=planned_at.isoformat(),
    )
