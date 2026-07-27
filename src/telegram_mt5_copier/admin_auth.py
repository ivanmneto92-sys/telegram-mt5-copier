from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
from pathlib import Path
import secrets
from urllib.parse import urlsplit, urlunsplit

from .database import connect_database, initialize_database, utc_now


@dataclass(frozen=True)
class BrowserAdminSession:
    admin_telegram_user_id: int
    session_token: str
    expires_at: str


class AdminBrowserAuthService:
    def __init__(
        self,
        database_path: Path,
        *,
        admin_ids: tuple[int, ...],
        login_ttl_minutes: int = 5,
        session_ttl_hours: int = 12,
    ) -> None:
        self.database_path = database_path
        self.admin_ids = frozenset(admin_ids)
        self.login_ttl_minutes = login_ttl_minutes
        self.session_ttl_hours = session_ttl_hours
        initialize_database(database_path)

    def create_login_url(self, admin_telegram_user_id: int, admin_url: str) -> str:
        self._require_admin(admin_telegram_user_id)
        parsed = urlsplit(admin_url)
        if parsed.scheme != "https" or not parsed.netloc:
            raise ValueError("URL HTTPS do painel administrativo não configurada.")
        raw_token = secrets.token_urlsafe(32)
        now = datetime.now(tz=timezone.utc)
        expires_at = now + timedelta(minutes=self.login_ttl_minutes)
        with connect_database(self.database_path) as connection:
            connection.execute(
                """
                INSERT INTO admin_login_tokens (
                    token_hash, admin_telegram_user_id, expires_at, used_at, created_at
                )
                VALUES (?, ?, ?, NULL, ?)
                """,
                (
                    token_hash(raw_token),
                    admin_telegram_user_id,
                    expires_at.isoformat(),
                    now.isoformat(),
                ),
            ).close()
            connection.execute(
                """
                DELETE FROM admin_login_tokens
                WHERE expires_at < ? OR used_at IS NOT NULL
                """,
                ((now - timedelta(days=1)).isoformat(),),
            ).close()
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, parsed.query, f"token={raw_token}"))

    def consume_login_token(self, raw_token: str) -> BrowserAdminSession:
        if not raw_token or len(raw_token) > 256:
            raise ValueError("Link de acesso inválido.")
        now = datetime.now(tz=timezone.utc)
        with connect_database(self.database_path) as connection:
            row = connection.execute(
                """
                SELECT id, admin_telegram_user_id, expires_at, used_at
                FROM admin_login_tokens
                WHERE token_hash = ?
                """,
                (token_hash(raw_token),),
            ).fetchone()
            if (
                row is None
                or row[3] is not None
                or datetime.fromisoformat(str(row[2])) <= now
            ):
                raise ValueError("Link de acesso inválido ou expirado.")
            admin_id = int(row[1])
            self._require_admin(admin_id)
            session_token = secrets.token_urlsafe(48)
            expires_at = now + timedelta(hours=self.session_ttl_hours)
            update = connection.execute(
                "UPDATE admin_login_tokens SET used_at = ? WHERE id = ? AND used_at IS NULL",
                (now.isoformat(), int(row[0])),
            )
            consumed = update.rowcount
            update.close()
            if consumed != 1:
                raise ValueError("Link de acesso inválido ou expirado.")
            connection.execute(
                """
                INSERT INTO admin_browser_sessions (
                    session_hash, admin_telegram_user_id, expires_at,
                    revoked_at, last_seen_at, created_at
                )
                VALUES (?, ?, ?, NULL, ?, ?)
                """,
                (
                    token_hash(session_token),
                    admin_id,
                    expires_at.isoformat(),
                    now.isoformat(),
                    now.isoformat(),
                ),
            ).close()
        return BrowserAdminSession(
            admin_telegram_user_id=admin_id,
            session_token=session_token,
            expires_at=expires_at.isoformat(),
        )

    def authenticate_session(self, session_token: str) -> int:
        if not session_token or len(session_token) > 256:
            raise ValueError("Sessão administrativa inválida.")
        now = datetime.now(tz=timezone.utc)
        with connect_database(self.database_path) as connection:
            row = connection.execute(
                """
                SELECT id, admin_telegram_user_id, expires_at
                FROM admin_browser_sessions
                WHERE session_hash = ? AND revoked_at IS NULL
                """,
                (token_hash(session_token),),
            ).fetchone()
            if row is None or datetime.fromisoformat(str(row[2])) <= now:
                raise ValueError("Sessão administrativa expirada.")
            admin_id = int(row[1])
            self._require_admin(admin_id)
            connection.execute(
                "UPDATE admin_browser_sessions SET last_seen_at = ? WHERE id = ?",
                (now.isoformat(), int(row[0])),
            ).close()
        return admin_id

    def revoke_session(self, session_token: str) -> None:
        if not session_token:
            return
        with connect_database(self.database_path) as connection:
            connection.execute(
                """
                UPDATE admin_browser_sessions SET revoked_at = ?
                WHERE session_hash = ? AND revoked_at IS NULL
                """,
                (utc_now(), token_hash(session_token)),
            ).close()

    def _require_admin(self, telegram_user_id: int) -> None:
        if telegram_user_id not in self.admin_ids:
            raise ValueError("Administrador não autorizado.")


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
