from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from .models import (
    ACCOUNT_MODE_NETTING,
    ACCOUNT_TYPE_DEMO,
    ACCOUNT_TYPE_REAL,
    CONNECTION_STATUS_CONNECTED,
    MT5Account,
    PendingOrderPlan,
    SymbolInfo,
    TickInfo,
)
from .volume_allocator import validate_volume


class OrderValidationError(ValueError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def validate_pending_order_plan(
    *,
    plan: PendingOrderPlan,
    account: MT5Account,
    symbol_info: SymbolInfo,
    tick: TickInfo,
    execution_mode: str,
    global_kill_switch: bool = False,
) -> None:
    if global_kill_switch and execution_mode != "simulation":
        raise OrderValidationError("kill_switch_enabled")
    if account.connection_status != CONNECTION_STATUS_CONNECTED:
        raise OrderValidationError("account_disconnected")
    if account.account_type == ACCOUNT_TYPE_REAL:
        raise OrderValidationError("real_account_blocked")
    if account.account_type != ACCOUNT_TYPE_DEMO:
        raise OrderValidationError("only_demo_accounts_supported")
    if account.account_mode == ACCOUNT_MODE_NETTING and execution_mode != "simulation" and len(plan.orders) > 1:
        raise OrderValidationError("netting_multiple_tps_not_supported")
    if not plan.orders:
        raise OrderValidationError("missing_orders")
    if not symbol_info.trade_allowed:
        raise OrderValidationError("symbol_trade_disabled")
    expiration = datetime.fromisoformat(plan.expiration_at)
    if expiration <= datetime.now(tz=timezone.utc):
        raise OrderValidationError("order_expired")

    for order in plan.orders:
        validate_volume(order.normalized_volume, symbol_info)
        validate_order_prices(plan.direction, order.entry_price, order.stop_loss, order.take_profit)

    current_price = tick.ask if plan.direction == "BUY" else tick.bid
    if plan.direction == "BUY":
        if current_price <= plan.stop_loss:
            raise OrderValidationError("price_hit_sl_before_entry")
        if current_price >= min(order.take_profit for order in plan.orders):
            raise OrderValidationError("price_hit_tp_before_entry")
    else:
        if current_price >= plan.stop_loss:
            raise OrderValidationError("price_hit_sl_before_entry")
        if current_price <= max(order.take_profit for order in plan.orders):
            raise OrderValidationError("price_hit_tp_before_entry")


def validate_order_prices(direction: str, entry_price: Decimal, stop_loss: Decimal, take_profit: Decimal) -> None:
    if direction == "BUY":
        if stop_loss >= entry_price:
            raise OrderValidationError("buy_stop_loss_not_below_entry")
        if take_profit <= entry_price:
            raise OrderValidationError("buy_take_profit_not_above_entry")
        return

    if stop_loss <= entry_price:
        raise OrderValidationError("sell_stop_loss_not_above_entry")
    if take_profit >= entry_price:
        raise OrderValidationError("sell_take_profit_not_below_entry")
