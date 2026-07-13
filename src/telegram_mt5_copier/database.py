from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from typing import Iterator

from .models import DecisionStatus, IncomingMessage, TradeSignal, decimal_to_text

SQLITE_TIMEOUT_SECONDS = 30.0


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

    def has_duplicate(self, signature: str) -> bool:
        with connect_database(self.database_path) as connection:
            cursor = connection.execute(
                "SELECT 1 FROM signals WHERE signature = ? LIMIT 1",
                (signature,),
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
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    signal.signature,
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

            CREATE INDEX IF NOT EXISTS idx_signal_events_status
                ON signal_events(status);

            CREATE INDEX IF NOT EXISTS idx_signal_events_signature
                ON signal_events(signature);

            CREATE INDEX IF NOT EXISTS idx_commands_user_status
                ON commands(user_id, status);

            CREATE INDEX IF NOT EXISTS idx_admin_actions_admin
                ON admin_actions(admin_telegram_user_id);
            """
        )
        cursor.close()


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


def as_text(value: int | str | None) -> str | None:
    if value is None:
        return None
    return str(value)
