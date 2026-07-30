from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path

from .database import connect_database, initialize_database, utc_now

USER_STATUS_ACTIVE = "active"
USER_STATUS_PAUSED = "paused"
VALID_USER_STATUSES = {USER_STATUS_ACTIVE, USER_STATUS_PAUSED}


@dataclass(frozen=True)
class User:
    id: int
    telegram_user_id: int
    telegram_username: str | None
    status: str
    daily_signal_pause_until: str | None


class UserRepository:
    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path
        self._closed = False
        initialize_database(database_path)

    def close(self) -> None:
        self._closed = True

    def __enter__(self) -> "UserRepository":
        return self

    def __exit__(self, _exc_type: object, _exc: object, _traceback: object) -> None:
        self.close()

    def get_or_create_user(self, telegram_user_id: int, telegram_username: str | None) -> User:
        existing = self.get_by_telegram_user_id(telegram_user_id)
        if existing is not None:
            if existing.telegram_username != telegram_username:
                self.update_username(existing.id, telegram_username)
                return self.get_by_id(existing.id)
            return existing

        now = utc_now()
        with connect_database(self.database_path) as connection:
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
            try:
                user_id = int(cursor.lastrowid)
            finally:
                cursor.close()

        return self.get_by_id(user_id)

    def get_by_telegram_user_id(self, telegram_user_id: int) -> User | None:
        with connect_database(self.database_path) as connection:
            cursor = connection.execute(
                """
                SELECT id, telegram_user_id, telegram_username, status,
                       daily_signal_pause_until
                FROM users
                WHERE telegram_user_id = ?
                """,
                (telegram_user_id,),
            )
            try:
                row = cursor.fetchone()
            finally:
                cursor.close()

        return user_from_row(row) if row else None

    def get_by_id(self, user_id: int) -> User:
        with connect_database(self.database_path) as connection:
            cursor = connection.execute(
                """
                SELECT id, telegram_user_id, telegram_username, status,
                       daily_signal_pause_until
                FROM users
                WHERE id = ?
                """,
                (user_id,),
            )
            try:
                row = cursor.fetchone()
            finally:
                cursor.close()

        if row is None:
            raise ValueError("Usuario nao encontrado.")
        return user_from_row(row)

    def set_status(self, user_id: int, status: str) -> User:
        if status not in VALID_USER_STATUSES:
            raise ValueError("Status de usuario invalido.")

        with connect_database(self.database_path) as connection:
            cursor = connection.execute(
                "UPDATE users SET status = ?, updated_at = ? WHERE id = ?",
                (status, utc_now(), user_id),
            )
            cursor.close()

        return self.get_by_id(user_id)

    def set_daily_signal_pause_until(
        self,
        user_id: int,
        pause_until: str | None,
    ) -> User:
        with connect_database(self.database_path) as connection:
            cursor = connection.execute(
                """
                UPDATE users
                SET daily_signal_pause_until = ?, updated_at = ?
                WHERE id = ?
                """,
                (pause_until, utc_now(), user_id),
            )
            cursor.close()
        return self.get_by_id(user_id)

    def update_username(self, user_id: int, telegram_username: str | None) -> None:
        with connect_database(self.database_path) as connection:
            cursor = connection.execute(
                "UPDATE users SET telegram_username = ?, updated_at = ? WHERE id = ?",
                (telegram_username, utc_now(), user_id),
            )
            cursor.close()

    def log_admin_action(
        self,
        *,
        admin_telegram_user_id: int,
        action_type: str,
        payload: dict[str, object],
        target_user_id: int | None = None,
    ) -> None:
        with connect_database(self.database_path) as connection:
            cursor = connection.execute(
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
            cursor.close()

def user_from_row(row: tuple[object, ...]) -> User:
    return User(
        id=int(row[0]),
        telegram_user_id=int(row[1]),
        telegram_username=str(row[2]) if row[2] is not None else None,
        status=str(row[3]),
        daily_signal_pause_until=str(row[4]) if row[4] else None,
    )
