from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any

from .database import connect_database, initialize_database, utc_now

COMMAND_STATUS_PENDING = "pending"
COMMAND_STATUS_DONE = "done"
COMMAND_STATUS_FAILED = "failed"


@dataclass(frozen=True)
class Command:
    id: int
    user_id: int
    command_type: str
    payload: dict[str, Any]
    status: str
    created: bool


class CommandQueue:
    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path
        self._closed = False
        initialize_database(database_path)

    def close(self) -> None:
        self._closed = True

    def __enter__(self) -> "CommandQueue":
        return self

    def __exit__(self, _exc_type: object, _exc: object, _traceback: object) -> None:
        self.close()

    def enqueue(self, user_id: int, command_type: str, payload: dict[str, Any]) -> Command:
        payload_text = encode_payload(payload)
        existing = self.find_pending_duplicate(user_id, command_type, payload_text)
        if existing is not None:
            return existing

        with connect_database(self.database_path) as connection:
            cursor = connection.execute(
                """
                INSERT INTO commands (
                    user_id,
                    command_type,
                    payload,
                    status,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                (user_id, command_type, payload_text, COMMAND_STATUS_PENDING, utc_now()),
            )
            try:
                command_id = int(cursor.lastrowid)
            finally:
                cursor.close()

        return Command(
            id=command_id,
            user_id=user_id,
            command_type=command_type,
            payload=payload,
            status=COMMAND_STATUS_PENDING,
            created=True,
        )

    def find_pending_duplicate(self, user_id: int, command_type: str, payload_text: str) -> Command | None:
        with connect_database(self.database_path) as connection:
            cursor = connection.execute(
                """
                SELECT id, user_id, command_type, payload, status
                FROM commands
                WHERE user_id = ?
                  AND command_type = ?
                  AND payload = ?
                  AND status = ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (user_id, command_type, payload_text, COMMAND_STATUS_PENDING),
            )
            try:
                row = cursor.fetchone()
            finally:
                cursor.close()

        if row is None:
            return None
        return command_from_row(row, created=False)

    def count(self, user_id: int | None = None) -> int:
        query = "SELECT COUNT(*) FROM commands"
        params: tuple[int, ...] = ()
        if user_id is not None:
            query = f"{query} WHERE user_id = ?"
            params = (user_id,)

        with connect_database(self.database_path) as connection:
            cursor = connection.execute(query, params)
            try:
                return int(cursor.fetchone()[0])
            finally:
                cursor.close()

    def recent_for_user(self, user_id: int, limit: int = 10) -> list[dict[str, str]]:
        with connect_database(self.database_path) as connection:
            cursor = connection.execute(
                """
                SELECT command_type, payload, status, created_at
                FROM commands
                WHERE user_id = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (user_id, limit),
            )
            try:
                rows = cursor.fetchall()
            finally:
                cursor.close()

        return [
            {
                "command_type": str(row[0]),
                "payload": str(row[1]),
                "status": str(row[2]),
                "created_at": str(row[3]),
            }
            for row in rows
        ]

    def pending_for_account(self, user_id: int, account_id: int) -> list[Command]:
        with connect_database(self.database_path) as connection:
            cursor = connection.execute(
                """
                SELECT id, user_id, command_type, payload, status
                FROM commands
                WHERE user_id = ? AND status = ?
                ORDER BY id ASC
                """,
                (user_id, COMMAND_STATUS_PENDING),
            )
            try:
                rows = cursor.fetchall()
            finally:
                cursor.close()

        commands = [command_from_row(row, created=False) for row in rows]
        return [
            command
            for command in commands
            if payload_account_id(command.payload) == account_id
        ]

    def mark_done(self, command_id: int) -> None:
        self._mark_status(command_id, COMMAND_STATUS_DONE, None)

    def mark_failed(self, command_id: int, error_message: str) -> None:
        self._mark_status(command_id, COMMAND_STATUS_FAILED, error_message)

    def _mark_status(self, command_id: int, status: str, error_message: str | None) -> None:
        with connect_database(self.database_path) as connection:
            cursor = connection.execute(
                """
                UPDATE commands
                SET status = ?, executed_at = ?, error_message = ?
                WHERE id = ?
                """,
                (status, utc_now(), error_message, command_id),
            )
            cursor.close()

def encode_payload(payload: dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def command_from_row(row: tuple[object, ...], *, created: bool) -> Command:
    return Command(
        id=int(row[0]),
        user_id=int(row[1]),
        command_type=str(row[2]),
        payload=json.loads(str(row[3])),
        status=str(row[4]),
        created=created,
    )


def payload_account_id(payload: dict[str, Any]) -> int | None:
    raw_value = payload.get("mt5_account_id", payload.get("account_id"))
    try:
        return int(raw_value)
    except (TypeError, ValueError):
        return None
