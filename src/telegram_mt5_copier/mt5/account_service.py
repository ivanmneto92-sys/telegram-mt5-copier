from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
import json
from pathlib import Path
import time
from typing import Callable

from ..credential_service import CredentialService
from ..database import connect_database, initialize_database, utc_now
from .client import MT5Client
from .models import (
    ACCOUNT_MODE_HEDGING,
    ACCOUNT_TYPE_DEMO,
    ACCOUNT_TYPE_REAL,
    CONNECTION_STATUS_CONNECTED,
    CONNECTION_STATUS_DISCONNECTED,
    CONNECTION_STATUS_FAILED,
    ENTRY_EXECUTION_PENDING_ORDER,
    ENTRY_PRICE_FIRST_TOUCH,
    ExecutionProfile,
    MT5Account,
)
from .ipc_lock import MT5OperationLock
from .terminal_manager import TerminalManager


@dataclass(repr=False)
class MT5AccountForm:
    broker_name: str
    server_name: str
    login: str
    password: str = field(repr=False)
    account_alias: str

    def __repr__(self) -> str:
        return (
            "MT5AccountForm("
            f"broker_name={self.broker_name!r}, "
            f"server_name={self.server_name!r}, "
            f"login={self.login!r}, "
            "password=***REDACTED***, "
            f"account_alias={self.account_alias!r})"
        )


class MT5ConnectionTestError(ValueError):
    pass


class MT5UnsafeAccountError(ValueError):
    pass


class MT5AccountService:
    def __init__(
        self,
        database_path: Path,
        *,
        credential_service: CredentialService | None = None,
        terminal_manager: TerminalManager | None = None,
        client_factory: Callable[[], object] | None = None,
        allow_live_accounts: bool = False,
        max_accounts_per_vps: int = 10,
    ) -> None:
        self.database_path = database_path
        self.credential_service = credential_service
        self.terminal_manager = terminal_manager
        self.client_factory = client_factory or MT5Client
        self.allow_live_accounts = allow_live_accounts
        self.max_accounts_per_vps = max_accounts_per_vps
        self._closed = False
        initialize_database(database_path)

    def close(self) -> None:
        self._closed = True

    def __enter__(self) -> "MT5AccountService":
        return self

    def __exit__(self, _exc_type: object, _exc: object, _traceback: object) -> None:
        self.close()

    def register_account(
        self,
        user_id: int,
        form: MT5AccountForm,
        *,
        keep_on_connection_failure: bool = False,
    ) -> MT5Account:
        self._require_credentials()
        broker_name = require_text(form.broker_name, "broker_name")
        server_name = require_text(form.server_name, "server_name")
        login = require_login(form.login)
        account_alias = require_text(form.account_alias, "account_alias")
        password = require_text(form.password, "password")

        encrypted_password = self.credential_service.encrypt_password(password)
        password = ""
        form.password = ""
        if self._account_already_connected(user_id, login, server_name):
            raise ValueError("Conta MT5 ja cadastrada. Use Testar conexao no bot.")
        now = utc_now()
        retry_account = self._account_for_registration_retry(user_id, login, server_name)
        if retry_account is not None:
            account_id = retry_account.id
            with connect_database(self.database_path) as connection:
                connection.execute(
                    """
                    UPDATE mt5_accounts
                    SET broker_name = ?, server_name = ?, encrypted_password = ?,
                        account_alias = ?, connection_status = ?,
                        last_error = NULL, updated_at = ?
                    WHERE id = ? AND user_id = ?
                    """,
                    (
                        broker_name,
                        server_name,
                        encrypted_password,
                        account_alias,
                        CONNECTION_STATUS_DISCONNECTED,
                        now,
                        account_id,
                        user_id,
                    ),
                ).close()
            self.record_audit(
                user_id,
                "mt5_account_registration_retried",
                {"account_id": account_id},
            )
        else:
            if self.count_accounts() >= self.max_accounts_per_vps:
                raise ValueError("Limite de contas MT5 da VPS atingido.")
            with connect_database(self.database_path) as connection:
                cursor = connection.execute(
                    """
                    INSERT INTO mt5_accounts (
                        user_id,
                        broker_name,
                        server_name,
                        login,
                        encrypted_password,
                        account_alias,
                        account_mode,
                        account_type,
                        connection_status,
                        created_at,
                        updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        user_id,
                        broker_name,
                        server_name,
                        login,
                        encrypted_password,
                        account_alias,
                        ACCOUNT_MODE_HEDGING,
                        ACCOUNT_TYPE_DEMO,
                        CONNECTION_STATUS_DISCONNECTED,
                        now,
                        now,
                    ),
                )
                try:
                    account_id = int(cursor.lastrowid)
                finally:
                    cursor.close()

        try:
            if self.terminal_manager is not None:
                provisioned = self.terminal_manager.provision_account(
                    account_id,
                    sanitize_legacy=retry_account is not None,
                )
                self.update_terminal_path(user_id, account_id, provisioned.terminal_path)
            self.ensure_execution_profile(user_id, account_id)
            self.test_connection(user_id, account_id, startup_retry=True)
            self.record_audit(user_id, "mt5_account_registered", {"account_id": account_id})
        except Exception as exc:
            if keep_on_connection_failure and not isinstance(exc, MT5UnsafeAccountError):
                failed_account = self.get_account(user_id, account_id)
                if failed_account.connection_status != CONNECTION_STATUS_FAILED:
                    self.update_connection_status(
                        user_id,
                        account_id,
                        status=CONNECTION_STATUS_FAILED,
                        account_type=failed_account.account_type,
                        account_mode=failed_account.account_mode,
                        last_error=str(exc),
                        connected=False,
                    )
                self.record_audit(
                    user_id,
                    "mt5_account_registered_with_connection_failure",
                    {"account_id": account_id},
                )
                return self.get_account(user_id, account_id)
            if retry_account is not None:
                raise
            self.remove_account(user_id, account_id)
            raise

        return self.get_account(user_id, account_id)

    def test_connection(
        self,
        user_id: int,
        account_id: int,
        *,
        startup_retry: bool = False,
    ) -> MT5Account:
        self._require_credentials()
        account = self.get_account(user_id, account_id)
        if account.terminal_path is None:
            raise ValueError("Terminal MT5 ainda nao provisionado.")
        if (
            startup_retry
            and account.connection_status == CONNECTION_STATUS_FAILED
            and self.terminal_manager is not None
        ):
            self.terminal_manager.provision_account(
                account.id,
                sanitize_legacy=True,
            )

        client = self.client_factory()
        operation_lock = MT5OperationLock(account.terminal_path.parent)
        password: str | None = None
        try:
            operation_lock.acquire()
            password = self.credential_service.decrypt_password(account.encrypted_password)
            if not self._initialize_client(
                client,
                account,
                password,
                startup_retry=startup_retry,
            ):
                raise MT5ConnectionTestError(mt5_connection_error_message(client))
            terminal = client.terminal_info()
            if terminal is None or not terminal.connected:
                raise MT5ConnectionTestError("Terminal MT5 desconectado.")
            if not terminal.trade_allowed or terminal.tradeapi_disabled:
                raise MT5ConnectionTestError("Negociacao automatica/API Python desativada no terminal MT5.")
            info = client.account_info()
            if info is None:
                raise MT5ConnectionTestError(
                    mt5_connection_error_message(client, "MT5 nao retornou informacoes da conta.")
                )
            if int(info.login) != int(account.login):
                raise MT5ConnectionTestError("Login retornado pelo MT5 nao corresponde a conta cadastrada.")
            if not info.trade_allowed:
                raise MT5ConnectionTestError("Conta MT5 nao permite negociacao.")
            if info.account_type == ACCOUNT_TYPE_REAL and not self.allow_live_accounts:
                raise MT5UnsafeAccountError("Contas reais estao bloqueadas nesta fase.")

            self.update_connection_status(
                user_id,
                account_id,
                status=CONNECTION_STATUS_CONNECTED,
                account_type=info.account_type,
                account_mode=info.account_mode,
                last_error=None,
                connected=True,
                server_name=info.server,
                balance=info.balance,
                equity=info.equity,
            )
            self.record_audit(user_id, "mt5_connection_tested", {"account_id": account_id, "status": "connected"})
            return self.get_account(user_id, account_id)
        except Exception as exc:
            self.update_connection_status(
                user_id,
                account_id,
                status=CONNECTION_STATUS_FAILED,
                account_type=account.account_type,
                account_mode=account.account_mode,
                last_error=str(exc),
                connected=False,
            )
            raise
        finally:
            password = None
            client.shutdown()
            operation_lock.close()

    def _initialize_client(
        self,
        client: object,
        account: MT5Account,
        password: str,
        *,
        startup_retry: bool,
    ) -> bool:
        attempts = 2 if startup_retry else 1
        for attempt in range(attempts):
            if client.initialize(
                account.terminal_path,
                int(account.login),
                password,
                account.server_name,
            ):
                return True
            if attempt + 1 >= attempts or not is_transient_ipc_error(client):
                return False
            client.shutdown()
            time.sleep(3)
        return False

    def _account_for_registration_retry(
        self,
        user_id: int,
        login: str,
        server_name: str,
    ) -> MT5Account | None:
        with connect_database(self.database_path) as connection:
            row = connection.execute(
                """
                SELECT id, user_id, broker_name, server_name, login, encrypted_password,
                       account_alias, terminal_path, account_type, connection_status,
                       last_error, last_connected_at, account_mode, balance, equity,
                       worker_heartbeat_at
                FROM mt5_accounts
                WHERE user_id = ? AND login = ?
                  AND lower(server_name) = lower(?)
                  AND connection_status IN (?, ?)
                ORDER BY id DESC LIMIT 1
                """,
                (
                    user_id,
                    login,
                    server_name,
                    CONNECTION_STATUS_FAILED,
                    CONNECTION_STATUS_DISCONNECTED,
                ),
            ).fetchone()
            if row is None:
                row = connection.execute(
                    """
                    SELECT id, user_id, broker_name, server_name, login, encrypted_password,
                           account_alias, terminal_path, account_type, connection_status,
                           last_error, last_connected_at, account_mode, balance, equity,
                           worker_heartbeat_at
                    FROM mt5_accounts
                    WHERE user_id = ? AND login = ?
                      AND connection_status IN (?, ?)
                    ORDER BY id DESC LIMIT 1
                    """,
                    (
                        user_id,
                        login,
                        CONNECTION_STATUS_FAILED,
                        CONNECTION_STATUS_DISCONNECTED,
                    ),
                ).fetchone()
        return account_from_row(row) if row is not None else None

    def _account_already_connected(
        self,
        user_id: int,
        login: str,
        server_name: str,
    ) -> bool:
        with connect_database(self.database_path) as connection:
            row = connection.execute(
                """
                SELECT 1 FROM mt5_accounts
                WHERE user_id = ? AND login = ?
                  AND lower(server_name) = lower(?)
                  AND connection_status = ?
                LIMIT 1
                """,
                (
                    user_id,
                    login,
                    server_name,
                    CONNECTION_STATUS_CONNECTED,
                ),
            ).fetchone()
        return row is not None

    def update_terminal_path(self, user_id: int, account_id: int, terminal_path: Path) -> None:
        with connect_database(self.database_path) as connection:
            cursor = connection.execute(
                """
                UPDATE mt5_accounts
                SET terminal_path = ?, updated_at = ?
                WHERE id = ? AND user_id = ?
                """,
                (str(terminal_path), utc_now(), account_id, user_id),
            )
            cursor.close()

    def update_connection_status(
        self,
        user_id: int,
        account_id: int,
        *,
        status: str,
        account_type: str,
        account_mode: str,
        last_error: str | None,
        connected: bool,
        server_name: str | None = None,
        balance: Decimal | None = None,
        equity: Decimal | None = None,
    ) -> None:
        with connect_database(self.database_path) as connection:
            cursor = connection.execute(
                """
                UPDATE mt5_accounts
                SET connection_status = ?,
                    server_name = COALESCE(NULLIF(?, ''), server_name),
                    account_type = ?,
                    account_mode = ?,
                    balance = ?,
                    equity = ?,
                    last_error = ?,
                    last_connected_at = ?,
                    updated_at = ?
                WHERE id = ? AND user_id = ?
                """,
                (
                    status,
                    server_name,
                    account_type,
                    account_mode,
                    str(balance) if balance is not None else None,
                    str(equity) if equity is not None else None,
                    last_error,
                    utc_now() if connected else None,
                    utc_now(),
                    account_id,
                    user_id,
                ),
            )
            cursor.close()

    def list_accounts(self, user_id: int) -> list[MT5Account]:
        with connect_database(self.database_path) as connection:
            cursor = connection.execute(
                """
                SELECT id, user_id, broker_name, server_name, login, encrypted_password,
                       account_alias, terminal_path, account_type, connection_status,
                       last_error, last_connected_at, account_mode, balance, equity,
                       worker_heartbeat_at
                FROM mt5_accounts
                WHERE user_id = ?
                ORDER BY id ASC
                """,
                (user_id,),
            )
            try:
                rows = cursor.fetchall()
            finally:
                cursor.close()
        return [account_from_row(row) for row in rows]

    def get_account(self, user_id: int, account_id: int) -> MT5Account:
        with connect_database(self.database_path) as connection:
            cursor = connection.execute(
                """
                SELECT id, user_id, broker_name, server_name, login, encrypted_password,
                       account_alias, terminal_path, account_type, connection_status,
                       last_error, last_connected_at, account_mode, balance, equity,
                       worker_heartbeat_at
                FROM mt5_accounts
                WHERE id = ? AND user_id = ?
                """,
                (account_id, user_id),
            )
            try:
                row = cursor.fetchone()
            finally:
                cursor.close()
        if row is None:
            raise ValueError("Conta MT5 nao encontrada para este usuario.")
        return account_from_row(row)

    def first_account(self, user_id: int) -> MT5Account | None:
        accounts = self.list_accounts(user_id)
        return accounts[0] if accounts else None

    def remove_account(self, user_id: int, account_id: int) -> None:
        with connect_database(self.database_path) as connection:
            for sql in (
                """
                DELETE FROM execution_notifications
                WHERE execution_group_id IN (
                    SELECT id FROM execution_groups WHERE user_id = ? AND mt5_account_id = ?
                )
                """,
                """
                DELETE FROM execution_orders
                WHERE execution_group_id IN (
                    SELECT id FROM execution_groups WHERE user_id = ? AND mt5_account_id = ?
                )
                """,
                "DELETE FROM execution_groups WHERE user_id = ? AND mt5_account_id = ?",
                "DELETE FROM executions WHERE user_id = ? AND mt5_account_id = ?",
                "DELETE FROM execution_profiles WHERE user_id = ? AND mt5_account_id = ?",
                "DELETE FROM mt5_accounts WHERE user_id = ? AND id = ?",
            ):
                cursor = connection.execute(sql, (user_id, account_id))
                cursor.close()
        self.record_audit(user_id, "mt5_account_removed", {"account_id": account_id})

    def count_accounts(self) -> int:
        with connect_database(self.database_path) as connection:
            cursor = connection.execute("SELECT COUNT(*) FROM mt5_accounts")
            try:
                return int(cursor.fetchone()[0])
            finally:
                cursor.close()

    def ensure_execution_profile(self, user_id: int, account_id: int) -> ExecutionProfile:
        existing = self.get_execution_profile(user_id, account_id)
        if existing is not None:
            return existing

        with connect_database(self.database_path) as connection:
            settings_cursor = connection.execute(
                """
                SELECT risk_mode, fixed_lot, risk_percent, daily_profit_target,
                       daily_loss_limit, max_open_trades, breakeven_enabled, trailing_enabled
                FROM user_settings WHERE user_id = ?
                """,
                (user_id,),
            )
            try:
                legacy = settings_cursor.fetchone()
            finally:
                settings_cursor.close()
            risk_mode = str(legacy[0]) if legacy else "fixed_lot"
            fixed_lot = str(legacy[1]) if legacy else "0.01"
            risk_percent = str(legacy[2]) if legacy else "1"
            daily_profit_target = str(legacy[3]) if legacy else "0"
            daily_loss_limit = str(legacy[4]) if legacy else "0"
            max_open_signals = int(legacy[5]) if legacy else 1
            breakeven_enabled = int(legacy[6]) if legacy else 0
            trailing_enabled = int(legacy[7]) if legacy else 0
            cursor = connection.execute(
                """
                INSERT INTO execution_profiles (
                    user_id, mt5_account_id, enabled, risk_mode, fixed_lot, risk_percent,
                    max_spread_points, max_slippage_points, daily_profit_target,
                    daily_loss_limit, max_open_signals, split_tps, breakeven_enabled,
                    trailing_enabled, entry_execution_mode, entry_price_mode,
                    pending_expiration_minutes, take_profit_limit, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    account_id,
                    1,
                    risk_mode,
                    fixed_lot,
                    risk_percent,
                    300,
                    30,
                    daily_profit_target,
                    daily_loss_limit,
                    max_open_signals,
                    1,
                    breakeven_enabled,
                    trailing_enabled,
                    ENTRY_EXECUTION_PENDING_ORDER,
                    ENTRY_PRICE_FIRST_TOUCH,
                    120,
                    0,
                    utc_now(),
                ),
            )
            cursor.close()
        return self.get_execution_profile(user_id, account_id)

    def get_execution_profile(self, user_id: int, account_id: int) -> ExecutionProfile | None:
        with connect_database(self.database_path) as connection:
            cursor = connection.execute(
                """
                SELECT user_id, mt5_account_id, enabled, risk_mode, fixed_lot, risk_percent,
                       max_spread_points, max_slippage_points, daily_profit_target,
                       daily_loss_limit, max_open_signals, split_tps, breakeven_enabled,
                       trailing_enabled, entry_execution_mode, entry_price_mode,
                       pending_expiration_minutes, take_profit_limit
                FROM execution_profiles
                WHERE user_id = ? AND mt5_account_id = ?
                """,
                (user_id, account_id),
            )
            try:
                row = cursor.fetchone()
            finally:
                cursor.close()
        return execution_profile_from_row(row) if row else None

    def update_execution_profile_fixed_lot(
        self,
        user_id: int,
        account_id: int,
        fixed_lot: Decimal | str,
    ) -> ExecutionProfile:
        self.ensure_execution_profile(user_id, account_id)
        value = Decimal(str(fixed_lot))
        if value <= 0:
            raise ValueError("Lote fixo deve ser maior que zero.")
        with connect_database(self.database_path) as connection:
            cursor = connection.execute(
                """
                UPDATE execution_profiles
                SET fixed_lot = ?, updated_at = ?
                WHERE user_id = ? AND mt5_account_id = ?
                """,
                (str(value), utc_now(), user_id, account_id),
            )
            cursor.close()
        profile = self.get_execution_profile(user_id, account_id)
        if profile is None:
            raise ValueError("Perfil de execucao nao encontrado.")
        return profile

    def update_execution_profile_field(
        self,
        user_id: int,
        account_id: int,
        field_name: str,
        value: str | int,
    ) -> ExecutionProfile:
        allowed_fields = {
            "entry_execution_mode",
            "entry_price_mode",
            "pending_expiration_minutes",
            "take_profit_limit",
            "split_tps",
            "risk_mode",
            "risk_percent",
            "max_spread_points",
            "max_slippage_points",
            "daily_profit_target",
            "daily_loss_limit",
            "max_open_signals",
            "breakeven_enabled",
            "trailing_enabled",
        }
        if field_name not in allowed_fields:
            raise ValueError("Campo de execucao invalido.")
        if field_name == "take_profit_limit":
            value = int(value)
            if value < 0 or value > 10:
                raise ValueError("Quantidade de Take Profits deve ficar entre 1 e 10, ou 0 para todos.")
        self.ensure_execution_profile(user_id, account_id)
        with connect_database(self.database_path) as connection:
            cursor = connection.execute(
                f"UPDATE execution_profiles SET {field_name} = ?, updated_at = ? WHERE user_id = ? AND mt5_account_id = ?",
                (value, utc_now(), user_id, account_id),
            )
            cursor.close()
        profile = self.get_execution_profile(user_id, account_id)
        if profile is None:
            raise ValueError("Perfil de execucao nao encontrado.")
        return profile

    def update_execution_profile_risk_value(
        self,
        user_id: int,
        account_id: int,
        field_name: str,
        value: Decimal | str | int,
    ) -> ExecutionProfile:
        decimal_fields = {"risk_percent", "daily_profit_target", "daily_loss_limit"}
        integer_fields = {"max_spread_points", "max_slippage_points", "max_open_signals"}
        if field_name in decimal_fields:
            normalized: str | int = str(Decimal(str(value)))
            number = Decimal(normalized)
            if field_name == "risk_percent" and (number <= 0 or number > Decimal("100")):
                raise ValueError("Risco percentual deve estar entre 0 e 100.")
            if field_name != "risk_percent" and number < 0:
                raise ValueError("Valor financeiro nao pode ser negativo.")
        elif field_name in integer_fields:
            normalized = int(value)
            if normalized < 1:
                raise ValueError("O valor deve ser maior que zero.")
        else:
            raise ValueError("Campo de risco invalido.")
        return self.update_execution_profile_field(user_id, account_id, field_name, normalized)

    def connected_demo_accounts_for_active_users(
        self,
        source_chat_id: int | str | None = None,
    ) -> list[tuple[MT5Account, ExecutionProfile]]:
        channel_filter = channel_selection_filter_sql() if source_chat_id is not None else ""
        parameters: tuple[object, ...] = (CONNECTION_STATUS_CONNECTED, ACCOUNT_TYPE_DEMO)
        if source_chat_id is not None:
            parameters += (str(source_chat_id),)
        with connect_database(self.database_path) as connection:
            cursor = connection.execute(
                f"""
                SELECT a.id, a.user_id, a.broker_name, a.server_name, a.login,
                       a.encrypted_password, a.account_alias, a.terminal_path,
                       a.account_type, a.connection_status, a.last_error, a.last_connected_at,
                       a.account_mode, a.balance, a.equity, a.worker_heartbeat_at,
                       p.enabled, p.risk_mode, p.fixed_lot, p.risk_percent,
                       p.max_spread_points, p.max_slippage_points, p.daily_profit_target,
                       p.daily_loss_limit, p.max_open_signals, p.split_tps,
                       p.breakeven_enabled, p.trailing_enabled, p.entry_execution_mode,
                       p.entry_price_mode, p.pending_expiration_minutes, p.take_profit_limit
                FROM mt5_accounts a
                JOIN users u ON u.id = a.user_id
                JOIN execution_profiles p ON p.user_id = a.user_id AND p.mt5_account_id = a.id
                JOIN customer_billing b ON b.user_id = a.user_id
                WHERE u.status = 'active'
                  AND b.billing_status = 'paid'
                  AND b.due_date IS NOT NULL
                  AND date(b.due_date) >= date('now')
                  AND EXISTS (
                      SELECT 1 FROM customer_payments cp
                      WHERE cp.user_id = a.user_id
                        AND cp.status = 'paid'
                        AND CAST(cp.amount AS REAL) > 0
                  )
                  AND a.connection_status = ?
                  AND a.account_type = ?
                  AND p.enabled = 1
                  {channel_filter}
                ORDER BY a.id ASC
                """,
                parameters,
            )
            try:
                rows = cursor.fetchall()
            finally:
                cursor.close()

        results: list[tuple[MT5Account, ExecutionProfile]] = []
        for row in rows:
            account = account_from_row(row[:16])
            profile = execution_profile_from_row((row[1], row[0], *row[16:]))
            results.append((account, profile))
        return results

    def accounts_for_approved_users(
        self,
        source_chat_id: int | str | None = None,
    ) -> list[tuple[MT5Account, ExecutionProfile]]:
        channel_filter = channel_selection_filter_sql() if source_chat_id is not None else ""
        parameters: tuple[object, ...] = (
            (str(source_chat_id),) if source_chat_id is not None else ()
        )
        with connect_database(self.database_path) as connection:
            cursor = connection.execute(
                f"""
                SELECT a.id, a.user_id, a.broker_name, a.server_name, a.login,
                       a.encrypted_password, a.account_alias, a.terminal_path,
                       a.account_type, a.connection_status, a.last_error, a.last_connected_at,
                       a.account_mode, a.balance, a.equity, a.worker_heartbeat_at,
                       p.enabled, p.risk_mode, p.fixed_lot, p.risk_percent,
                       p.max_spread_points, p.max_slippage_points, p.daily_profit_target,
                       p.daily_loss_limit, p.max_open_signals, p.split_tps,
                       p.breakeven_enabled, p.trailing_enabled, p.entry_execution_mode,
                       p.entry_price_mode, p.pending_expiration_minutes, p.take_profit_limit
                FROM mt5_accounts a
                JOIN users u ON u.id = a.user_id
                JOIN execution_profiles p ON p.user_id = a.user_id AND p.mt5_account_id = a.id
                JOIN customer_billing b ON b.user_id = a.user_id
                WHERE u.status = 'active'
                  AND b.billing_status = 'paid'
                  AND b.due_date IS NOT NULL
                  AND date(b.due_date) >= date('now')
                  AND EXISTS (
                      SELECT 1 FROM customer_payments cp
                      WHERE cp.user_id = a.user_id
                        AND cp.status = 'paid'
                        AND CAST(cp.amount AS REAL) > 0
                  )
                  AND p.enabled = 1
                  {channel_filter}
                ORDER BY a.id ASC
                """,
                parameters,
            )
            try:
                rows = cursor.fetchall()
            finally:
                cursor.close()

        return [
            (account_from_row(row[:16]), execution_profile_from_row((row[1], row[0], *row[16:])))
            for row in rows
        ]

    def accounts_for_active_users(self) -> list[tuple[MT5Account, ExecutionProfile]]:
        """Accounts kept operational for heartbeat and existing-position management."""
        with connect_database(self.database_path) as connection:
            cursor = connection.execute(
                """
                SELECT a.id, a.user_id, a.broker_name, a.server_name, a.login,
                       a.encrypted_password, a.account_alias, a.terminal_path,
                       a.account_type, a.connection_status, a.last_error, a.last_connected_at,
                       a.account_mode, a.balance, a.equity, a.worker_heartbeat_at,
                       p.enabled, p.risk_mode, p.fixed_lot, p.risk_percent,
                       p.max_spread_points, p.max_slippage_points, p.daily_profit_target,
                       p.daily_loss_limit, p.max_open_signals, p.split_tps,
                       p.breakeven_enabled, p.trailing_enabled, p.entry_execution_mode,
                       p.entry_price_mode, p.pending_expiration_minutes, p.take_profit_limit
                FROM mt5_accounts a
                JOIN users u ON u.id = a.user_id
                JOIN execution_profiles p ON p.user_id = a.user_id AND p.mt5_account_id = a.id
                WHERE u.status = 'active'
                  AND p.enabled = 1
                ORDER BY a.id ASC
                """
            )
            try:
                rows = cursor.fetchall()
            finally:
                cursor.close()
        return [
            (account_from_row(row[:16]), execution_profile_from_row((row[1], row[0], *row[16:])))
            for row in rows
        ]

    def decrypted_password_for_account(self, account: MT5Account) -> str:
        self._require_credentials()
        return self.credential_service.decrypt_password(account.encrypted_password)

    def update_worker_heartbeat(self, user_id: int, account_id: int) -> None:
        with connect_database(self.database_path) as connection:
            cursor = connection.execute(
                """
                UPDATE mt5_accounts
                SET worker_heartbeat_at = ?, updated_at = ?
                WHERE id = ? AND user_id = ?
                """,
                (utc_now(), utc_now(), account_id, user_id),
            )
            cursor.close()

    def record_audit(self, user_id: int, action_type: str, payload: dict[str, object]) -> None:
        with connect_database(self.database_path) as connection:
            cursor = connection.execute(
                """
                INSERT INTO audit_events (user_id, action_type, payload, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (user_id, action_type, json.dumps(payload, sort_keys=True), utc_now()),
            )
            cursor.close()

    def _require_credentials(self) -> None:
        if self.credential_service is None:
            raise ValueError("MT5_CREDENTIAL_KEY nao configurada.")


def channel_selection_filter_sql() -> str:
    return """
      AND (
          COALESCE(
              (SELECT settings.selection_mode
               FROM user_channel_settings settings
               WHERE settings.user_id = a.user_id),
              'all'
          ) = 'all'
          OR EXISTS (
              SELECT 1
              FROM user_channel_subscriptions subscription
              JOIN source_channels channel
                ON channel.id = subscription.source_channel_id
              WHERE subscription.user_id = a.user_id
                AND subscription.enabled = 1
                AND channel.status = 'active'
                AND channel.telegram_chat_id = ?
          )
      )
    """


def account_from_row(row: tuple[object, ...]) -> MT5Account:
    terminal_path = Path(str(row[7])) if row[7] else None
    return MT5Account(
        id=int(row[0]),
        user_id=int(row[1]),
        broker_name=str(row[2]),
        server_name=str(row[3]),
        login=str(row[4]),
        encrypted_password=str(row[5]),
        account_alias=str(row[6]),
        terminal_path=terminal_path,
        account_type=str(row[8]),
        connection_status=str(row[9]),
        last_error=str(row[10]) if row[10] else None,
        last_connected_at=str(row[11]) if row[11] else None,
        account_mode=str(row[12]) if len(row) > 12 and row[12] else ACCOUNT_MODE_HEDGING,
        balance=Decimal(str(row[13])) if len(row) > 13 and row[13] is not None else None,
        equity=Decimal(str(row[14])) if len(row) > 14 and row[14] is not None else None,
        worker_heartbeat_at=str(row[15]) if len(row) > 15 and row[15] else None,
    )


def execution_profile_from_row(row: tuple[object, ...]) -> ExecutionProfile:
    return ExecutionProfile(
        user_id=int(row[0]),
        mt5_account_id=int(row[1]),
        enabled=bool(row[2]),
        risk_mode=str(row[3]),
        fixed_lot=Decimal(str(row[4])),
        risk_percent=Decimal(str(row[5])),
        max_spread_points=int(row[6]),
        max_slippage_points=int(row[7]),
        daily_profit_target=Decimal(str(row[8])),
        daily_loss_limit=Decimal(str(row[9])),
        max_open_signals=int(row[10]),
        split_tps=bool(row[11]),
        breakeven_enabled=bool(row[12]),
        trailing_enabled=bool(row[13]),
        entry_execution_mode=str(row[14]) if len(row) > 14 else ENTRY_EXECUTION_PENDING_ORDER,
        entry_price_mode=str(row[15]) if len(row) > 15 else ENTRY_PRICE_FIRST_TOUCH,
        pending_expiration_minutes=int(row[16]) if len(row) > 16 else 120,
        take_profit_limit=int(row[17]) if len(row) > 17 else 0,
    )


def require_text(value: str, field_name: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field_name} obrigatorio.")
    return normalized


def require_login(value: str) -> str:
    normalized = require_text(value, "login")
    if not normalized.isdigit():
        raise ValueError("login deve conter apenas numeros.")
    return normalized


def mt5_connection_error_message(client: object, fallback: str = "MT5 nao conseguiu conectar a conta.") -> str:
    last_error = None
    if hasattr(client, "last_error"):
        try:
            last_error = client.last_error()
        except Exception:
            last_error = None
    detail = sanitize_mt5_error(last_error)
    if detail:
        return f"{fallback} Detalhe MT5: {detail}"
    return fallback


def is_transient_ipc_error(client: object) -> bool:
    if not hasattr(client, "last_error"):
        return False
    try:
        last_error = client.last_error()
    except Exception:
        return False
    normalized = str(last_error).lower()
    return any(
        marker in normalized
        for marker in (
            "-10005",
            "ipc timeout",
            "-10001",
            "ipc send failed",
        )
    )


def sanitize_mt5_error(value: object | None) -> str:
    if value is None:
        return ""
    text = " ".join(str(value).split())
    if not text or "password" in text.lower() or "senha" in text.lower():
        return ""
    return text[:240]
