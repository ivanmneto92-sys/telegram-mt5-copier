from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
import secrets
import sqlite3
from urllib.parse import urlsplit, urlunsplit

# Primitivos de e-mail/senha (hash_password, validate_password, normalize_email,
# etc.) moram em admin_auth.py e sao só reaproveitados aqui — ver o comentário
# lá para o motivo (evitar import circular com token_hash).
from .admin_auth import (
    DUMMY_PASSWORD_HASH,
    PASSWORD_LOCK_ATTEMPTS,
    PASSWORD_LOCK_MINUTES,
    hash_password,
    normalize_email,
    token_hash,
    validate_password,
    verify_password,
)
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
        password_reset_ttl_minutes: int = 30,
        email_confirmation_ttl_hours: int = 48,
    ) -> None:
        self.database_path = database_path
        self.login_ttl_minutes = login_ttl_minutes
        self.session_ttl_hours = session_ttl_hours
        self.password_reset_ttl_minutes = password_reset_ttl_minutes
        self.email_confirmation_ttl_hours = email_confirmation_ttl_hours
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

    def request_password_reset(self, email: str, app_url: str) -> str | None:
        """Gera o link de redefinicao se o e-mail existir; None caso contrario.

        Nunca informe ao cliente HTTP se o e-mail existe ou nao (evita que
        alguem descubra quais e-mails estao cadastrados) — a camada HTTP deve
        responder {"ok": true} de qualquer forma, com ou sem retorno aqui.
        """
        parsed = urlsplit(app_url)
        if parsed.scheme != "https" or not parsed.netloc:
            raise ValueError("URL HTTPS do aplicativo do cliente nao configurada.")
        clean_email = normalize_email(email)
        now = datetime.now(tz=timezone.utc)
        with connect_database(self.database_path) as connection:
            row = connection.execute(
                "SELECT user_id FROM client_credentials WHERE email = ? COLLATE NOCASE",
                (clean_email,),
            ).fetchone()
            if row is None:
                return None
            user_id = int(row[0])
            raw_token = secrets.token_urlsafe(32)
            expires_at = now + timedelta(minutes=self.password_reset_ttl_minutes)
            connection.execute(
                """
                INSERT INTO client_password_reset_tokens (
                    token_hash, user_id, expires_at, used_at, created_at
                ) VALUES (?, ?, ?, NULL, ?)
                """,
                (token_hash(raw_token), user_id, expires_at.isoformat(), now.isoformat()),
            )
            connection.execute(
                "DELETE FROM client_password_reset_tokens WHERE expires_at < ? OR used_at IS NOT NULL",
                ((now - timedelta(days=1)).isoformat(),),
            )
        return urlunsplit(
            (parsed.scheme, parsed.netloc, parsed.path or "/", parsed.query, f"reset_token={raw_token}")
        )

    def reset_password(self, raw_token: str, new_password: str) -> int:
        """Redefine a senha e devolve o user_id afetado.

        O retorno permite que a camada HTTP envie o aviso de seguranca
        ("sua senha foi alterada") sem precisar decodificar o token de novo.
        """
        if not raw_token or len(raw_token) > 256:
            raise ValueError("Link de redefinição inválido ou expirado.")
        password_hash = hash_password(validate_password(new_password))
        now = datetime.now(tz=timezone.utc)
        with connect_database(self.database_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT id, user_id, expires_at, used_at
                FROM client_password_reset_tokens WHERE token_hash = ?
                """,
                (token_hash(raw_token),),
            ).fetchone()
            if row is None or row[3] is not None or datetime.fromisoformat(str(row[2])) <= now:
                raise ValueError("Link de redefinição inválido ou expirado.")
            update = connection.execute(
                "UPDATE client_password_reset_tokens SET used_at = ? WHERE id = ? AND used_at IS NULL",
                (now.isoformat(), int(row[0])),
            )
            if update.rowcount != 1:
                raise ValueError("Link de redefinição inválido ou expirado.")
            user_id = int(row[1])
            connection.execute(
                """
                UPDATE client_credentials
                SET password_hash = ?, failed_attempts = 0, locked_until = NULL,
                    password_changed_at = ?, updated_at = ?
                WHERE user_id = ?
                """,
                (password_hash, now.isoformat(), now.isoformat(), user_id),
            )
            # Redefinir a senha revoga sessoes existentes: se alguem indevido
            # tinha acesso, perde a sessao no momento em que o dono retoma a conta.
            connection.execute(
                """
                UPDATE client_browser_sessions SET revoked_at = ?
                WHERE user_id = ? AND revoked_at IS NULL
                """,
                (now.isoformat(), user_id),
            )
        return user_id

    def request_email_confirmation(self, user_id: int, app_url: str) -> str:
        parsed = urlsplit(app_url)
        if parsed.scheme != "https" or not parsed.netloc:
            raise ValueError("URL HTTPS do aplicativo do cliente nao configurada.")
        now = datetime.now(tz=timezone.utc)
        with connect_database(self.database_path) as connection:
            row = connection.execute(
                "SELECT email FROM client_credentials WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            if row is None:
                raise ValueError("Cliente não encontrado.")
            current_email = str(row[0])
            raw_token = secrets.token_urlsafe(32)
            expires_at = now + timedelta(hours=self.email_confirmation_ttl_hours)
            connection.execute(
                """
                INSERT INTO client_email_confirmation_tokens (
                    token_hash, user_id, email, expires_at, used_at, created_at
                ) VALUES (?, ?, ?, ?, NULL, ?)
                """,
                (token_hash(raw_token), user_id, current_email, expires_at.isoformat(), now.isoformat()),
            )
            connection.execute(
                "DELETE FROM client_email_confirmation_tokens WHERE expires_at < ? OR used_at IS NOT NULL",
                ((now - timedelta(days=1)).isoformat(),),
            )
        return urlunsplit(
            (parsed.scheme, parsed.netloc, parsed.path or "/", parsed.query, f"confirm_token={raw_token}")
        )

    def confirm_email(self, raw_token: str) -> None:
        if not raw_token or len(raw_token) > 256:
            raise ValueError("Link de confirmação inválido ou expirado.")
        now = datetime.now(tz=timezone.utc)
        with connect_database(self.database_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT id, user_id, email, expires_at, used_at
                FROM client_email_confirmation_tokens WHERE token_hash = ?
                """,
                (token_hash(raw_token),),
            ).fetchone()
            if row is None or row[4] is not None or datetime.fromisoformat(str(row[3])) <= now:
                raise ValueError("Link de confirmação inválido ou expirado.")
            update = connection.execute(
                "UPDATE client_email_confirmation_tokens SET used_at = ? WHERE id = ? AND used_at IS NULL",
                (now.isoformat(), int(row[0])),
            )
            if update.rowcount != 1:
                raise ValueError("Link de confirmação inválido ou expirado.")
            user_id = int(row[1])
            token_email = str(row[2])
            # So confirma se o e-mail continuar sendo o mesmo de quando o link
            # foi emitido — se o cliente trocou de e-mail depois, este link
            # antigo nao pode confirmar o e-mail novo.
            connection.execute(
                """
                UPDATE client_credentials SET email_confirmed_at = ?, updated_at = ?
                WHERE user_id = ? AND email = ? COLLATE NOCASE
                """,
                (now.isoformat(), now.isoformat(), user_id, token_email),
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
