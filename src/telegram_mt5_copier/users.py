from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import sqlite3

from .database import initialize_database, utc_now

USER_STATUS_ACTIVE = "active"
USER_STATUS_PAUSED = "paused"
VALID_USER_STATUSES = {USER_STATUS_ACTIVE, USER_STATUS_PAUSED}


@dataclass(frozen=True)
class User:
    id: int
    telegram_user_id: int
    telegram_username: str | None
    status: str


class UserRepository:
    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path
        initialize_database(database_path)

    def get_or_create_user(self, telegram_user_id: int, telegram_username: str | None) -> User:
        existing = self.get_by_telegram_user_id(telegram_user_id)
        if existing is not None:
            if existing.telegram_username != telegram_username:
                self.update_username(existing.id, telegram_username)
                return self.get_by_id(existing.id)
            return existing

        now = utc_now()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO users (
                    telegram_user_id,
                    telegram_username,
                    status,
                    created_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                (telegram_user_id, telegram_username, USER_STATUS_PAUSED, now, now),
            )
            user_id = int(cursor.lastrowid)

        return self.get_by_id(user_id)

    def get_by_telegram_user_id(self, telegram_user_id: int) -> User | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT id, telegram_user_id, telegram_username, status
                FROM users
                WHERE telegram_user_id = ?
                """,
                (telegram_user_id,),
            ).fetchone()

        return user_from_row(row) if row else None

    def get_by_id(self, user_id: int) -> User:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT id, telegram_user_id, telegram_username, status
                FROM users
                WHERE id = ?
                """,
                (user_id,),
            ).fetchone()

        if row is None:
            raise ValueError("Usuario nao encontrado.")
        return user_from_row(row)

    def set_status(self, user_id: int, status: str) -> User:
        if status not in VALID_USER_STATUSES:
            raise ValueError("Status de usuario invalido.")

        with self._connect() as connection:
            connection.execute(
                "UPDATE users SET status = ?, updated_at = ? WHERE id = ?",
                (status, utc_now(), user_id),
            )

        return self.get_by_id(user_id)

    def update_username(self, user_id: int, telegram_username: str | None) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE users SET telegram_username = ?, updated_at = ? WHERE id = ?",
                (telegram_username, utc_now(), user_id),
            )

    def log_admin_action(
        self,
        *,
        admin_telegram_user_id: int,
        action_type: str,
        payload: dict[str, object],
        target_user_id: int | None = None,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO admin_actions (
                    admin_telegram_user_id,
                    target_user_id,
                    action_type,
                    payload,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    admin_telegram_user_id,
                    target_user_id,
                    action_type,
                    json.dumps(payload, sort_keys=True),
                    utc_now(),
                ),
            )

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.database_path)


def user_from_row(row: tuple[int, int, str | None, str]) -> User:
    return User(
        id=int(row[0]),
        telegram_user_id=int(row[1]),
        telegram_username=row[2],
        status=row[3],
    )
