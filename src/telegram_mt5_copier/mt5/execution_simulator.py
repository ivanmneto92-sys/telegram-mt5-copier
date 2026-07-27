from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN
from pathlib import Path
from typing import Callable

from ..database import connect_database, initialize_database, utc_now
from ..models import TradeSignal, decimal_to_text
from .account_service import MT5AccountService
from .client import SimulatedMT5Client
from .models import EXECUTION_STATUS_SIMULATED, ExecutionProfile, MT5Account, SimulatedExecution, SymbolInfo
from .symbol_resolver import SymbolResolver


@dataclass(frozen=True)
class SimulationResult:
    account: MT5Account
    executions: tuple[SimulatedExecution, ...]
    message: str
    duplicate: bool = False


class ExecutionSimulator:
    def __init__(
        self,
        database_path: Path,
        accounts: MT5AccountService,
        *,
        client_factory: Callable[[], object] | None = None,
    ) -> None:
        self.database_path = database_path
        self.accounts = accounts
        self.client_factory = client_factory or SimulatedMT5Client
        self._closed = False
        initialize_database(database_path)

    def close(self) -> None:
        if hasattr(self.accounts, "close"):
            self.accounts.close()
        self._closed = True

    def __enter__(self) -> "ExecutionSimulator":
        return self

    def __exit__(self, _exc_type: object, _exc: object, _traceback: object) -> None:
        self.close()

    def simulate_for_signal(self, signal: TradeSignal) -> list[SimulationResult]:
        results: list[SimulationResult] = []
        for account, profile in self.accounts.connected_demo_accounts_for_active_users(
            signal.source_chat_id
        ):
            if self.execution_exists(signal.signature, account.user_id, account.id):
                results.append(SimulationResult(account=account, executions=(), message="", duplicate=True))
                continue

            client = self.client_factory()
            symbol = SymbolResolver(client).resolve(signal.symbol)
            symbol_info = client.symbol_info(symbol)
            if symbol_info is None:
                raise ValueError("Simbolo indisponivel para simulacao.")

            volumes = split_volume(profile, len(signal.take_profits), symbol_info)
            executions = []
            for volume, take_profit in zip(volumes, signal.take_profits):
                execution = self.record_simulated_execution(
                    signal=signal,
                    account=account,
                    symbol=symbol,
                    volume=volume,
                    take_profit=take_profit,
                )
                executions.append(execution)

            results.append(
                SimulationResult(
                    account=account,
                    executions=tuple(executions),
                    message=format_simulation_message(signal, account, sum(volumes), len(executions)),
                )
            )
        return results

    def execution_exists(self, signal_id: str, user_id: int, account_id: int) -> bool:
        with connect_database(self.database_path) as connection:
            cursor = connection.execute(
                """
                SELECT 1 FROM executions
                WHERE signal_id = ? AND user_id = ? AND mt5_account_id = ?
                LIMIT 1
                """,
                (signal_id, user_id, account_id),
            )
            try:
                return cursor.fetchone() is not None
            finally:
                cursor.close()

    def record_simulated_execution(
        self,
        *,
        signal: TradeSignal,
        account: MT5Account,
        symbol: str,
        volume: Decimal,
        take_profit: Decimal,
    ) -> SimulatedExecution:
        with connect_database(self.database_path) as connection:
            cursor = connection.execute(
                """
                INSERT INTO executions (
                    signal_id, user_id, mt5_account_id, status, symbol, direction,
                    requested_volume, executed_volume, entry_price, stop_loss,
                    take_profit, terminal_ticket, error_code, error_message,
                    created_at, executed_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    signal.signature,
                    account.user_id,
                    account.id,
                    EXECUTION_STATUS_SIMULATED,
                    symbol,
                    signal.direction.value,
                    decimal_to_text(volume),
                    decimal_to_text(volume),
                    f"{decimal_to_text(signal.entry_low)}-{decimal_to_text(signal.entry_high)}",
                    decimal_to_text(signal.stop_loss),
                    decimal_to_text(take_profit),
                    None,
                    None,
                    None,
                    utc_now(),
                    utc_now(),
                ),
            )
            try:
                execution_id = int(cursor.lastrowid)
            finally:
                cursor.close()
        return SimulatedExecution(
            id=execution_id,
            signal_id=signal.signature,
            user_id=account.user_id,
            mt5_account_id=account.id,
            symbol=symbol,
            direction=signal.direction.value,
            requested_volume=volume,
            take_profit=take_profit,
        )


def split_volume(profile: ExecutionProfile, take_profit_count: int, symbol_info: SymbolInfo) -> tuple[Decimal, ...]:
    if take_profit_count < 1:
        raise ValueError("Sinal sem take profit.")

    total = profile.fixed_lot
    if total < symbol_info.volume_min or total > symbol_info.volume_max:
        raise ValueError("Lote fora dos limites do simbolo.")

    if profile.split_tps:
        raw_volume = total / Decimal(take_profit_count)
        volume = raw_volume.quantize(symbol_info.volume_step, rounding=ROUND_DOWN)
        volumes = tuple(volume for _ in range(take_profit_count))
    else:
        volumes = tuple(total for _ in range(take_profit_count))

    for volume in volumes:
        validate_volume(volume, symbol_info)
    return volumes


def validate_volume(volume: Decimal, symbol_info: SymbolInfo) -> None:
    if volume < symbol_info.volume_min or volume > symbol_info.volume_max:
        raise ValueError("Lote fora dos limites do simbolo.")
    steps = volume / symbol_info.volume_step
    if steps != steps.to_integral_value():
        raise ValueError("Lote nao respeita o step do simbolo.")


def format_simulation_message(
    signal: TradeSignal,
    account: MT5Account,
    total_volume: Decimal,
    order_count: int,
) -> str:
    return "\n".join(
        [
            "🧪 EXECUÇÃO SIMULADA",
            "",
            f"{signal.symbol} — {signal.direction.value}",
            f"Conta: {account.masked_login}",
            f"Entrada: {decimal_to_text(signal.entry_low)} até {decimal_to_text(signal.entry_high)}",
            f"Lote total: {decimal_to_text(total_volume)}",
            f"Ordens simuladas: {order_count}",
            "",
            "Nenhuma ordem foi enviada ao MetaTrader.",
        ]
    )
