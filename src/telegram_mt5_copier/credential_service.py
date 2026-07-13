from __future__ import annotations

from dataclasses import dataclass, field

from cryptography.fernet import Fernet, InvalidToken

PASSWORD_REDACTION = "***REDACTED***"
TOKEN_PREFIX = "v1:"


@dataclass(repr=False)
class CredentialService:
    primary_key: str = field(repr=False)
    previous_keys: tuple[str, ...] = field(default=(), repr=False)

    def __post_init__(self) -> None:
        self._fernets = tuple(Fernet(key.encode("utf-8")) for key in (self.primary_key, *self.previous_keys))
        if not self._fernets:
            raise ValueError("MT5_CREDENTIAL_KEY nao configurada.")

    def __repr__(self) -> str:
        return "CredentialService(primary_key=***REDACTED***)"

    @staticmethod
    def generate_key() -> str:
        return Fernet.generate_key().decode("utf-8")

    def encrypt_password(self, password: str) -> str:
        if not password:
            raise ValueError("Senha MT5 obrigatoria.")
        token = self._fernets[0].encrypt(password.encode("utf-8")).decode("utf-8")
        return f"{TOKEN_PREFIX}{token}"

    def decrypt_password(self, encrypted_password: str) -> str:
        token = encrypted_password
        if encrypted_password.startswith(TOKEN_PREFIX):
            token = encrypted_password[len(TOKEN_PREFIX) :]

        last_error: InvalidToken | None = None
        for fernet in self._fernets:
            try:
                return fernet.decrypt(token.encode("utf-8")).decode("utf-8")
            except InvalidToken as exc:
                last_error = exc

        raise ValueError("Credencial MT5 nao pode ser descriptografada.") from last_error


def redact_sensitive(value: object) -> str:
    text = str(value)
    if not text:
        return ""
    return PASSWORD_REDACTION
