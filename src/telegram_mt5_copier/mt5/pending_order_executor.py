from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from ..models import TradeSignal, decimal_to_text
from .account_service import MT5AccountService
from .client import SimulatedMT5Client
from .execution_group_service import ExecutionGroupResult, ExecutionGroupService, build_latency_metrics
from .execution_repository import ExecutionRepository
from .models import (
    ACCOUNT_MODE_NETTING,
    ACCOUNT_TYPE_REAL,
    ExecutionProfile,
    MT5Account,
    PendingOrderPlan,
    SymbolInfo,
    TickInfo,
)
from .order_validator import OrderValidationError, validate_pending_order_plan
from .pending_order_planner import PendingOrderPlanner, PendingOrderPlanningError
from .symbol_resolver import SymbolResolver


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
        client = self.client_factory()
        symbol = SymbolResolver(client).resolve(signal.symbol)
        symbol_info = client.symbol_info(symbol)
        tick = client.symbol_info_tick(symbol)
        if symbol_info is None or tick is None:
            raise ValueError("Informacoes do simbolo indisponiveis.")

        planned_at = datetime.now(tz=timezone.utc)
        try:
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
            group_result = self.groups.reject_group(plan, exc.reason)
            return PendingExecutionResult(
                account=account,
                group_result=group_result,
                message=format_rejection_message(exc.reason, account),
            )
        except ValueError as exc:
            group_result = self.groups.reject_group(plan, str(exc))
            return PendingExecutionResult(
                account=account,
                group_result=group_result,
                message=format_rejection_message(str(exc), account),
            )

        validation_finished_at = datetime.now(tz=timezone.utc)
        if self.execution_mode == "simulation":
            metrics = build_latency_metrics(
                listener_received_at=started_at,
                signal_parsed_at=started_at,
                execution_planned_at=planned_at,
                validation_started_at=validation_started_at,
                validation_finished_at=validation_finished_at,
            )
            group_result = self.groups.create_simulated_group(plan, metrics=metrics)
            message = "" if group_result.duplicate else format_pending_simulation_message(plan, account)
            return PendingExecutionResult(account=account, group_result=group_result, message=message)

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
