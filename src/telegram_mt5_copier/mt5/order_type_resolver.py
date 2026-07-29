from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from ..models import Direction
from .models import (
    ENTRY_EXECUTION_MARKET_IMMEDIATE,
    ENTRY_EXECUTION_MARKET_ON_ZONE,
    PendingOrderType,
)


@dataclass(frozen=True)
class OrderTypeResolution:
    order_type: PendingOrderType | None
    market_required: bool = False


def resolve_order_type(
    *,
    direction: Direction,
    entry_price: Decimal,
    current_price: Decimal,
    entry_low: Decimal,
    entry_high: Decimal,
    entry_execution_mode: str,
) -> OrderTypeResolution:
    if entry_execution_mode == ENTRY_EXECUTION_MARKET_IMMEDIATE:
        return OrderTypeResolution(order_type=None, market_required=True)
    if entry_low <= current_price <= entry_high and entry_execution_mode == ENTRY_EXECUTION_MARKET_ON_ZONE:
        return OrderTypeResolution(order_type=None, market_required=True)

    if direction == Direction.BUY:
        if entry_price <= current_price:
            return OrderTypeResolution(PendingOrderType.BUY_LIMIT)
        return OrderTypeResolution(PendingOrderType.BUY_STOP)

    if entry_price >= current_price:
        return OrderTypeResolution(PendingOrderType.SELL_LIMIT)
    return OrderTypeResolution(PendingOrderType.SELL_STOP)
