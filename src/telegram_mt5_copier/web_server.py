from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import sys
from urllib.parse import parse_qs

from .config import AppConfig
from .credential_service import CredentialService
from .mt5.account_service import MT5AccountService
from .mt5.terminal_manager import TerminalManager
from .users import UserRepository
from .web_app import (
    CSRFTokenService,
    MT5OnboardingService,
    WebAppValidationError,
    render_onboarding_form,
    validate_telegram_web_app_init_data,
)


class OnboardingHandler(BaseHTTPRequestHandler):
    onboarding: MT5OnboardingService
    csrf: CSRFTokenService
    bot_token: str

    def log_message(self, format: str, *args: object) -> None:
        if self.command == "POST":
            sys.stderr.write("%s - - %s\n" % (self.address_string(), format % args))
            return
        super().log_message(format, *args)

    def do_GET(self) -> None:
        if self.path == "/health":
            safe_log("health_check")
            self.send_json({"status": "ok"})
            return
        if self.path != "/":
            self.send_error(404)
            return
        safe_log("page_loaded")
        self.send_html(render_onboarding_form())

    def do_POST(self) -> None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length).decode("utf-8")
            fields = {key: values[0] for key, values in parse_qs(body, keep_blank_values=True).items()}
            if self.path == "/api/log":
                self.handle_frontend_log(fields)
                return
            if self.path in {"/api/csrf", "/csrf"}:
                self.handle_csrf(fields)
                return
            if self.path in {"/api/connect", "/connect"}:
                self.handle_connect(fields)
                return
            self.send_error(404)
        except WebAppValidationError as exc:
            safe_log("validation_rejected", reason=safe_reason(str(exc)))
            self.send_json({"ok": False, "error": "Não foi possível validar sua sessão do Telegram."}, status=403)
        except Exception as exc:
            safe_log("api_error", error_type=type(exc).__name__)
            self.send_json({"ok": False, "error": "Falha ao processar solicitacao."}, status=500)

    def handle_csrf(self, fields: dict[str, str]) -> None:
        has_init_data = bool(fields.get("init_data", ""))
        safe_log("csrf_requested", init_data="presente" if has_init_data else "ausente")
        init = validate_telegram_web_app_init_data(fields.get("init_data", ""), self.bot_token)
        safe_log("validation_accepted", user_id=str(init.user.id))
        self.send_json({"ok": True, "csrf_token": self.csrf.issue(init.user.id)})

    def handle_connect(self, fields: dict[str, str]) -> None:
        scheme = self.headers.get("X-Forwarded-Proto", "http")
        safe_log(
            "connect_requested",
            init_data="presente" if fields.get("init_data") else "ausente",
            scheme=scheme,
        )
        result = self.onboarding.submit_account_form(
            init_data=fields.get("init_data", ""),
            csrf_token=fields.get("csrf_token", ""),
            request_scheme=scheme,
            broker_name=fields.get("broker_name", ""),
            server_name=fields.get("server_name", ""),
            login=fields.get("login", ""),
            password=fields.get("password", ""),
            account_alias=fields.get("account_alias", ""),
        )
        self.send_json(
            {
                "ok": True,
                "account_id": result.account_id,
                "masked_login": result.masked_login,
                "connection_status": result.connection_status,
            }
        )

    def handle_frontend_log(self, fields: dict[str, str]) -> None:
        event = safe_frontend_event(fields.get("event", "frontend_event"))
        has_init_data = "presente" if fields.get("has_init_data") == "true" else "ausente"
        safe_log(event, init_data=has_init_data, endpoint=safe_endpoint(fields.get("endpoint", "")))
        self.send_json({"ok": True})

    def send_html(self, body: str, status: int = 200) -> None:
        encoded = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_common_security_headers()
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def send_json(self, payload: dict[str, object], status: int = 200) -> None:
        encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_common_security_headers()
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def send_common_security_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", content_security_policy())


def main() -> int:
    try:
        config = AppConfig.load(create_dirs=True)
        if not config.telegram_bot_token:
            raise ValueError("TELEGRAM_BOT_TOKEN nao configurado.")
        if not config.mt5_credential_key:
            raise ValueError("MT5_CREDENTIAL_KEY nao configurada.")

        users = UserRepository(config.database_path)
        credential_service = CredentialService(config.mt5_credential_key)
        terminal_manager = TerminalManager(config.mt5_base_dir, config.mt5_template_path)
        accounts = MT5AccountService(
            config.database_path,
            credential_service=credential_service,
            terminal_manager=terminal_manager,
            allow_live_accounts=config.allow_live_accounts,
            max_accounts_per_vps=config.mt5_max_accounts_per_vps,
        )
        csrf = CSRFTokenService(config.mt5_credential_key)
        onboarding = MT5OnboardingService(
            bot_token=config.telegram_bot_token,
            users=users,
            accounts=accounts,
            csrf=csrf,
            require_https=True,
        )
        OnboardingHandler.bot_token = config.telegram_bot_token
        OnboardingHandler.csrf = csrf
        OnboardingHandler.onboarding = onboarding

        server = ThreadingHTTPServer(("127.0.0.1", 8080), OnboardingHandler)
        print("Mini App MT5 ouvindo em http://127.0.0.1:8080. Publique atras de HTTPS.")
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        print(f"Falha ao iniciar Mini App MT5: {exc}", file=sys.stderr)
        return 2

    return 0


def safe_log(event: str, **fields: str) -> None:
    safe_fields = " ".join(
        f"{safe_key(key)}={safe_value(value)}"
        for key, value in fields.items()
        if value
    )
    message = f"mini_app {safe_key(event)}"
    if safe_fields:
        message = f"{message} {safe_fields}"
    sys.stderr.write(f"{message}\n")


def safe_key(value: str) -> str:
    return "".join(char for char in value.lower().replace("-", "_") if char.isalnum() or char == "_")[:48]


def safe_value(value: object) -> str:
    text = str(value)
    return "".join(char for char in text if char.isalnum() or char in {"_", "-", "."})[:80]


def safe_reason(value: str) -> str:
    lowered = value.lower()
    if "initdata" in lowered:
        return "init_data"
    if "csrf" in lowered:
        return "csrf"
    if "https" in lowered:
        return "https"
    return "validation"


def safe_frontend_event(value: str) -> str:
    allowed = {
        "telegram_webapp_missing",
        "telegram_webapp_detected",
        "frontend_error",
        "frontend_unhandledrejection",
        "api_error",
    }
    return value if value in allowed else "frontend_event"


def safe_endpoint(value: str) -> str:
    return value if value in {"csrf", "connect"} else ""


def content_security_policy() -> str:
    return (
        "default-src 'self'; "
        "script-src 'self' https://telegram.org; "
        "connect-src 'self'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; "
        "base-uri 'self'; "
        "form-action 'self'; "
        "frame-ancestors 'self' https://web.telegram.org https://*.telegram.org"
    )


if __name__ == "__main__":
    raise SystemExit(main())
