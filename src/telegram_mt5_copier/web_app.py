from __future__ import annotations

from dataclasses import dataclass
import hashlib
import hmac
from html import escape
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
    if auth_date > current_time + 60:
        raise WebAppValidationError("auth_date invalido.")
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


OUTSIDE_TELEGRAM_MESSAGE = "Abra esta página pelo botão Conectar conta MT5 dentro do bot."
EMPTY_INIT_DATA_MESSAGE = "Não foi possível identificar sua sessão. Feche esta tela e abra novamente pelo bot."
VALIDATION_FAILED_MESSAGE = "Não foi possível validar sua sessão do Telegram."
FRIENDLY_OPEN_ERROR_MESSAGE = "Não foi possível abrir a conexão segura. Feche esta tela e tente novamente pelo bot."


def render_onboarding_form(script_nonce: str = "", csrf_token: str = "") -> str:
    nonce_attribute = f' nonce="{escape(script_nonce, quote=True)}"' if script_nonce else ""
    return f"""<!doctype html>
<html lang="pt-BR">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Conectar MT5</title>
  <style>
    :root {{ color-scheme: light; }}
    body {{ font-family: -apple-system, BlinkMacSystemFont, Segoe UI, sans-serif; margin: 0; padding: 24px; background: #f6f7f9; color: #15171a; }}
    main {{ max-width: 520px; margin: 0 auto; }}
    h1 {{ font-size: 24px; margin: 0 0 10px; }}
    p {{ line-height: 1.45; }}
    .panel {{ background: #ffffff; border: 1px solid #dde2ea; border-radius: 8px; padding: 18px; }}
    .message {{ display: block; margin: 14px 0; padding: 12px; border-radius: 8px; border: 1px solid #d6dae2; background: #ffffff; }}
    .message.error {{ border-color: #f0b8b8; background: #fff5f5; color: #8a1f1f; }}
    .message.ok {{ border-color: #b9dfc2; background: #f3fff5; color: #185c28; }}
    .message.notice {{ border-color: #b9cce8; background: #f4f8ff; color: #17406f; }}
    form {{ display: block; }}
    label {{ display: block; margin-top: 14px; font-weight: 600; }}
    input {{ width: 100%; box-sizing: border-box; padding: 12px; border: 1px solid #c9ced6; border-radius: 8px; margin-top: 6px; }}
    button {{ margin-top: 20px; width: 100%; padding: 13px; border: 0; border-radius: 8px; background: #1473e6; color: white; font-weight: 700; }}
    button:disabled {{ opacity: .62; }}
  </style>
  <script{nonce_attribute}>
    (function () {{
      window.__telegramMt5InlineNonceOk = true;
      function postTelegramEvent(eventType) {{
        try {{
          if (window.TelegramWebviewProxy && typeof window.TelegramWebviewProxy.postEvent === "function") {{
            window.TelegramWebviewProxy.postEvent(eventType, "{{}}");
            return;
          }}
          if (window.external && typeof window.external.notify === "function") {{
            window.external.notify(JSON.stringify({{ eventType: eventType, eventData: {{}} }}));
          }}
        }} catch (_eventError) {{}}
      }}
      postTelegramEvent("web_app_ready");
    }}());
  </script>
  <script defer src="https://telegram.org/js/telegram-web-app.js"></script>
  <script defer src="/miniapp.js"></script>
</head>
<body>
  <main>
    <section class="panel">
      <h1>Conectar conta MT5</h1>
      <p id="intro">Preencha os dados da sua conta de demonstração pelo ambiente seguro do Telegram.</p>
      <div id="message" class="message error" role="alert">{OUTSIDE_TELEGRAM_MESSAGE}</div>
    <form id="connect-form" method="post" action="/api/connect" autocomplete="off">
      <input type="hidden" name="csrf_token" value="{csrf_token}">
      <input type="hidden" name="init_data" id="init_data">
      <label>Corretora<input name="broker_name" required></label>
      <label>Servidor<input name="server_name" required></label>
      <label>Login<input name="login" inputmode="numeric" required></label>
      <label>Senha<input name="password" type="password" required></label>
      <label>Apelido da conta<input name="account_alias" required></label>
      <button type="submit">Salvar conexão segura</button>
    </form>
    <noscript>
      <div class="message error" style="display:block">Abra esta página pelo botão Conectar conta MT5 dentro do bot.</div>
    </noscript>
    </section>
  </main>
</body>
</html>"""


def render_miniapp_script() -> str:
    messages = json.dumps(
        {
            "outsideTelegram": OUTSIDE_TELEGRAM_MESSAGE,
            "emptyInitData": EMPTY_INIT_DATA_MESSAGE,
            "validationFailed": VALIDATION_FAILED_MESSAGE,
            "openError": FRIENDLY_OPEN_ERROR_MESSAGE,
            "validating": "Validando sessão segura do Telegram.",
            "success": "Conta MT5 cadastrada com segurança.",
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return """
(function () {
  var messages = __MESSAGES__;
  var tg = window.Telegram && window.Telegram.WebApp
    ? window.Telegram.WebApp
    : null;

  function postTelegramEvent(eventType) {
    try {
      if (window.TelegramWebviewProxy && typeof window.TelegramWebviewProxy.postEvent === "function") {
        window.TelegramWebviewProxy.postEvent(eventType, "{}");
        return;
      }
      if (window.external && typeof window.external.notify === "function") {
        window.external.notify(JSON.stringify({ eventType: eventType, eventData: {} }));
        return;
      }
      if (window.parent && window.parent !== window) {
        window.parent.postMessage(JSON.stringify({ eventType: eventType, eventData: {} }), "*");
      }
    } catch (_eventError) {}
  }

  function notifyTelegramReady() {
    try {
      if (tg && typeof tg.ready === "function") {
        tg.ready();
      } else {
        postTelegramEvent("web_app_ready");
      }
    } catch (_readyError) {
      postTelegramEvent("web_app_ready");
    }
  }

  function notifyTelegramExpanded() {
    try {
      if (tg && typeof tg.expand === "function") {
        tg.expand();
      } else {
        postTelegramEvent("web_app_expand");
      }
    } catch (_expandError) {
      postTelegramEvent("web_app_expand");
    }
  }

  notifyTelegramReady();
  notifyTelegramExpanded();

  function onReady(callback) {
    if (document.readyState === "loading") {
      document.addEventListener("DOMContentLoaded", callback);
      return;
    }
    callback();
  }

  function encodeForm(fields) {
    var pairs = [];
    for (var key in fields) {
      if (Object.prototype.hasOwnProperty.call(fields, key)) {
        pairs.push(encodeURIComponent(key) + "=" + encodeURIComponent(fields[key] || ""));
      }
    }
    return pairs.join("&");
  }

  function postApi(path, fields) {
    return fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: encodeForm(fields)
    });
  }

  onReady(function () {
    var form = document.getElementById("connect-form");
    var message = document.getElementById("message");
    var initDataInput = document.getElementById("init_data");
    var csrfInput = document.querySelector("input[name='csrf_token']");
    var passwordInput = document.querySelector("input[name='password']");
    var submitButton = form ? form.querySelector("button") : null;

    function showMessage(text, kind) {
      if (!message) {
        return;
      }
      message.className = "message " + (kind || "error");
      message.textContent = text;
      message.style.display = "block";
    }

    function hideMessage() {
      if (!message) {
        return;
      }
      message.textContent = "";
      message.style.display = "none";
    }

    function showForm() {
      if (form) {
        form.style.display = "block";
      }
    }

    function setSubmitEnabled(enabled) {
      if (submitButton) {
        submitButton.disabled = !enabled;
      }
    }

    function logFrontend(eventName, fields) {
      if (!window.fetch) {
        return;
      }
      try {
        var payload = fields || {};
        payload.event = eventName;
        postApi("/api/log", payload).catch(function () {});
      } catch (_logError) {}
    }

    function hasInitData() {
      return initDataInput && initDataInput.value ? "true" : "false";
    }

    window.addEventListener("error", function () {
      notifyTelegramReady();
      showForm();
      setSubmitEnabled(false);
      showMessage(messages.openError, "error");
      logFrontend("frontend_error", { has_init_data: hasInitData() });
    });

    window.addEventListener("unhandledrejection", function () {
      notifyTelegramReady();
      showForm();
      setSubmitEnabled(false);
      showMessage(messages.openError, "error");
      logFrontend("frontend_unhandledrejection", { has_init_data: hasInitData() });
    });

    showForm();
    setSubmitEnabled(false);
    notifyTelegramReady();

    if (!window.fetch) {
      showMessage(messages.openError, "error");
      return;
    }

    if (!tg) {
      showMessage(messages.outsideTelegram, "error");
      logFrontend("telegram_webapp_missing", { has_init_data: "false" });
      return;
    }

    var initData = tg.initData || "";
    if (initDataInput) {
      initDataInput.value = initData;
    }
    logFrontend("telegram_webapp_detected", { has_init_data: initData ? "true" : "false" });

    if (!initData) {
      showMessage(messages.emptyInitData, "error");
      return;
    }

    showMessage(messages.validating, "notice");
    postApi("/api/csrf", { init_data: initData })
      .then(function (response) {
        if (!response.ok) {
          throw new Error("csrf rejected");
        }
        return response.json();
      })
      .then(function (data) {
        if (!data || !data.csrf_token) {
          throw new Error("csrf missing");
        }
        if (csrfInput) {
          csrfInput.value = data.csrf_token;
        }
        hideMessage();
        setSubmitEnabled(true);
      })
      .catch(function () {
        setSubmitEnabled(false);
        showMessage(messages.validationFailed, "error");
        logFrontend("api_error", { endpoint: "csrf", has_init_data: "true" });
      });

    if (!form) {
      return;
    }

    form.addEventListener("submit", function (event) {
      event.preventDefault();
      setSubmitEnabled(false);
      var fields = {
        csrf_token: csrfInput ? csrfInput.value : "",
        init_data: initDataInput ? initDataInput.value : "",
        broker_name: document.querySelector("input[name='broker_name']").value,
        server_name: document.querySelector("input[name='server_name']").value,
        login: document.querySelector("input[name='login']").value,
        password: passwordInput ? passwordInput.value : "",
        account_alias: document.querySelector("input[name='account_alias']").value
      };
      postApi("/api/connect", fields)
        .then(function (response) {
          if (!response.ok) {
            throw new Error("connect failed");
          }
          return response.json();
        })
        .then(function () {
          if (passwordInput) {
            passwordInput.value = "";
          }
          showMessage(messages.success, "ok");
        })
        .catch(function () {
          if (passwordInput) {
            passwordInput.value = "";
          }
          setSubmitEnabled(true);
          showMessage(messages.validationFailed, "error");
          logFrontend("api_error", { endpoint: "connect", has_init_data: hasInitData() });
        });
    });
  });
}());
""".replace("__MESSAGES__", messages).lstrip()


def build_signed_init_data(bot_token: str, payload: dict[str, str]) -> str:
    data_check_string = "\n".join(f"{key}={value}" for key, value in sorted(payload.items()))
    secret_key = hmac.new(b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256).digest()
    payload_hash = hmac.new(secret_key, data_check_string.encode("utf-8"), hashlib.sha256).hexdigest()
    return urlencode({**payload, "hash": payload_hash})
