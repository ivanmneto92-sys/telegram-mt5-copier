from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
from typing import Iterator

from .models import DecisionStatus, IncomingMessage, TradeSignal, decimal_to_text

SQLITE_TIMEOUT_SECONDS = 30.0
DUPLICATE_WINDOW_MINUTES = 240
SIGNAL_MONITOR_SERVICE_NAME = "telegram_signal_monitor"


class SignalDatabase:
    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._closed = False

    def close(self) -> None:
        self._closed = True

    def __enter__(self) -> "SignalDatabase":
        return self

    def __exit__(self, _exc_type: object, _exc: object, _traceback: object) -> None:
        self.close()

    def initialize(self) -> None:
        initialize_database(self.database_path)

    def has_duplicate(
        self,
        content_signature: str,
        source_chat_id: int | str | None = None,
    ) -> bool:
        cutoff = (datetime.now(tz=timezone.utc) - timedelta(minutes=DUPLICATE_WINDOW_MINUTES)).isoformat()
        query = "SELECT 1 FROM signals WHERE content_signature = ? AND created_at >= ?"
        parameters: list[str] = [content_signature, cutoff]
        if source_chat_id is not None:
            query += " AND source_chat_id = ?"
            parameters.append(str(source_chat_id))
        query += " LIMIT 1"
        with connect_database(self.database_path) as connection:
            cursor = connection.execute(query, parameters)
            try:
                return cursor.fetchone() is not None
            finally:
                cursor.close()

    def claim_signal(
        self,
        content_signature: str,
        source_chat_id: int | str | None = None,
    ) -> bool:
        """Reserva atomicamente um sinal antes de publicar ou executar.

        A reserva fecha a janela entre consultar a duplicidade e gravar o sinal,
        inclusive quando mensagem nova/edicao ou dois monitores chegam juntos.
        """
        now = datetime.now(tz=timezone.utc)
        cutoff = (now - timedelta(minutes=DUPLICATE_WINDOW_MINUTES)).isoformat()
        source_key = str(source_chat_id) if source_chat_id is not None else ""
        with connect_database(self.database_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                "DELETE FROM signal_processing_claims WHERE claimed_at < ?",
                (cutoff,),
            )
            cursor.close()
            cursor = connection.execute(
                """
                SELECT 1 FROM signals
                WHERE content_signature = ?
                  AND (
                      source_chat_id = ?
                      OR (source_chat_id IS NULL AND ? = '')
                  )
                  AND created_at >= ?
                LIMIT 1
                """,
                (content_signature, source_key, source_key, cutoff),
            )
            try:
                if cursor.fetchone() is not None:
                    return False
            finally:
                cursor.close()
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO signal_processing_claims (
                    source_chat_id, content_signature, claimed_at
                ) VALUES (?, ?, ?)
                """,
                (source_key, content_signature, now.isoformat()),
            )
            try:
                return cursor.rowcount == 1
            finally:
                cursor.close()

    def has_accepted_source_message(
        self,
        source_chat_id: int | str | None,
        source_message_id: int | str | None,
    ) -> bool:
        if source_chat_id is None or source_message_id is None:
            return False
        with connect_database(self.database_path) as connection:
            cursor = connection.execute(
                """
                SELECT 1 FROM signals
                WHERE source_chat_id = ? AND source_message_id = ?
                LIMIT 1
                """,
                (str(source_chat_id), str(source_message_id)),
            )
            try:
                return cursor.fetchone() is not None
            finally:
                cursor.close()

    def record_accepted(self, signal: TradeSignal, formatted_message: str) -> None:
        now = utc_now()
        with connect_database(self.database_path) as connection:
            cursor = connection.execute(
                """
                INSERT INTO signals (
                    signature,
                    content_signature,
                    symbol,
                    direction,
                    entry_low,
                    entry_high,
                    stop_loss,
                    take_profits,
                    source_chat_id,
                    source_message_id,
                    raw_text,
                    formatted_message,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    signal.signature,
                    signal.content_signature,
                    signal.symbol,
                    signal.direction.value,
                    decimal_to_text(signal.entry_low),
                    decimal_to_text(signal.entry_high),
                    decimal_to_text(signal.stop_loss),
                    json.dumps([decimal_to_text(tp) for tp in signal.take_profits]),
                    as_text(signal.source_chat_id),
                    as_text(signal.source_message_id),
                    signal.raw_text,
                    formatted_message,
                    now,
                ),
            )
            cursor.close()
            cursor = connection.execute(
                """
                INSERT INTO signal_events (
                    status,
                    reason,
                    signature,
                    source_chat_id,
                    source_message_id,
                    raw_text,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    DecisionStatus.ACCEPTED.value,
                    "accepted",
                    signal.signature,
                    as_text(signal.source_chat_id),
                    as_text(signal.source_message_id),
                    signal.raw_text,
                    now,
                ),
            )
            cursor.close()

    def record_event(
        self,
        status: DecisionStatus,
        reason: str,
        *,
        incoming: IncomingMessage | None = None,
        signal: TradeSignal | None = None,
        raw_text: str | None = None,
    ) -> None:
        with connect_database(self.database_path) as connection:
            cursor = connection.execute(
                """
                INSERT INTO signal_events (
                    status,
                    reason,
                    signature,
                    source_chat_id,
                    source_message_id,
                    raw_text,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    status.value,
                    reason,
                    signal.signature if signal else None,
                    as_text(signal.source_chat_id if signal else incoming.source_chat_id if incoming else None),
                    as_text(
                        signal.source_message_id
                        if signal
                        else incoming.source_message_id
                        if incoming
                        else None
                    ),
                    raw_text if raw_text is not None else signal.raw_text if signal else None,
                    utc_now(),
                ),
            )
            cursor.close()

    def count_events(self, status: DecisionStatus | None = None, reason: str | None = None) -> int:
        query = "SELECT COUNT(*) FROM signal_events"
        params: list[str] = []
        clauses: list[str] = []
        if status is not None:
            clauses.append("status = ?")
            params.append(status.value)
        if reason is not None:
            clauses.append("reason = ?")
            params.append(reason)
        if clauses:
            query = f"{query} WHERE {' AND '.join(clauses)}"

        with connect_database(self.database_path) as connection:
            cursor = connection.execute(query, params)
            try:
                return int(cursor.fetchone()[0])
            finally:
                cursor.close()


def initialize_database(database_path: Path) -> None:
    database_path.parent.mkdir(parents=True, exist_ok=True)
    with connect_database(database_path) as connection:
        cursor = connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                signature TEXT NOT NULL UNIQUE,
                content_signature TEXT,
                symbol TEXT NOT NULL,
                direction TEXT NOT NULL,
                entry_low TEXT NOT NULL,
                entry_high TEXT NOT NULL,
                stop_loss TEXT NOT NULL,
                take_profits TEXT NOT NULL,
                source_chat_id TEXT,
                source_message_id TEXT,
                raw_text TEXT NOT NULL,
                formatted_message TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS signal_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                status TEXT NOT NULL,
                reason TEXT NOT NULL,
                signature TEXT,
                source_chat_id TEXT,
                source_message_id TEXT,
                raw_text TEXT,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_user_id INTEGER NOT NULL UNIQUE,
                telegram_username TEXT,
                status TEXT NOT NULL,
                daily_signal_pause_until TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS user_settings (
                user_id INTEGER PRIMARY KEY,
                risk_mode TEXT NOT NULL,
                fixed_lot TEXT NOT NULL,
                risk_percent TEXT NOT NULL,
                daily_profit_target TEXT NOT NULL,
                daily_loss_limit TEXT NOT NULL,
                max_open_trades INTEGER NOT NULL,
                tp_distribution_mode TEXT NOT NULL,
                breakeven_enabled INTEGER NOT NULL,
                trailing_enabled INTEGER NOT NULL,
                tp1_breakeven_enabled INTEGER NOT NULL DEFAULT 1,
                avoid_high_impact_news INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id)
            );

            CREATE TABLE IF NOT EXISTS commands (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                command_type TEXT NOT NULL,
                payload TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                executed_at TEXT,
                error_message TEXT,
                FOREIGN KEY (user_id) REFERENCES users(id)
            );

            CREATE TABLE IF NOT EXISTS admin_actions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                admin_telegram_user_id INTEGER NOT NULL,
                target_user_id INTEGER,
                action_type TEXT NOT NULL,
                payload TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS mt5_accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                broker_name TEXT NOT NULL,
                server_name TEXT NOT NULL,
                login TEXT NOT NULL,
                encrypted_password TEXT NOT NULL,
                account_alias TEXT NOT NULL,
                terminal_path TEXT,
                account_type TEXT NOT NULL,
                account_mode TEXT NOT NULL DEFAULT 'hedging',
                connection_status TEXT NOT NULL,
                last_error TEXT,
                last_connected_at TEXT,
                balance TEXT,
                equity TEXT,
                worker_heartbeat_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id),
                UNIQUE (user_id, login, server_name)
            );

            CREATE TABLE IF NOT EXISTS execution_profiles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                mt5_account_id INTEGER NOT NULL,
                enabled INTEGER NOT NULL,
                risk_mode TEXT NOT NULL,
                fixed_lot TEXT NOT NULL,
                risk_percent TEXT NOT NULL,
                max_spread_points INTEGER NOT NULL,
                max_slippage_points INTEGER NOT NULL,
                daily_profit_target TEXT NOT NULL,
                daily_loss_limit TEXT NOT NULL,
                max_open_signals INTEGER NOT NULL,
                split_tps INTEGER NOT NULL,
                breakeven_enabled INTEGER NOT NULL,
                trailing_enabled INTEGER NOT NULL,
                entry_execution_mode TEXT NOT NULL DEFAULT 'pending_order',
                entry_price_mode TEXT NOT NULL DEFAULT 'first_touch',
                pending_expiration_minutes INTEGER NOT NULL DEFAULT 120,
                take_profit_limit INTEGER NOT NULL DEFAULT 0,
                tp1_breakeven_enabled INTEGER NOT NULL DEFAULT 1,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id),
                FOREIGN KEY (mt5_account_id) REFERENCES mt5_accounts(id),
                UNIQUE (user_id, mt5_account_id)
            );

            CREATE TABLE IF NOT EXISTS executions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                signal_id TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                mt5_account_id INTEGER NOT NULL,
                status TEXT NOT NULL,
                symbol TEXT NOT NULL,
                direction TEXT NOT NULL,
                requested_volume TEXT NOT NULL,
                executed_volume TEXT NOT NULL,
                entry_price TEXT NOT NULL,
                stop_loss TEXT NOT NULL,
                take_profit TEXT NOT NULL,
                terminal_ticket TEXT,
                error_code TEXT,
                error_message TEXT,
                created_at TEXT NOT NULL,
                executed_at TEXT,
                FOREIGN KEY (user_id) REFERENCES users(id),
                FOREIGN KEY (mt5_account_id) REFERENCES mt5_accounts(id),
                UNIQUE (signal_id, user_id, mt5_account_id, take_profit)
            );

            CREATE TABLE IF NOT EXISTS execution_groups (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                signal_id TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                mt5_account_id INTEGER NOT NULL,
                status TEXT NOT NULL,
                direction TEXT NOT NULL,
                symbol TEXT NOT NULL,
                entry_low TEXT NOT NULL,
                entry_high TEXT NOT NULL,
                selected_entry_price TEXT NOT NULL,
                order_type TEXT NOT NULL,
                total_volume TEXT NOT NULL,
                stop_loss TEXT NOT NULL,
                expiration_at TEXT NOT NULL,
                execution_mode TEXT NOT NULL,
                signal_received_at TEXT NOT NULL,
                pending_created_at TEXT NOT NULL,
                telegram_message_at TEXT,
                listener_received_at TEXT,
                signal_parsed_at TEXT,
                execution_planned_at TEXT,
                validation_started_at TEXT,
                validation_finished_at TEXT,
                mt5_order_sent_at TEXT,
                broker_response_at TEXT,
                telegram_to_listener_ms INTEGER,
                parsing_ms INTEGER,
                planning_ms INTEGER,
                validation_ms INTEGER,
                mt5_submission_ms INTEGER,
                broker_response_ms INTEGER,
                total_ms INTEGER,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                error_code TEXT,
                error_message TEXT,
                tp1_reached_at TEXT,
                breakeven_applied_at TEXT,
                FOREIGN KEY (user_id) REFERENCES users(id),
                FOREIGN KEY (mt5_account_id) REFERENCES mt5_accounts(id),
                UNIQUE (signal_id, user_id, mt5_account_id)
            );

            CREATE TABLE IF NOT EXISTS execution_orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                execution_group_id INTEGER NOT NULL,
                tp_index INTEGER NOT NULL,
                requested_volume TEXT NOT NULL,
                normalized_volume TEXT NOT NULL,
                entry_price TEXT NOT NULL,
                stop_loss TEXT NOT NULL,
                take_profit TEXT NOT NULL,
                order_type TEXT NOT NULL,
                status TEXT NOT NULL,
                mt5_order_ticket TEXT UNIQUE,
                mt5_position_ticket TEXT,
                broker_retcode TEXT,
                broker_message TEXT,
                submitted_at TEXT,
                filled_at TEXT,
                closed_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (execution_group_id) REFERENCES execution_groups(id),
                UNIQUE (execution_group_id, tp_index)
            );

            CREATE TABLE IF NOT EXISTS execution_notifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_type TEXT NOT NULL,
                execution_group_id INTEGER NOT NULL,
                execution_order_id INTEGER,
                created_at TEXT NOT NULL,
                FOREIGN KEY (execution_group_id) REFERENCES execution_groups(id),
                FOREIGN KEY (execution_order_id) REFERENCES execution_orders(id),
                UNIQUE (event_type, execution_group_id, execution_order_id)
            );

            CREATE TABLE IF NOT EXISTS economic_calendar_events (
                provider TEXT NOT NULL,
                provider_event_id TEXT NOT NULL,
                event_at TEXT NOT NULL,
                country TEXT NOT NULL,
                currency TEXT NOT NULL,
                event_name TEXT NOT NULL,
                category TEXT,
                importance INTEGER NOT NULL,
                date_span INTEGER NOT NULL DEFAULT 0,
                previous_value TEXT,
                forecast_value TEXT,
                actual_value TEXT,
                source_url TEXT,
                fetched_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (provider, provider_event_id)
            );

            CREATE TABLE IF NOT EXISTS economic_calendar_notifications (
                provider TEXT NOT NULL,
                provider_event_id TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                phase TEXT NOT NULL,
                sent_at TEXT NOT NULL,
                PRIMARY KEY (provider, provider_event_id, user_id, phase),
                FOREIGN KEY (user_id) REFERENCES users(id)
            );

            CREATE TABLE IF NOT EXISTS audit_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                action_type TEXT NOT NULL,
                payload TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id)
            );

            CREATE TABLE IF NOT EXISTS user_notification_settings (
                user_id INTEGER PRIMARY KEY,
                result_mode TEXT NOT NULL DEFAULT 'all',
                updated_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id)
            );

            CREATE TABLE IF NOT EXISTS execution_close_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                mt5_account_id INTEGER NOT NULL,
                execution_group_id INTEGER NOT NULL,
                execution_order_id INTEGER NOT NULL,
                mt5_deal_ticket TEXT NOT NULL UNIQUE,
                close_reason TEXT NOT NULL,
                close_price TEXT NOT NULL,
                gross_profit TEXT NOT NULL,
                commission TEXT NOT NULL,
                swap TEXT NOT NULL,
                fee TEXT NOT NULL,
                net_profit TEXT NOT NULL,
                closed_at TEXT NOT NULL,
                notification_status TEXT NOT NULL DEFAULT 'pending',
                notification_attempts INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id),
                FOREIGN KEY (mt5_account_id) REFERENCES mt5_accounts(id),
                FOREIGN KEY (execution_group_id) REFERENCES execution_groups(id),
                FOREIGN KEY (execution_order_id) REFERENCES execution_orders(id)
            );

            CREATE TABLE IF NOT EXISTS mt5_settlement_state (
                mt5_account_id INTEGER PRIMARY KEY,
                last_reconciled_at TEXT NOT NULL,
                FOREIGN KEY (mt5_account_id) REFERENCES mt5_accounts(id)
            );

            CREATE TABLE IF NOT EXISTS signal_processing_claims (
                source_chat_id TEXT NOT NULL,
                content_signature TEXT NOT NULL,
                claimed_at TEXT NOT NULL,
                PRIMARY KEY (source_chat_id, content_signature)
            );

            CREATE TABLE IF NOT EXISTS customer_billing (
                user_id INTEGER PRIMARY KEY,
                customer_name TEXT,
                email TEXT,
                phone TEXT,
                plan_name TEXT NOT NULL DEFAULT 'Mensal',
                monthly_amount TEXT NOT NULL DEFAULT '0',
                due_date TEXT,
                billing_status TEXT NOT NULL DEFAULT 'pending',
                last_paid_at TEXT,
                notes TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id)
            );

            CREATE TABLE IF NOT EXISTS customer_payments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                amount TEXT NOT NULL,
                paid_at TEXT NOT NULL,
                period_start TEXT,
                period_end TEXT,
                method TEXT,
                reference TEXT,
                status TEXT NOT NULL DEFAULT 'paid',
                admin_telegram_user_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id)
            );

            CREATE TABLE IF NOT EXISTS admin_login_tokens (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                token_hash TEXT NOT NULL UNIQUE,
                admin_telegram_user_id INTEGER NOT NULL,
                expires_at TEXT NOT NULL,
                used_at TEXT,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS admin_browser_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_hash TEXT NOT NULL UNIQUE,
                admin_telegram_user_id INTEGER NOT NULL,
                expires_at TEXT NOT NULL,
                revoked_at TEXT,
                last_seen_at TEXT,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS source_channels (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_chat_id TEXT UNIQUE,
                username TEXT,
                title TEXT NOT NULL,
                display_name TEXT,
                canonical_link TEXT,
                status TEXT NOT NULL DEFAULT 'pending_review',
                access_status TEXT NOT NULL DEFAULT 'pending',
                content_protected INTEGER NOT NULL DEFAULT 0,
                history_accessible INTEGER NOT NULL DEFAULT 0,
                last_message_id TEXT,
                last_error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                validated_at TEXT,
                approved_at TEXT,
                approved_by_admin_id INTEGER
            );

            CREATE TABLE IF NOT EXISTS channel_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                source_channel_id INTEGER,
                submitted_link TEXT NOT NULL,
                normalized_key TEXT NOT NULL,
                username TEXT,
                canonical_link TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending_access',
                admin_notes TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id),
                FOREIGN KEY (source_channel_id) REFERENCES source_channels(id)
            );

            CREATE TABLE IF NOT EXISTS user_channel_settings (
                user_id INTEGER PRIMARY KEY,
                selection_mode TEXT NOT NULL DEFAULT 'all',
                updated_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id)
            );

            CREATE TABLE IF NOT EXISTS user_channel_subscriptions (
                user_id INTEGER NOT NULL,
                source_channel_id INTEGER NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (user_id, source_channel_id),
                FOREIGN KEY (user_id) REFERENCES users(id),
                FOREIGN KEY (source_channel_id) REFERENCES source_channels(id)
            );

            CREATE TABLE IF NOT EXISTS service_heartbeats (
                service_name TEXT PRIMARY KEY,
                heartbeat_at TEXT NOT NULL,
                details TEXT
            );

            CREATE TABLE IF NOT EXISTS operational_alert_states (
                alert_key TEXT PRIMARY KEY,
                alert_type TEXT NOT NULL,
                entity_id TEXT,
                summary TEXT NOT NULL,
                is_active INTEGER NOT NULL DEFAULT 1,
                opened_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                last_notified_at TEXT,
                resolved_at TEXT
            );

            CREATE TABLE IF NOT EXISTS account_daily_performance (
                mt5_account_id INTEGER NOT NULL,
                performance_date TEXT NOT NULL,
                realized_profit TEXT NOT NULL,
                gross_profit TEXT,
                trading_costs TEXT,
                starting_balance TEXT,
                return_percent TEXT,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (mt5_account_id, performance_date),
                FOREIGN KEY (mt5_account_id) REFERENCES mt5_accounts(id)
            );

            CREATE INDEX IF NOT EXISTS idx_signal_events_status
                ON signal_events(status);

            CREATE INDEX IF NOT EXISTS idx_signal_events_signature
                ON signal_events(signature);

            CREATE INDEX IF NOT EXISTS idx_commands_user_status
                ON commands(user_id, status);

            CREATE INDEX IF NOT EXISTS idx_admin_actions_admin
                ON admin_actions(admin_telegram_user_id);

            CREATE INDEX IF NOT EXISTS idx_mt5_accounts_user
                ON mt5_accounts(user_id);

            CREATE INDEX IF NOT EXISTS idx_mt5_accounts_status
                ON mt5_accounts(connection_status);

            CREATE INDEX IF NOT EXISTS idx_execution_profiles_user_account
                ON execution_profiles(user_id, mt5_account_id);

            CREATE INDEX IF NOT EXISTS idx_executions_signal
                ON executions(signal_id);

            CREATE INDEX IF NOT EXISTS idx_executions_user_account
                ON executions(user_id, mt5_account_id);

            CREATE INDEX IF NOT EXISTS idx_execution_groups_status
                ON execution_groups(status);

            CREATE INDEX IF NOT EXISTS idx_execution_groups_user_account
                ON execution_groups(user_id, mt5_account_id);

            CREATE INDEX IF NOT EXISTS idx_execution_orders_group
                ON execution_orders(execution_group_id);

            CREATE INDEX IF NOT EXISTS idx_execution_orders_status
                ON execution_orders(status);

            CREATE INDEX IF NOT EXISTS idx_execution_close_events_delivery
                ON execution_close_events(notification_status, mt5_account_id);

            CREATE INDEX IF NOT EXISTS idx_audit_events_user
                ON audit_events(user_id);

            CREATE INDEX IF NOT EXISTS idx_customer_billing_due
                ON customer_billing(due_date, billing_status);

            CREATE INDEX IF NOT EXISTS idx_customer_payments_user_paid
                ON customer_payments(user_id, paid_at);

            CREATE INDEX IF NOT EXISTS idx_admin_login_tokens_expiration
                ON admin_login_tokens(expires_at, used_at);

            CREATE INDEX IF NOT EXISTS idx_admin_browser_sessions_expiration
                ON admin_browser_sessions(expires_at, revoked_at);

            CREATE UNIQUE INDEX IF NOT EXISTS idx_source_channels_username
                ON source_channels(username COLLATE NOCASE)
                WHERE username IS NOT NULL;

            CREATE INDEX IF NOT EXISTS idx_source_channels_status
                ON source_channels(status, access_status);

            CREATE INDEX IF NOT EXISTS idx_channel_requests_status
                ON channel_requests(status, normalized_key);

            CREATE INDEX IF NOT EXISTS idx_channel_requests_user
                ON channel_requests(user_id, created_at);

            CREATE INDEX IF NOT EXISTS idx_user_channel_subscriptions_channel
                ON user_channel_subscriptions(source_channel_id, enabled);
            """
        )
        cursor.close()
        run_schema_migrations(connection)


@contextmanager
def connect_database(database_path: Path) -> Iterator[sqlite3.Connection]:
    connection: sqlite3.Connection | None = sqlite3.connect(
        database_path,
        timeout=SQLITE_TIMEOUT_SECONDS,
    )
    try:
        cursor = connection.execute("PRAGMA foreign_keys = ON")
        cursor.close()
        yield connection
        connection.commit()
    except Exception:
        if connection is not None:
            connection.rollback()
        raise
    finally:
        if connection is not None:
            connection.close()
            connection = None


def utc_now() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def update_service_heartbeat(
    database_path: Path,
    service_name: str,
    *,
    details: str | None = None,
) -> str:
    heartbeat_at = utc_now()
    with connect_database(database_path) as connection:
        cursor = connection.execute(
            """
            INSERT INTO service_heartbeats (service_name, heartbeat_at, details)
            VALUES (?, ?, ?)
            ON CONFLICT(service_name) DO UPDATE SET
                heartbeat_at = excluded.heartbeat_at,
                details = excluded.details
            """,
            (service_name, heartbeat_at, details),
        )
        cursor.close()
    return heartbeat_at


def get_service_heartbeat(database_path: Path, service_name: str) -> str | None:
    with connect_database(database_path) as connection:
        cursor = connection.execute(
            "SELECT heartbeat_at FROM service_heartbeats WHERE service_name = ?",
            (service_name,),
        )
        try:
            row = cursor.fetchone()
        finally:
            cursor.close()
    return str(row[0]) if row is not None else None


def as_text(value: int | str | None) -> str | None:
    if value is None:
        return None
    return str(value)


def run_schema_migrations(connection: sqlite3.Connection) -> None:
    ensure_column(connection, "users", "daily_signal_pause_until", "TEXT")
    ensure_column(connection, "mt5_accounts", "account_mode", "TEXT NOT NULL DEFAULT 'hedging'")
    ensure_column(connection, "mt5_accounts", "balance", "TEXT")
    ensure_column(connection, "mt5_accounts", "equity", "TEXT")
    ensure_column(connection, "signals", "content_signature", "TEXT")
    cursor = connection.execute(
        "UPDATE signals SET content_signature = signature WHERE content_signature IS NULL"
    )
    cursor.close()
    ensure_column(connection, "mt5_accounts", "worker_heartbeat_at", "TEXT")
    ensure_column(
        connection,
        "execution_profiles",
        "entry_execution_mode",
        "TEXT NOT NULL DEFAULT 'pending_order'",
    )
    ensure_column(
        connection,
        "execution_profiles",
        "entry_price_mode",
        "TEXT NOT NULL DEFAULT 'first_touch'",
    )
    ensure_column(
        connection,
        "execution_profiles",
        "pending_expiration_minutes",
        "INTEGER NOT NULL DEFAULT 120",
    )
    ensure_column(
        connection,
        "execution_profiles",
        "take_profit_limit",
        "INTEGER NOT NULL DEFAULT 0",
    )
    ensure_column(
        connection,
        "user_settings",
        "tp1_breakeven_enabled",
        "INTEGER NOT NULL DEFAULT 1",
    )
    ensure_column(
        connection,
        "user_settings",
        "avoid_high_impact_news",
        "INTEGER NOT NULL DEFAULT 0",
    )
    ensure_column(
        connection,
        "execution_profiles",
        "tp1_breakeven_enabled",
        "INTEGER NOT NULL DEFAULT 1",
    )
    ensure_column(connection, "execution_groups", "tp1_reached_at", "TEXT")
    ensure_column(connection, "execution_groups", "breakeven_applied_at", "TEXT")
    ensure_column(connection, "source_channels", "display_name", "TEXT")
    ensure_column(connection, "account_daily_performance", "gross_profit", "TEXT")
    ensure_column(connection, "account_daily_performance", "trading_costs", "TEXT")
    ensure_column(connection, "execution_orders", "close_reason", "TEXT")
    ensure_column(connection, "execution_orders", "close_price", "TEXT")
    ensure_column(connection, "execution_orders", "gross_profit", "TEXT")
    ensure_column(connection, "execution_orders", "commission", "TEXT")
    ensure_column(connection, "execution_orders", "swap", "TEXT")
    ensure_column(connection, "execution_orders", "fee", "TEXT")
    ensure_column(connection, "execution_orders", "net_profit", "TEXT")
    ensure_column(connection, "execution_orders", "mt5_close_deal_ticket", "TEXT")
    ensure_column(
        connection,
        "execution_orders",
        "be_status",
        "TEXT NOT NULL DEFAULT 'not_required'",
    )
    ensure_column(connection, "execution_orders", "be_requested_at", "TEXT")
    ensure_column(connection, "execution_orders", "be_applied_at", "TEXT")
    ensure_column(connection, "execution_orders", "be_target_sl", "TEXT")
    ensure_column(connection, "execution_orders", "be_confirmed_sl", "TEXT")
    ensure_column(connection, "execution_orders", "be_last_error", "TEXT")
    ensure_column(
        connection,
        "execution_orders",
        "be_attempts",
        "INTEGER NOT NULL DEFAULT 0",
    )


def ensure_column(connection: sqlite3.Connection, table_name: str, column_name: str, definition: str) -> None:
    cursor = connection.execute(f"PRAGMA table_info({table_name})")
    try:
        existing_columns = {str(row[1]) for row in cursor.fetchall()}
    finally:
        cursor.close()

    if column_name in existing_columns:
        return

    cursor = connection.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {definition}")
    cursor.close()
