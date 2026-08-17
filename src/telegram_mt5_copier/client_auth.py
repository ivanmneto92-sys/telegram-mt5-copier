from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
import secrets
from urllib.parse import urlsplit, urlunsplit

from .admin_auth import token_hash
from .database import connect_database, initialize_database, utc_now


@dataclass(frozen=True)
class BrowserClientSession:
    user_id: int
    session_token: str
    expires_at: str


class ClientBrowserAuthService:
    """Emite links descartaveis e sessoes web vinculadas a um unico cliente."""

    def __init__(
        self,
        database_path: Path,
        *,
        login_ttl_minutes: int = 5,
        session_ttl_hours: int = 12,
    ) -> None:
        self.database_path = database_path
        self.login_ttl_minutes = login_ttl_minutes
        self.session_ttl_hours = session_ttl_hours
        initialize_database(database_path)

    def create_login_url(self, user_id: int, app_url: str) -> str:
        parsed = urlsplit(app_url)
        if parsed.scheme != "https" or not parsed.netloc:
            raise ValueError("URL HTTPS do aplicativo do cliente nao configurada.")
        now = datetime.now(tz=timezone.utc)
        raw_token = secrets.token_urlsafe(32)
        expires_at = now + timedelta(minutes=self.login_ttl_minutes)
        with connect_database(self.database_path) as connection:
            exists = connection.execute(
                "SELECT 1 FROM users WHERE id = ?", (user_id,)
            ).fetchone()
            if exists is None:
                raise ValueError("Cliente nao encontrado.")
            connection.execute(
                """
                INSERT INTO client_login_tokens (
                    token_hash, user_id, expires_at, used_at, created_at
                ) VALUES (?, ?, ?, NULL, ?)
                """,
                (token_hash(raw_token), user_id, expires_at.isoformat(), now.isoformat()),
            )
            connection.execute(
                "DELETE FROM client_login_tokens WHERE expires_at < ? OR used_at IS NOT NULL",
                ((now - timedelta(days=1)).isoformat(),),
            )
        # O fragmento nao e enviado ao servidor HTTP nem aparece em access logs.
        return urlunsplit(
            (parsed.scheme, parsed.netloc, parsed.path or "/", parsed.query, f"token={raw_token}")
        )

    def consume_login_token(self, raw_token: str) -> BrowserClientSession:
        if not raw_token or len(raw_token) > 256:
            raise ValueError("Link de acesso invalido.")
        now = datetime.now(tz=timezone.utc)
        with connect_database(self.database_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT id, user_id, expires_at, used_at
                FROM client_login_tokens WHERE token_hash = ?
                """,
                (token_hash(raw_token),),
            ).fetchone()
            if row is None or row[3] is not None or datetime.fromisoformat(str(row[2])) <= now:
                raise ValueError("Link de acesso invalido ou expirado.")
            update = connection.execute(
                "UPDATE client_login_tokens SET used_at = ? WHERE id = ? AND used_at IS NULL",
                (now.isoformat(), int(row[0])),
            )
            if update.rowcount != 1:
                raise ValueError("Link de acesso invalido ou expirado.")
            session_token = secrets.token_urlsafe(48)
            expires_at = now + timedelta(hours=self.session_ttl_hours)
            connection.execute(
                """
                INSERT INTO client_browser_sessions (
                    session_hash, user_id, expires_at, revoked_at, last_seen_at, created_at
                ) VALUES (?, ?, ?, NULL, ?, ?)
                """,
                (
                    token_hash(session_token), int(row[1]), expires_at.isoformat(),
                    now.isoformat(), now.isoformat(),
                ),
            )
        return BrowserClientSession(int(row[1]), session_token, expires_at.isoformat())

    def authenticate_session(self, session_token: str) -> int:
        if not session_token or len(session_token) > 256:
            raise ValueError("Sessao do cliente invalida.")
        now = datetime.now(tz=timezone.utc)
        with connect_database(self.database_path) as connection:
            row = connection.execute(
                """
                SELECT id, user_id, expires_at FROM client_browser_sessions
                WHERE session_hash = ? AND revoked_at IS NULL
                """,
                (token_hash(session_token),),
            ).fetchone()
            if row is None or datetime.fromisoformat(str(row[2])) <= now:
                raise ValueError("Sessao do cliente expirada.")
            connection.execute(
                "UPDATE client_browser_sessions SET last_seen_at = ? WHERE id = ?",
                (now.isoformat(), int(row[0])),
            )
        return int(row[1])

    def revoke_session(self, session_token: str) -> None:
        if not session_token:
            return
        with connect_database(self.database_path) as connection:
            connection.execute(
                """
                UPDATE client_browser_sessions SET revoked_at = ?
                WHERE session_hash = ? AND revoked_at IS NULL
                """,
                (utc_now(), token_hash(session_token)),
            )
