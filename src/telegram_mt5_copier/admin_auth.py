from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
from pathlib import Path
import re
import secrets
import sqlite3
from urllib.parse import urlsplit, urlunsplit

from .database import connect_database, initialize_database, utc_now


# Primitivos de e-mail/senha compartilhados por admin e cliente. Moram aqui
# (nao em client_auth.py) porque client_auth.py ja importa token_hash deste
# modulo — colocar as duas coisas no mesmo lugar evita import circular.
PASSWORD_LOCK_ATTEMPTS = 5
PASSWORD_LOCK_MINUTES = 15
PASSWORD_SCRYPT_N = 2**14
PASSWORD_SCRYPT_R = 8
PASSWORD_SCRYPT_P = 1
EMAIL_PATTERN = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


def normalize_email(value: str) -> str:
    email = value.strip().casefold()
    if len(email) > 254 or not EMAIL_PATTERN.fullmatch(email):
        raise ValueError("Informe um e-mail válido.")
    return email


def validate_password(value: str) -> str:
    if len(value) < 8 or len(value) > 128:
        raise ValueError("A senha deve ter entre 8 e 128 caracteres.")
    if not any(character.isalpha() for character in value) or not any(
        character.isdigit() for character in value
    ):
        raise ValueError("A senha deve conter letras e números.")
    return value


def hash_password(value: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        value.encode("utf-8"),
        salt=salt,
        n=PASSWORD_SCRYPT_N,
        r=PASSWORD_SCRYPT_R,
        p=PASSWORD_SCRYPT_P,
        dklen=32,
    )
    return "$".join(
        (
            "scrypt",
            str(PASSWORD_SCRYPT_N),
            str(PASSWORD_SCRYPT_R),
            str(PASSWORD_SCRYPT_P),
            base64.urlsafe_b64encode(salt).decode("ascii"),
            base64.urlsafe_b64encode(digest).decode("ascii"),
        )
    )


def verify_password(value: str, encoded: str) -> bool:
    try:
        scheme, raw_n, raw_r, raw_p, raw_salt, raw_digest = encoded.split("$", 5)
        if scheme != "scrypt":
            return False
        salt = base64.urlsafe_b64decode(raw_salt.encode("ascii"))
        expected = base64.urlsafe_b64decode(raw_digest.encode("ascii"))
        actual = hashlib.scrypt(
            value.encode("utf-8"),
            salt=salt,
            n=int(raw_n),
            r=int(raw_r),
            p=int(raw_p),
            dklen=len(expected),
        )
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


DUMMY_PASSWORD_HASH = hash_password("senha-inexistente-123")


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

    def set_password_for_admin(
        self, admin_telegram_user_id: int, *, email: str, password: str
    ) -> None:
        """Configura login por e-mail/senha para um admin ja autorizado.

        So pode ser chamado por quem ja provou ser admin de outra forma (uma
        sessao de navegador valida, aberta a partir do link do bot) — nao existe
        auto-cadastro de administrador. `admin_telegram_user_id` precisa estar
        em BOT_ADMIN_IDS; senao a config e quem manda, nao esta tabela.
        """
        self._require_admin(admin_telegram_user_id)
        clean_email = normalize_email(email)
        password_hash = hash_password(validate_password(password))
        now = utc_now()
        try:
            with connect_database(self.database_path) as connection:
                connection.execute("BEGIN IMMEDIATE")
                owner = connection.execute(
                    "SELECT admin_telegram_user_id FROM admin_credentials WHERE email = ? COLLATE NOCASE",
                    (clean_email,),
                ).fetchone()
                if owner is not None and int(owner[0]) != admin_telegram_user_id:
                    raise ValueError("Este e-mail já está em uso.")
                connection.execute(
                    """
                    INSERT INTO admin_credentials (
                        admin_telegram_user_id, email, password_hash, failed_attempts,
                        locked_until, password_changed_at, created_at, updated_at
                    ) VALUES (?, ?, ?, 0, NULL, ?, ?, ?)
                    ON CONFLICT(admin_telegram_user_id) DO UPDATE SET
                        email = excluded.email,
                        password_hash = excluded.password_hash,
                        failed_attempts = 0,
                        locked_until = NULL,
                        password_changed_at = excluded.password_changed_at,
                        updated_at = excluded.updated_at
                    """,
                    (admin_telegram_user_id, clean_email, password_hash, now, now, now),
                ).close()
        except sqlite3.IntegrityError as exc:
            raise ValueError("Este e-mail já está em uso.") from exc

    def login(self, *, email: str, password: str) -> BrowserAdminSession:
        clean_email = normalize_email(email)
        candidate = password[:129]
        now = datetime.now(tz=timezone.utc)
        rejection: str | None = None
        with connect_database(self.database_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT admin_telegram_user_id, password_hash, failed_attempts, locked_until
                FROM admin_credentials WHERE email = ? COLLATE NOCASE
                """,
                (clean_email,),
            ).fetchone()
            stored_hash = str(row[1]) if row is not None else DUMMY_PASSWORD_HASH
            password_matches = verify_password(candidate, stored_hash)
            admin_id = int(row[0]) if row is not None else None
            authorized = admin_id is not None and admin_id in self.admin_ids
            if row is None or not password_matches or not authorized:
                if row is not None:
                    failed_attempts = int(row[2]) + 1
                    locked_until = None
                    if failed_attempts >= PASSWORD_LOCK_ATTEMPTS:
                        locked_until = (
                            now + timedelta(minutes=PASSWORD_LOCK_MINUTES)
                        ).isoformat()
                    connection.execute(
                        """
                        UPDATE admin_credentials
                        SET failed_attempts = ?, locked_until = ?, updated_at = ?
                        WHERE admin_telegram_user_id = ?
                        """,
                        (failed_attempts, locked_until, now.isoformat(), admin_id),
                    ).close()
                rejection = "E-mail ou senha inválidos."
            else:
                locked_until = datetime.fromisoformat(str(row[3])) if row[3] else None
                if locked_until is not None and locked_until > now:
                    rejection = "Muitas tentativas. Aguarde 15 minutos e tente novamente."
                else:
                    connection.execute(
                        """
                        UPDATE admin_credentials
                        SET failed_attempts = 0, locked_until = NULL, updated_at = ?
                        WHERE admin_telegram_user_id = ?
                        """,
                        (now.isoformat(), admin_id),
                    ).close()
                    session_token = secrets.token_urlsafe(48)
                    expires_at = now + timedelta(hours=self.session_ttl_hours)
                    connection.execute(
                        """
                        INSERT INTO admin_browser_sessions (
                            session_hash, admin_telegram_user_id, expires_at,
                            revoked_at, last_seen_at, created_at
                        ) VALUES (?, ?, ?, NULL, ?, ?)
                        """,
                        (
                            token_hash(session_token), admin_id, expires_at.isoformat(),
                            now.isoformat(), now.isoformat(),
                        ),
                    ).close()
                    return BrowserAdminSession(admin_id, session_token, expires_at.isoformat())
        raise ValueError(rejection or "E-mail ou senha inválidos.")

    def _require_admin(self, telegram_user_id: int) -> None:
        if telegram_user_id not in self.admin_ids:
            raise ValueError("Administrador não autorizado.")


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
