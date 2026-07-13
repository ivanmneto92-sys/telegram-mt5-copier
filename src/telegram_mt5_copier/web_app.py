from __future__ import annotations

from dataclasses import dataclass
import hashlib
import hmac
import json
import time
from typing import MutableSet
from urllib.parse import parse_qsl, urlencode

from .mt5.account_service import MT5AccountForm, MT5AccountService
from .mt5.models import mask_login
from .users import UserRepository


@dataclass(frozen=True)
class TelegramWebAppUser:
    id: int
    username: str | None


@dataclass(frozen=True)
class TelegramWebAppInitData:
    user: TelegramWebAppUser
    auth_date: int
    query_id: str | None
    init_hash: str


@dataclass(frozen=True)
class OnboardingResult:
    account_id: int
    masked_login: str
    connection_status: str


class WebAppValidationError(ValueError):
    pass


def validate_telegram_web_app_init_data(
    init_data: str,
    bot_token: str,
    *,
    max_age_seconds: int = 3600,
    now: int | None = None,
    replay_cache: MutableSet[str] | None = None,
) -> TelegramWebAppInitData:
    if not init_data:
        raise WebAppValidationError("initData ausente.")
    if not bot_token:
        raise WebAppValidationError("Token do bot indisponivel para validacao.")

    pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    received_hash = pairs.pop("hash", None)
    if not received_hash:
        raise WebAppValidationError("Hash do initData ausente.")

    data_check_string = "\n".join(f"{key}={value}" for key, value in sorted(pairs.items()))
    secret_key = hmac.new(b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256).digest()
    expected_hash = hmac.new(secret_key, data_check_string.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected_hash, received_hash):
        raise WebAppValidationError("initData invalido.")

    try:
        auth_date = int(pairs["auth_date"])
    except (KeyError, ValueError) as exc:
        raise WebAppValidationError("auth_date invalido.") from exc

    current_time = int(time.time()) if now is None else now
    if current_time - auth_date > max_age_seconds:
        raise WebAppValidationError("Sessao do Web App expirada.")

    replay_key = received_hash
    if replay_cache is not None:
        if replay_key in replay_cache:
            raise WebAppValidationError("initData ja utilizado.")
        replay_cache.add(replay_key)

    try:
        user_payload = json.loads(pairs["user"])
        user_id = int(user_payload["id"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise WebAppValidationError("Usuario do Web App invalido.") from exc

    return TelegramWebAppInitData(
        user=TelegramWebAppUser(id=user_id, username=user_payload.get("username")),
        auth_date=auth_date,
        query_id=pairs.get("query_id"),
        init_hash=received_hash,
    )


class CSRFTokenService:
    def __init__(self, secret: str, ttl_seconds: int = 1800) -> None:
        if not secret:
            raise ValueError("Segredo CSRF obrigatorio.")
        self.secret = secret
        self.ttl_seconds = ttl_seconds

    def issue(self, telegram_user_id: int, now: int | None = None) -> str:
        timestamp = int(time.time()) if now is None else now
        payload = f"{telegram_user_id}:{timestamp}"
        signature = hmac.new(self.secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()
        return f"{payload}:{signature}"

    def validate(self, token: str, telegram_user_id: int, now: int | None = None) -> bool:
        parts = token.split(":")
        if len(parts) != 3:
            return False
        raw_user_id, raw_timestamp, signature = parts
        if raw_user_id != str(telegram_user_id):
            return False
        try:
            timestamp = int(raw_timestamp)
        except ValueError:
            return False
        current_time = int(time.time()) if now is None else now
        if current_time - timestamp > self.ttl_seconds:
            return False
        expected = hmac.new(
            self.secret.encode("utf-8"),
            f"{raw_user_id}:{timestamp}".encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return hmac.compare_digest(expected, signature)


class SimpleRateLimiter:
    def __init__(self, limit: int = 5, window_seconds: int = 60) -> None:
        self.limit = limit
        self.window_seconds = window_seconds
        self._events: dict[int, list[float]] = {}

    def allow(self, telegram_user_id: int) -> bool:
        now = time.monotonic()
        window_start = now - self.window_seconds
        events = [event for event in self._events.get(telegram_user_id, []) if event >= window_start]
        if len(events) >= self.limit:
            self._events[telegram_user_id] = events
            return False
        events.append(now)
        self._events[telegram_user_id] = events
        return True


class MT5OnboardingService:
    def __init__(
        self,
        *,
        bot_token: str,
        users: UserRepository,
        accounts: MT5AccountService,
        csrf: CSRFTokenService,
        rate_limiter: SimpleRateLimiter | None = None,
        replay_cache: MutableSet[str] | None = None,
        require_https: bool = True,
    ) -> None:
        self.bot_token = bot_token
        self.users = users
        self.accounts = accounts
        self.csrf = csrf
        self.rate_limiter = rate_limiter or SimpleRateLimiter()
        self.replay_cache = replay_cache if replay_cache is not None else set()
        self.require_https = require_https

    def submit_account_form(
        self,
        *,
        init_data: str,
        csrf_token: str,
        request_scheme: str,
        broker_name: str,
        server_name: str,
        login: str,
        password: str,
        account_alias: str,
    ) -> OnboardingResult:
        if self.require_https and request_scheme.lower() != "https":
            raise WebAppValidationError("HTTPS obrigatorio em producao.")

        init = validate_telegram_web_app_init_data(
            init_data,
            self.bot_token,
            replay_cache=self.replay_cache,
        )
        if not self.csrf.validate(csrf_token, init.user.id):
            raise WebAppValidationError("CSRF invalido.")
        if not self.rate_limiter.allow(init.user.id):
            raise WebAppValidationError("Muitas tentativas em pouco tempo.")

        user = self.users.get_or_create_user(init.user.id, init.user.username)
        form = MT5AccountForm(
            broker_name=broker_name,
            server_name=server_name,
            login=login,
            password=password,
            account_alias=account_alias,
        )
        account = self.accounts.register_account(user.id, form)
        password = ""
        return OnboardingResult(
            account_id=account.id,
            masked_login=mask_login(account.login),
            connection_status=account.connection_status,
        )


def render_onboarding_form(csrf_token: str = "") -> str:
    return f"""<!doctype html>
<html lang="pt-BR">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Conectar MT5</title>
  <style>
    body {{ font-family: system-ui, -apple-system, Segoe UI, sans-serif; margin: 0; padding: 24px; background: #f6f7f9; color: #15171a; }}
    main {{ max-width: 520px; margin: 0 auto; }}
    label {{ display: block; margin-top: 14px; font-weight: 600; }}
    input {{ width: 100%; box-sizing: border-box; padding: 12px; border: 1px solid #c9ced6; border-radius: 8px; margin-top: 6px; }}
    button {{ margin-top: 20px; width: 100%; padding: 13px; border: 0; border-radius: 8px; background: #1473e6; color: white; font-weight: 700; }}
  </style>
</head>
<body>
  <main>
    <h1>Conectar conta MT5</h1>
    <form method="post" action="/connect" autocomplete="off">
      <input type="hidden" name="csrf_token" value="{csrf_token}">
      <input type="hidden" name="init_data" id="init_data">
      <label>Corretora<input name="broker_name" required></label>
      <label>Servidor<input name="server_name" required></label>
      <label>Login<input name="login" inputmode="numeric" required></label>
      <label>Senha<input name="password" type="password" required></label>
      <label>Apelido da conta<input name="account_alias" required></label>
      <button type="submit">Salvar conexão segura</button>
    </form>
  </main>
  <script>
    const webApp = window.Telegram && window.Telegram.WebApp;
    if (webApp) {{
      webApp.ready();
      document.getElementById("init_data").value = webApp.initData || "";
      fetch("/csrf", {{
        method: "POST",
        headers: {{ "Content-Type": "application/x-www-form-urlencoded" }},
        body: new URLSearchParams({{ init_data: webApp.initData || "" }})
      }})
        .then((response) => response.ok ? response.json() : Promise.reject())
        .then((data) => {{ document.querySelector("input[name='csrf_token']").value = data.csrf_token || ""; }})
        .catch(() => {{}});
    }}
  </script>
</body>
</html>"""


def build_signed_init_data(bot_token: str, payload: dict[str, str]) -> str:
    data_check_string = "\n".join(f"{key}={value}" for key, value in sorted(payload.items()))
    secret_key = hmac.new(b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256).digest()
    payload_hash = hmac.new(secret_key, data_check_string.encode("utf-8"), hashlib.sha256).hexdigest()
    return urlencode({**payload, "hash": payload_hash})
