from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import sqlite3
from typing import Any

from .database import initialize_database, utc_now

COMMAND_STATUS_PENDING = "pending"


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
        initialize_database(database_path)

    def enqueue(self, user_id: int, command_type: str, payload: dict[str, Any]) -> Command:
        payload_text = encode_payload(payload)
        existing = self.find_pending_duplicate(user_id, command_type, payload_text)
        if existing is not None:
            return existing

        with self._connect() as connection:
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
            command_id = int(cursor.lastrowid)

        return Command(
            id=command_id,
            user_id=user_id,
            command_type=command_type,
            payload=payload,
            status=COMMAND_STATUS_PENDING,
            created=True,
        )

    def find_pending_duplicate(self, user_id: int, command_type: str, payload_text: str) -> Command | None:
        with self._connect() as connection:
            row = connection.execute(
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
            ).fetchone()

        if row is None:
            return None
        return command_from_row(row, created=False)

    def count(self, user_id: int | None = None) -> int:
        query = "SELECT COUNT(*) FROM commands"
        params: tuple[int, ...] = ()
        if user_id is not None:
            query = f"{query} WHERE user_id = ?"
            params = (user_id,)

        with self._connect() as connection:
            return int(connection.execute(query, params).fetchone()[0])

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.database_path)


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
