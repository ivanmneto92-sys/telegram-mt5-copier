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
        if self.path != "/":
            self.send_error(404)
            return
        self.send_html(render_onboarding_form())

    def do_POST(self) -> None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length).decode("utf-8")
            fields = {key: values[0] for key, values in parse_qs(body, keep_blank_values=True).items()}
            if self.path == "/csrf":
                self.handle_csrf(fields)
                return
            if self.path == "/connect":
                self.handle_connect(fields)
                return
            self.send_error(404)
        except WebAppValidationError as exc:
            self.send_json({"ok": False, "error": str(exc)}, status=403)
        except Exception:
            self.send_json({"ok": False, "error": "Falha ao processar solicitacao."}, status=500)

    def handle_csrf(self, fields: dict[str, str]) -> None:
        init = validate_telegram_web_app_init_data(fields.get("init_data", ""), self.bot_token)
        self.send_json({"ok": True, "csrf_token": self.csrf.issue(init.user.id)})

    def handle_connect(self, fields: dict[str, str]) -> None:
        scheme = self.headers.get("X-Forwarded-Proto", "http")
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

    def send_html(self, body: str, status: int = 200) -> None:
        encoded = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def send_json(self, payload: dict[str, object], status: int = 200) -> None:
        encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


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


if __name__ == "__main__":
    raise SystemExit(main())
