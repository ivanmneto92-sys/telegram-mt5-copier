from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from pathlib import Path


ACCOUNT_TYPE_DEMO = "demo"
ACCOUNT_TYPE_REAL = "real"
CONNECTION_STATUS_CONNECTED = "connected"
CONNECTION_STATUS_DISCONNECTED = "disconnected"
CONNECTION_STATUS_FAILED = "failed"
EXECUTION_STATUS_SIMULATED = "simulated"
ACCOUNT_MODE_HEDGING = "hedging"
ACCOUNT_MODE_NETTING = "netting"
ENTRY_EXECUTION_PENDING_ORDER = "pending_order"
ENTRY_EXECUTION_MARKET_ON_ZONE = "market_on_zone"
ENTRY_PRICE_FIRST_TOUCH = "first_touch"
ENTRY_PRICE_MIDDLE = "middle"
ENTRY_PRICE_DISTRIBUTED = "distributed"
GROUP_STATUS_PLANNED = "planned"
GROUP_STATUS_SIMULATED = "simulated"
GROUP_STATUS_REJECTED = "rejected"
GROUP_STATUS_EXPIRED = "expired"
GROUP_STATUS_CANCELLED = "cancelled"
GROUP_STATUS_PENDING_SUBMISSION = "pending_submission"
GROUP_STATUS_PENDING_ACTIVE = "pending_active"
GROUP_STATUS_FAILED = "failed"
ORDER_STATUS_PLANNED = "planned"
ORDER_STATUS_SIMULATED = "simulated"
ORDER_STATUS_REJECTED = "rejected"
ORDER_STATUS_EXPIRED = "expired"
ORDER_STATUS_PENDING_SUBMISSION = "pending_submission"
ORDER_STATUS_PENDING_ACTIVE = "pending_active"
ORDER_STATUS_FAILED = "failed"


class PendingOrderType(StrEnum):
    BUY_LIMIT = "BUY_LIMIT"
    BUY_STOP = "BUY_STOP"
    SELL_LIMIT = "SELL_LIMIT"
    SELL_STOP = "SELL_STOP"


@dataclass(frozen=True)
class MT5Account:
    id: int
    user_id: int
    broker_name: str
    server_name: str
    login: str = field(repr=False)
    encrypted_password: str = field(repr=False)
    account_alias: str
    terminal_path: Path | None
    account_type: str
    account_mode: str
    connection_status: str
    last_error: str | None
    last_connected_at: str | None
    balance: Decimal | None = None
    equity: Decimal | None = None
    worker_heartbeat_at: str | None = None

    @property
    def masked_login(self) -> str:
        return mask_login(self.login)


@dataclass(frozen=True)
class AccountConnectionInfo:
    login: int
    trade_allowed: bool
    server: str
    account_type: str = ACCOUNT_TYPE_DEMO
    account_mode: str = ACCOUNT_MODE_HEDGING
    balance: Decimal | None = None
    equity: Decimal | None = None


@dataclass(frozen=True)
class TerminalInfo:
    path: Path | None
    connected: bool


@dataclass(frozen=True)
class SymbolInfo:
    name: str
    volume_min: Decimal = Decimal("0.01")
    volume_max: Decimal = Decimal("100")
    volume_step: Decimal = Decimal("0.01")
    trade_tick_size: Decimal = Decimal("0.01")
    digits: int = 2
    stops_level_points: int = 0
    filling_mode: int | None = None
    trade_allowed: bool = True


@dataclass(frozen=True)
class TickInfo:
    bid: Decimal
    ask: Decimal


@dataclass(frozen=True)
class ExecutionProfile:
    user_id: int
    mt5_account_id: int
    enabled: bool
    risk_mode: str
    fixed_lot: Decimal
    risk_percent: Decimal
    max_spread_points: int
    max_slippage_points: int
    daily_profit_target: Decimal
    daily_loss_limit: Decimal
    max_open_signals: int
    split_tps: bool
    breakeven_enabled: bool
    trailing_enabled: bool
    entry_execution_mode: str = ENTRY_EXECUTION_PENDING_ORDER
    entry_price_mode: str = ENTRY_PRICE_FIRST_TOUCH
    pending_expiration_minutes: int = 120


@dataclass(frozen=True)
class SimulatedExecution:
    id: int
    signal_id: str
    user_id: int
    mt5_account_id: int
    symbol: str
    direction: str
    requested_volume: Decimal
    take_profit: Decimal


@dataclass(frozen=True)
class ExecutionGroup:
    id: int
    signal_id: str
    user_id: int
    mt5_account_id: int
    status: str
    direction: str
    symbol: str
    entry_low: Decimal
    entry_high: Decimal
    selected_entry_price: Decimal
    order_type: str
    total_volume: Decimal
    stop_loss: Decimal
    expiration_at: str
    execution_mode: str
    error_code: str | None
    error_message: str | None


@dataclass(frozen=True)
class ExecutionOrder:
    id: int
    execution_group_id: int
    tp_index: int
    requested_volume: Decimal
    normalized_volume: Decimal
    entry_price: Decimal
    stop_loss: Decimal
    take_profit: Decimal
    order_type: str
    status: str
    mt5_order_ticket: str | None = None
    mt5_position_ticket: str | None = None


@dataclass(frozen=True)
class PlannedOrder:
    tp_index: int
    requested_volume: Decimal
    normalized_volume: Decimal
    entry_price: Decimal
    stop_loss: Decimal
    take_profit: Decimal
    order_type: PendingOrderType


@dataclass(frozen=True)
class PendingOrderPlan:
    signal_id: str
    user_id: int
    mt5_account_id: int
    account_mode: str
    direction: str
    symbol: str
    entry_low: Decimal
    entry_high: Decimal
    selected_entry_price: Decimal
    order_type: PendingOrderType
    total_volume: Decimal
    stop_loss: Decimal
    expiration_at: str
    execution_mode: str
    orders: tuple[PlannedOrder, ...]
    signal_received_at: str
    pending_created_at: str


@dataclass(frozen=True)
class LatencyMetrics:
    telegram_message_at: str | None = None
    listener_received_at: str | None = None
    signal_parsed_at: str | None = None
    execution_planned_at: str | None = None
    validation_started_at: str | None = None
    validation_finished_at: str | None = None
    mt5_order_sent_at: str | None = None
    broker_response_at: str | None = None
    telegram_to_listener_ms: int | None = None
    parsing_ms: int | None = None
    planning_ms: int | None = None
    validation_ms: int | None = None
    mt5_submission_ms: int | None = None
    broker_response_ms: int | None = None
    total_ms: int | None = None


def mask_login(login: str | int) -> str:
    text = str(login)
    if len(text) <= 4:
        return f"••••{text}"
    return f"••••{text[-4:]}"
