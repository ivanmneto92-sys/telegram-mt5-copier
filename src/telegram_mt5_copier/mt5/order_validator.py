from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from .models import (
    ACCOUNT_MODE_NETTING,
    ACCOUNT_TYPE_DEMO,
    ACCOUNT_TYPE_REAL,
    CONNECTION_STATUS_CONNECTED,
    ExecutionProfile,
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
    allow_live_accounts: bool = False,
) -> None:
    if global_kill_switch and execution_mode != "simulation":
        raise OrderValidationError("kill_switch_enabled")
    if account.connection_status != CONNECTION_STATUS_CONNECTED:
        raise OrderValidationError("account_disconnected")
    if account.account_type == ACCOUNT_TYPE_REAL and not (
        execution_mode == "live_execution" and allow_live_accounts
    ):
        raise OrderValidationError("real_account_blocked")
    if account.account_type not in {ACCOUNT_TYPE_DEMO, ACCOUNT_TYPE_REAL}:
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
        validate_stops_level(order.entry_price, order.stop_loss, order.take_profit, symbol_info)

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


def validate_daily_result_headroom(
    plan: PendingOrderPlan,
    profile: ExecutionProfile,
    symbol_info: SymbolInfo,
    daily_realized_result: Decimal,
) -> None:
    """Reject a pending order whose own worst-case outcome alone could blow
    past today's profit target or loss limit in a single fill.

    The daily-limit checks elsewhere only look at deals already closed
    before this order was planned. A pending order can sail through that
    check with the daily result still at zero, sit waiting for price, and
    then once it eventually fills — instead of gradually — add most or all
    of its potential profit in one shot, days after being accepted. That
    lets the realized result jump straight past the configured target
    before the kill switch's next check even runs, and by then there is
    nothing left to prevent — only to react to. Comparing today's realized
    result plus this order's own worst-case swing against the configured
    target/limit catches that before the order goes live, rather than only
    closing everything after the overshoot already happened.
    """
    if profile.daily_profit_target <= 0 and profile.daily_loss_limit <= 0:
        return
    if symbol_info.trade_tick_size <= 0:
        return

    potential_profit = Decimal("0")
    potential_loss = Decimal("0")
    for order in plan.orders:
        ticks_to_tp = abs(order.take_profit - order.entry_price) / symbol_info.trade_tick_size
        ticks_to_sl = abs(order.entry_price - order.stop_loss) / symbol_info.trade_tick_size
        potential_profit += order.normalized_volume * ticks_to_tp * symbol_info.trade_tick_value
        potential_loss += order.normalized_volume * ticks_to_sl * symbol_info.trade_tick_value

    if (
        profile.daily_profit_target > 0
        and daily_realized_result + potential_profit >= profile.daily_profit_target
    ):
        raise OrderValidationError("daily_profit_target_would_be_exceeded")
    if (
        profile.daily_loss_limit > 0
        and daily_realized_result - potential_loss <= -profile.daily_loss_limit
    ):
        raise OrderValidationError("daily_loss_limit_would_be_exceeded")


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


def validate_stops_level(
    entry_price: Decimal,
    stop_loss: Decimal,
    take_profit: Decimal,
    symbol_info: SymbolInfo,
) -> None:
    minimum_distance = Decimal(symbol_info.stops_level_points) * symbol_info.point
    if minimum_distance <= 0:
        return
    if abs(entry_price - stop_loss) < minimum_distance:
        raise OrderValidationError("stop_loss_inside_broker_stops_level")
    if abs(take_profit - entry_price) < minimum_distance:
        raise OrderValidationError("take_profit_inside_broker_stops_level")
