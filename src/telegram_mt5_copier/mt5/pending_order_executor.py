from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Callable

from ..models import TradeSignal, decimal_to_text
from .account_service import MT5AccountService
from .client import SimulatedMT5Client
from .execution_group_service import ExecutionGroupResult, ExecutionGroupService, build_latency_metrics
from .execution_repository import ExecutionRepository
from .models import (
    ACCOUNT_MODE_NETTING,
    ACCOUNT_TYPE_DEMO,
    ACCOUNT_TYPE_REAL,
    CONNECTION_STATUS_CONNECTED,
    ExecutionProfile,
    GROUP_STATUS_PENDING_ACTIVE,
    GROUP_STATUS_PENDING_SUBMISSION,
    MT5Account,
    ORDER_STATUS_PENDING_ACTIVE,
    ORDER_STATUS_PENDING_SUBMISSION,
    PendingOrderPlan,
    PlannedOrder,
    SymbolInfo,
    TickInfo,
)
from .order_validator import OrderValidationError, validate_pending_order_plan
from .pending_order_planner import PendingOrderPlanner, PendingOrderPlanningError
from .symbol_resolver import SymbolResolver

MT5_MAGIC_NUMBER = 27071301


@dataclass(frozen=True)
class PendingExecutionResult:
    account: MT5Account
    group_result: ExecutionGroupResult
    message: str


class PendingOrderExecutor:
    def __init__(
        self,
        database_path: Path,
        accounts: MT5AccountService,
        *,
        execution_mode: str = "simulation",
        global_kill_switch: bool = True,
        client_factory: Callable[[], object] | None = None,
        planner: PendingOrderPlanner | None = None,
        repository: ExecutionRepository | None = None,
    ) -> None:
        self.database_path = database_path
        self.accounts = accounts
        self.execution_mode = execution_mode
        self.global_kill_switch = global_kill_switch
        self.client_factory = client_factory or SimulatedMT5Client
        self.planner = planner or PendingOrderPlanner()
        self.repository = repository or ExecutionRepository(database_path)
        self.groups = ExecutionGroupService(self.repository)
        self._closed = False

    def close(self) -> None:
        if self._closed:
            return
        self.groups.close()
        if hasattr(self.accounts, "close"):
            self.accounts.close()
        self._closed = True

    def __enter__(self) -> "PendingOrderExecutor":
        return self

    def __exit__(self, _exc_type: object, _exc: object, _traceback: object) -> None:
        self.close()

    def execute_for_signal(self, signal: TradeSignal) -> list[PendingExecutionResult]:
        results: list[PendingExecutionResult] = []
        for account, profile in self.accounts.connected_demo_accounts_for_active_users():
            results.append(self.execute_for_account(signal, account, profile))
        return results

    def execute_for_account(
        self,
        signal: TradeSignal,
        account: MT5Account,
        profile: ExecutionProfile,
    ) -> PendingExecutionResult:
        started_at = datetime.now(tz=timezone.utc)
        preflight_reason = self._preflight_rejection_reason(account)
        if preflight_reason is not None:
            return PendingExecutionResult(
                account=account,
                group_result=ExecutionGroupResult(group=None, orders=(), rejected_reason=preflight_reason),
                message=format_rejection_message(preflight_reason, account),
            )

        client = self.client_factory()
        initialized_client = False

        def shutdown_if_needed() -> None:
            if initialized_client and self.execution_mode == "demo_execution":
                client.shutdown()

        try:
            if self.execution_mode == "demo_execution":
                self._initialize_demo_client(client, account)
                initialized_client = True

            symbol = SymbolResolver(client).resolve(signal.symbol)
            if hasattr(client, "symbol_select") and not client.symbol_select(symbol, True):
                raise ValueError("symbol_select_failed")
            symbol_info = client.symbol_info(symbol)
            tick = client.symbol_info_tick(symbol)
            if symbol_info is None or tick is None:
                raise ValueError("Informacoes do simbolo indisponiveis.")

            planned_at = datetime.now(tz=timezone.utc)
            plan = self.planner.plan(
                signal=signal,
                account=account,
                profile=profile,
                symbol_info=symbol_info,
                tick=tick,
                execution_mode=self.execution_mode,
                now=planned_at,
            )
        except PendingOrderPlanningError as exc:
            shutdown_if_needed()
            if exc.plan is not None:
                group_result = self.groups.reject_group(exc.plan, exc.reason)
                return PendingExecutionResult(
                    account=account,
                    group_result=group_result,
                    message=format_rejection_message(exc.reason, account),
                )
            return PendingExecutionResult(
                account=account,
                group_result=ExecutionGroupResult(group=None, orders=(), rejected_reason=exc.reason),
                message=format_rejection_message(exc.reason, account),
            )
        except ValueError as exc:
            shutdown_if_needed()
            return PendingExecutionResult(
                account=account,
                group_result=ExecutionGroupResult(group=None, orders=(), rejected_reason=str(exc)),
                message=format_rejection_message(str(exc), account),
            )

        validation_started_at = datetime.now(tz=timezone.utc)
        try:
            self._validate_execution_mode(account, plan)
            validate_pending_order_plan(
                plan=plan,
                account=account,
                symbol_info=symbol_info,
                tick=tick,
                execution_mode=self.execution_mode,
                global_kill_switch=self.global_kill_switch,
            )
        except OrderValidationError as exc:
            shutdown_if_needed()
            group_result = self.groups.reject_group(plan, exc.reason)
            return PendingExecutionResult(
                account=account,
                group_result=group_result,
                message=format_rejection_message(exc.reason, account),
            )
        except ValueError as exc:
            shutdown_if_needed()
            group_result = self.groups.reject_group(plan, str(exc))
            return PendingExecutionResult(
                account=account,
                group_result=group_result,
                message=format_rejection_message(str(exc), account),
            )

        validation_finished_at = datetime.now(tz=timezone.utc)
        metrics = build_latency_metrics(
            listener_received_at=started_at,
            signal_parsed_at=started_at,
            execution_planned_at=planned_at,
            validation_started_at=validation_started_at,
            validation_finished_at=validation_finished_at,
        )
        if self.execution_mode == "simulation":
            group_result = self.groups.create_simulated_group(plan, metrics=metrics)
            message = "" if group_result.duplicate else format_pending_simulation_message(plan, account)
            return PendingExecutionResult(account=account, group_result=group_result, message=message)

        if self.execution_mode == "demo_execution":
            try:
                return self._execute_demo_plan(
                    plan=plan,
                    account=account,
                    profile=profile,
                    client=client,
                    symbol_info=symbol_info,
                    metrics=metrics,
                )
            finally:
                shutdown_if_needed()

        group_result = self.groups.reject_group(plan, "execution_mode_not_enabled")
        return PendingExecutionResult(
            account=account,
            group_result=group_result,
            message=format_rejection_message("execution_mode_not_enabled", account),
        )

    def _validate_execution_mode(self, account: MT5Account, plan: PendingOrderPlan) -> None:
        if self.execution_mode == "live_execution":
            raise OrderValidationError("live_execution_blocked")
        if account.account_type == ACCOUNT_TYPE_REAL:
            raise OrderValidationError("real_account_blocked")
        if (
            self.execution_mode == "demo_execution"
            and account.account_mode == ACCOUNT_MODE_NETTING
            and len(plan.orders) > 1
        ):
            raise OrderValidationError("netting_multiple_tps_not_supported")

    def _preflight_rejection_reason(self, account: MT5Account) -> str | None:
        if self.execution_mode == "live_execution":
            return "live_execution_blocked"
        if self.global_kill_switch and self.execution_mode != "simulation":
            return "kill_switch_enabled"
        if account.connection_status != CONNECTION_STATUS_CONNECTED:
            return "account_disconnected"
        if account.account_type == ACCOUNT_TYPE_REAL:
            return "real_account_blocked"
        if account.account_type != ACCOUNT_TYPE_DEMO:
            return "only_demo_accounts_supported"
        if self.execution_mode == "demo_execution" and account.terminal_path is None:
            return "terminal_not_provisioned"
        return None

    def _initialize_demo_client(self, client: object, account: MT5Account) -> None:
        if account.terminal_path is None:
            raise ValueError("terminal_not_provisioned")
        password: str | None = None
        try:
            password = self.accounts.decrypted_password_for_account(account)
            initialized = client.initialize(account.terminal_path, int(account.login), password, account.server_name)
            if not initialized:
                raise ValueError("mt5_initialize_failed")
        finally:
            password = None

    def _execute_demo_plan(
        self,
        *,
        plan: PendingOrderPlan,
        account: MT5Account,
        profile: ExecutionProfile,
        client: object,
        symbol_info: SymbolInfo,
        metrics: object,
    ) -> PendingExecutionResult:
        if self.repository.has_execution_group(plan.signal_id, plan.user_id, plan.mt5_account_id):
            return PendingExecutionResult(
                account=account,
                group_result=ExecutionGroupResult(group=None, orders=(), duplicate=True),
                message="",
            )

        requests = [
            build_pending_order_request(
                client=client,
                plan=plan,
                order=order,
                profile=profile,
                symbol_info=symbol_info,
            )
            for order in plan.orders
        ]
        check_results = [client.order_check(request) for request in requests]
        for check_result in check_results:
            if not is_successful_mt5_result(client, check_result, check=True):
                reason = f"order_check_failed:{result_message(check_result)}"
                group_result = self.groups.reject_group(plan, reason)
                return PendingExecutionResult(
                    account=account,
                    group_result=group_result,
                    message=format_rejection_message(reason, account),
                )

        group, orders = self.repository.create_group_with_orders(
            plan,
            status=GROUP_STATUS_PENDING_SUBMISSION,
            order_status=ORDER_STATUS_PENDING_SUBMISSION,
            metrics=metrics,
        )
        send_results = []
        for database_order, request in zip(orders, requests):
            send_result = client.order_send(request)
            send_results.append(send_result)
            if not is_successful_mt5_result(client, send_result, check=False):
                reason = f"order_send_failed:{result_message(send_result)}"
                self.repository.mark_group_failed(group.id, reason)
                return PendingExecutionResult(
                    account=account,
                    group_result=ExecutionGroupResult(group=group, orders=orders, rejected_reason=reason),
                    message=format_rejection_message(reason, account),
                )
            self.repository.mark_order_submitted(
                database_order.id,
                ticket=result_ticket(send_result),
                broker_retcode=str(result_retcode(send_result)),
                broker_message=result_message(send_result),
                status=ORDER_STATUS_PENDING_ACTIVE,
            )

        self.repository.update_group_status(group.id, GROUP_STATUS_PENDING_ACTIVE)
        return PendingExecutionResult(
            account=account,
            group_result=ExecutionGroupResult(group=group, orders=orders),
            message=format_demo_execution_message(plan, account, send_results),
        )


def format_pending_simulation_message(plan: PendingOrderPlan, account: MT5Account) -> str:
    lines = [
        "🧪 ORDENS PENDENTES SIMULADAS",
        "",
        f"{plan.symbol} — {order_type_label(plan.order_type.value)}",
        "",
        f"Conta: {account.masked_login}",
        f"Entrada selecionada: {decimal_to_text(plan.selected_entry_price)}",
        f"Faixa: {decimal_to_text(plan.entry_low)} até {decimal_to_text(plan.entry_high)}",
        f"Stop Loss: {decimal_to_text(plan.stop_loss)}",
        f"Validade: {expiration_label(plan)}",
        "",
        "Ordens simuladas:",
        "",
    ]
    for order in plan.orders:
        lines.append(
            f"TP{order.tp_index}: {decimal_to_text(order.take_profit)} | "
            f"Lote {decimal_to_text(order.normalized_volume)}"
        )
    lines.extend(["", "Nenhuma ordem foi enviada ao MetaTrader 5."])
    return "\n".join(lines)


def format_rejection_message(reason: str, account: MT5Account) -> str:
    return "\n".join(
        [
            "⚠️ EXECUÇÃO REJEITADA",
            "",
            f"Conta: {account.masked_login}",
            f"Motivo: {reason}",
            "",
            "Nenhuma ordem foi enviada ao MetaTrader 5.",
        ]
    )


def order_type_label(order_type: str) -> str:
    return order_type.replace("_", " ")


def expiration_label(plan: PendingOrderPlan) -> str:
    created = datetime.fromisoformat(plan.pending_created_at)
    expires = datetime.fromisoformat(plan.expiration_at)
    minutes = int((expires - created).total_seconds() / 60)
    if minutes % 60 == 0:
        hours = minutes // 60
        return f"{hours} hora" if hours == 1 else f"{hours} horas"
    return f"{minutes} minutos"


def build_pending_order_request(
    *,
    client: object,
    plan: PendingOrderPlan,
    order: PlannedOrder,
    profile: ExecutionProfile,
    symbol_info: SymbolInfo,
) -> dict[str, object]:
    request = {
        "action": mt5_constant(client, "TRADE_ACTION_PENDING", 5),
        "symbol": plan.symbol,
        "volume": decimal_to_float(order.normalized_volume),
        "type": pending_order_type_constant(client, order.order_type.value),
        "price": decimal_to_float(order.entry_price),
        "sl": decimal_to_float(order.stop_loss),
        "tp": decimal_to_float(order.take_profit),
        "deviation": int(profile.max_slippage_points),
        "magic": MT5_MAGIC_NUMBER,
        "comment": f"telegram-mt5-copier TP{order.tp_index}",
        "type_time": mt5_constant(client, "ORDER_TIME_SPECIFIED", 2),
        "expiration": datetime.fromisoformat(plan.expiration_at),
    }
    if symbol_info.filling_mode is not None:
        request["type_filling"] = symbol_info.filling_mode
    else:
        request["type_filling"] = mt5_constant(client, "ORDER_FILLING_RETURN", 2)
    return request


def pending_order_type_constant(client: object, order_type: str) -> int:
    constants = {
        "BUY_LIMIT": ("ORDER_TYPE_BUY_LIMIT", 2),
        "SELL_LIMIT": ("ORDER_TYPE_SELL_LIMIT", 3),
        "BUY_STOP": ("ORDER_TYPE_BUY_STOP", 4),
        "SELL_STOP": ("ORDER_TYPE_SELL_STOP", 5),
    }
    constant_name, default = constants[order_type]
    return mt5_constant(client, constant_name, default)


def mt5_constant(client: object, name: str, default: int) -> int:
    if hasattr(client, "constant"):
        return int(client.constant(name, default))
    return default


def decimal_to_float(value: Decimal) -> float:
    return float(value)


def is_successful_mt5_result(client: object, result: object | None, *, check: bool) -> bool:
    if result is None:
        return False
    retcode = result_retcode(result)
    if check:
        return retcode == 0
    accepted_ret_codes = {
        0,
        mt5_constant(client, "TRADE_RETCODE_PLACED", 10008),
        mt5_constant(client, "TRADE_RETCODE_DONE", 10009),
    }
    return retcode in accepted_ret_codes


def result_retcode(result: object | None) -> int:
    value = result_value(result, "retcode", -1)
    try:
        return int(value)
    except (TypeError, ValueError):
        return -1


def result_ticket(result: object | None) -> str:
    value = result_value(result, "order", None)
    if value is None:
        value = result_value(result, "deal", "")
    return str(value or "")


def result_message(result: object | None) -> str:
    for key in ("comment", "message", "retcode_external"):
        value = result_value(result, key, None)
        if value:
            return str(value)
    return f"retcode={result_retcode(result)}"


def result_value(result: object | None, key: str, default: object) -> object:
    if result is None:
        return default
    if isinstance(result, dict):
        return result.get(key, default)
    if hasattr(result, key):
        return getattr(result, key)
    if hasattr(result, "_asdict"):
        return result._asdict().get(key, default)
    return default


def format_demo_execution_message(
    plan: PendingOrderPlan,
    account: MT5Account,
    send_results: list[object],
) -> str:
    lines = [
        "✅ ORDENS PENDENTES ENVIADAS EM CONTA DEMO",
        "",
        f"{plan.symbol} — {order_type_label(plan.order_type.value)}",
        "",
        f"Conta: {account.masked_login}",
        f"Entrada selecionada: {decimal_to_text(plan.selected_entry_price)}",
        f"Faixa: {decimal_to_text(plan.entry_low)} até {decimal_to_text(plan.entry_high)}",
        f"Stop Loss: {decimal_to_text(plan.stop_loss)}",
        f"Validade: {expiration_label(plan)}",
        "",
        "Tickets:",
    ]
    for order, send_result in zip(plan.orders, send_results):
        lines.append(
            f"TP{order.tp_index}: ticket {result_ticket(send_result)} | "
            f"Lote {decimal_to_text(order.normalized_volume)}"
        )
    return "\n".join(lines)
