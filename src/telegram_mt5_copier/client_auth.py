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

from .admin_auth import token_hash
from .database import connect_database, initialize_database, utc_now


PASSWORD_LOCK_ATTEMPTS = 5
PASSWORD_LOCK_MINUTES = 15
PASSWORD_SCRYPT_N = 2**14
PASSWORD_SCRYPT_R = 8
PASSWORD_SCRYPT_P = 1
EMAIL_PATTERN = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


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

    def register(
        self,
        *,
        customer_name: str,
        email: str,
        phone: str,
        password: str,
    ) -> BrowserClientSession:
        clean_name = validate_customer_name(customer_name)
        clean_email = normalize_email(email)
        clean_phone = validate_phone(phone)
        password_hash = hash_password(validate_password(password))
        now = datetime.now(tz=timezone.utc)

        try:
            with connect_database(self.database_path) as connection:
                connection.execute("BEGIN IMMEDIATE")
                existing = connection.execute(
                    """
                    SELECT 1 FROM client_credentials WHERE email = ? COLLATE NOCASE
                    UNION ALL
                    SELECT 1 FROM customer_billing
                    WHERE email = ? COLLATE NOCASE
                    LIMIT 1
                    """,
                    (clean_email, clean_email),
                ).fetchone()
                if existing is not None:
                    raise ValueError(
                        "Este e-mail já possui cadastro. Entre com sua senha ou use o acesso pelo Telegram."
                    )

                minimum = connection.execute(
                    "SELECT MIN(telegram_user_id) FROM users"
                ).fetchone()
                current_minimum = int(minimum[0]) if minimum and minimum[0] is not None else 0
                synthetic_telegram_id = min(-1, current_minimum - 1)
                now_text = now.isoformat()
                cursor = connection.execute(
                    """
                    INSERT INTO users (
                        telegram_user_id, telegram_username, status,
                        created_at, updated_at
                    ) VALUES (?, NULL, 'paused', ?, ?)
                    """,
                    (synthetic_telegram_id, now_text, now_text),
                )
                user_id = int(cursor.lastrowid)
                cursor.close()
                connection.execute(
                    """
                    INSERT INTO customer_billing (
                        user_id, customer_name, email, phone, plan_name,
                        monthly_amount, due_date, billing_status, last_paid_at,
                        notes, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, 'Mensal', '0', NULL, 'pending', NULL,
                              'Cadastro realizado pelo portal web.', ?, ?)
                    """,
                    (user_id, clean_name, clean_email, clean_phone, now_text, now_text),
                )
                connection.execute(
                    """
                    INSERT INTO client_credentials (
                        user_id, email, password_hash, failed_attempts,
                        locked_until, password_changed_at, created_at, updated_at
                    ) VALUES (?, ?, ?, 0, NULL, ?, ?, ?)
                    """,
                    (user_id, clean_email, password_hash, now_text, now_text, now_text),
                )
                return self._create_session(connection, user_id, now)
        except sqlite3.IntegrityError as exc:
            raise ValueError("Não foi possível concluir o cadastro com estes dados.") from exc

    def login(self, *, email: str, password: str) -> BrowserClientSession:
        clean_email = normalize_email(email)
        candidate = password[:129]
        now = datetime.now(tz=timezone.utc)
        rejection: str | None = None
        with connect_database(self.database_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT user_id, password_hash, failed_attempts, locked_until
                FROM client_credentials
                WHERE email = ? COLLATE NOCASE
                """,
                (clean_email,),
            ).fetchone()
            stored_hash = str(row[1]) if row is not None else DUMMY_PASSWORD_HASH
            password_matches = verify_password(candidate, stored_hash)
            if row is None or not password_matches:
                if row is not None:
                    failed_attempts = int(row[2]) + 1
                    locked_until = None
                    if failed_attempts >= PASSWORD_LOCK_ATTEMPTS:
                        locked_until = (
                            now + timedelta(minutes=PASSWORD_LOCK_MINUTES)
                        ).isoformat()
                    connection.execute(
                        """
                        UPDATE client_credentials
                        SET failed_attempts = ?, locked_until = ?, updated_at = ?
                        WHERE user_id = ?
                        """,
                        (failed_attempts, locked_until, now.isoformat(), int(row[0])),
                    )
                rejection = "E-mail ou senha inválidos."
            else:
                locked_until = datetime.fromisoformat(str(row[3])) if row[3] else None
                if locked_until is not None and locked_until > now:
                    rejection = "Muitas tentativas. Aguarde 15 minutos e tente novamente."
                else:
                    user_id = int(row[0])
                    connection.execute(
                        """
                        UPDATE client_credentials
                        SET failed_attempts = 0, locked_until = NULL, updated_at = ?
                        WHERE user_id = ?
                        """,
                        (now.isoformat(), user_id),
                    )
                    return self._create_session(connection, user_id, now)
        raise ValueError(rejection or "E-mail ou senha inválidos.")

    def set_password_for_user(self, user_id: int, *, email: str, password: str) -> None:
        clean_email = normalize_email(email)
        password_hash = hash_password(validate_password(password))
        now = utc_now()
        try:
            with connect_database(self.database_path) as connection:
                connection.execute("BEGIN IMMEDIATE")
                if connection.execute(
                    "SELECT 1 FROM users WHERE id = ?", (user_id,)
                ).fetchone() is None:
                    raise ValueError("Cliente não encontrado.")
                owner = connection.execute(
                    "SELECT user_id FROM client_credentials WHERE email = ? COLLATE NOCASE",
                    (clean_email,),
                ).fetchone()
                if owner is not None and int(owner[0]) != user_id:
                    raise ValueError("Este e-mail já está em uso.")
                connection.execute(
                    """
                    INSERT INTO client_credentials (
                        user_id, email, password_hash, failed_attempts,
                        locked_until, password_changed_at, created_at, updated_at
                    ) VALUES (?, ?, ?, 0, NULL, ?, ?, ?)
                    ON CONFLICT(user_id) DO UPDATE SET
                        email = excluded.email,
                        password_hash = excluded.password_hash,
                        failed_attempts = 0,
                        locked_until = NULL,
                        password_changed_at = excluded.password_changed_at,
                        updated_at = excluded.updated_at
                    """,
                    (user_id, clean_email, password_hash, now, now, now),
                )
                connection.execute(
                    """
                    UPDATE customer_billing SET email = ?, updated_at = ?
                    WHERE user_id = ?
                    """,
                    (clean_email, now, user_id),
                )
        except sqlite3.IntegrityError as exc:
            raise ValueError("Este e-mail já está em uso.") from exc

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
            session = self._create_session(connection, int(row[1]), now)
        return session

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

    def _create_session(
        self,
        connection: sqlite3.Connection,
        user_id: int,
        now: datetime,
    ) -> BrowserClientSession:
        session_token = secrets.token_urlsafe(48)
        expires_at = now + timedelta(hours=self.session_ttl_hours)
        connection.execute(
            """
            INSERT INTO client_browser_sessions (
                session_hash, user_id, expires_at, revoked_at, last_seen_at, created_at
            ) VALUES (?, ?, ?, NULL, ?, ?)
            """,
            (
                token_hash(session_token), user_id, expires_at.isoformat(),
                now.isoformat(), now.isoformat(),
            ),
        )
        return BrowserClientSession(user_id, session_token, expires_at.isoformat())


def normalize_email(value: str) -> str:
    email = value.strip().casefold()
    if len(email) > 254 or not EMAIL_PATTERN.fullmatch(email):
        raise ValueError("Informe um e-mail válido.")
    return email


def validate_customer_name(value: str) -> str:
    name = " ".join(value.strip().split())
    if len(name) < 3 or len(name) > 120:
        raise ValueError("Informe seu nome completo.")
    return name


def validate_phone(value: str) -> str:
    phone = " ".join(value.strip().split())
    digits = "".join(character for character in phone if character.isdigit())
    if len(phone) > 40 or len(digits) < 8:
        raise ValueError("Informe um telefone válido.")
    return phone


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
