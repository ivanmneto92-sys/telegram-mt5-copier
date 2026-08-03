from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from http.cookies import SimpleCookie
import json
import secrets
import sys
from urllib.parse import parse_qs, urlsplit

from .admin_auth import AdminBrowserAuthService
from .admin_panel import AdminIdentity, AdminPanelService, render_admin_panel, render_admin_script
from .config import AppConfig
from .credential_service import CredentialService
from .mt5.account_service import MT5AccountService
from .mt5.terminal_manager import TerminalManager
from .users import UserRepository
from .web_app import (
    CSRFTokenService,
    MT5OnboardingService,
    WebAppValidationError,
    render_miniapp_script,
    render_onboarding_form,
    validate_telegram_web_app_init_data,
)


class OnboardingHandler(BaseHTTPRequestHandler):
    onboarding: MT5OnboardingService
    admin_panel: AdminPanelService
    admin_browser_auth: AdminBrowserAuthService
    csrf: CSRFTokenService
    bot_token: str
    broker_options: tuple[str, ...] = ()
    brand_name: str = "Instituto Trader"
    instance_id: str = "main"

    def log_message(self, format: str, *args: object) -> None:
        if self.command == "POST":
            sys.stderr.write("%s - - %s\n" % (self.address_string(), format % args))
            return
        super().log_message(format, *args)

    def do_GET(self) -> None:
        path = self.route_path()
        if path == "/health":
            safe_log("health_check")
            self.send_json(
                {
                    "status": "ok",
                    "instance": self.instance_id,
                    "brand": self.brand_name,
                }
            )
            return
        if path == "/favicon.ico":
            self.send_empty(status=204)
            return
        if path == "/miniapp.js":
            self.send_javascript(render_miniapp_script())
            return
        if path == "/admin.js":
            self.send_javascript(render_admin_script())
            return
        if path == "/admin":
            safe_log("admin_page_loaded")
            script_nonce = generate_script_nonce()
            self.send_html(
                render_admin_panel(script_nonce, self.brand_name),
                script_nonce=script_nonce,
            )
            return
        if path != "/":
            self.send_error(404)
            return
        safe_log("page_loaded")
        script_nonce = generate_script_nonce()
        self.send_html(
            render_onboarding_form(
                script_nonce,
                broker_options=self.broker_options,
                brand_name=self.brand_name,
            ),
            script_nonce=script_nonce,
        )

    def do_HEAD(self) -> None:
        path = self.route_path()
        if path == "/health":
            safe_log("health_check")
            self.send_json(
                {
                    "status": "ok",
                    "instance": self.instance_id,
                    "brand": self.brand_name,
                },
                head_only=True,
            )
            return
        if path == "/favicon.ico":
            self.send_empty(status=204)
            return
        if path == "/miniapp.js":
            self.send_javascript(render_miniapp_script(), head_only=True)
            return
        if path == "/admin.js":
            self.send_javascript(render_admin_script(), head_only=True)
            return
        if path == "/admin":
            safe_log("admin_page_loaded")
            script_nonce = generate_script_nonce()
            self.send_html(
                render_admin_panel(script_nonce, self.brand_name),
                script_nonce=script_nonce,
                head_only=True,
            )
            return
        if path != "/":
            self.send_error(404)
            return
        safe_log("page_loaded")
        script_nonce = generate_script_nonce()
        self.send_html(
            render_onboarding_form(
                script_nonce,
                broker_options=self.broker_options,
                brand_name=self.brand_name,
            ),
            script_nonce=script_nonce,
            head_only=True,
        )

    def do_POST(self) -> None:
        path = ""
        try:
            path = self.route_path()
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length).decode("utf-8")
            fields = {key: values[0] for key, values in parse_qs(body, keep_blank_values=True).items()}
            if path == "/api/log":
                self.handle_frontend_log(fields)
                return
            if path in {"/api/csrf", "/csrf"}:
                self.handle_csrf(fields)
                return
            if path in {"/api/connect", "/connect"}:
                self.handle_connect(fields)
                return
            if path == "/api/admin/session":
                self.handle_admin_session(fields)
                return
            if path == "/api/admin/browser-login":
                self.handle_admin_browser_login(fields)
                return
            if path == "/api/admin/logout":
                self.handle_admin_logout(fields)
                return
            if path == "/api/admin/user-status":
                self.handle_admin_user_status(fields)
                return
            if path == "/api/admin/billing-update":
                self.handle_admin_billing_update(fields)
                return
            if path == "/api/admin/payment":
                self.handle_admin_payment(fields)
                return
            if path == "/api/admin/approve":
                self.handle_admin_approve(fields)
                return
            if path == "/api/admin/channel-approve":
                self.handle_admin_channel_action(fields, "approve")
                return
            if path == "/api/admin/channel-reject":
                self.handle_admin_channel_action(fields, "reject")
                return
            if path == "/api/admin/channel-revalidate":
                self.handle_admin_channel_action(fields, "revalidate")
                return
            if path == "/api/admin/channel-display-name":
                self.handle_admin_channel_display_name(fields)
                return
            self.send_error(404)
        except WebAppValidationError as exc:
            safe_log("validation_rejected", reason=safe_reason(str(exc)))
            error = (
                "Acesso administrativo não autorizado."
                if path.startswith("/api/admin/")
                else "Não foi possível validar sua sessão do Telegram."
            )
            self.send_json({"ok": False, "error": error}, status=403)
        except ValueError as exc:
            safe_log("api_rejected", endpoint=safe_endpoint(path), reason=safe_reason(str(exc)))
            self.send_json({"ok": False, "error": str(exc)}, status=400)
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

    def handle_admin_session(self, fields: dict[str, str]) -> None:
        identity = self.authenticate_admin(fields)
        self.send_admin_dashboard(identity)

    def handle_admin_browser_login(self, fields: dict[str, str]) -> None:
        try:
            session = self.admin_browser_auth.consume_login_token(fields.get("token", ""))
        except ValueError as exc:
            raise WebAppValidationError(str(exc)) from exc
        identity = AdminIdentity(session.admin_telegram_user_id, None)
        self.send_admin_dashboard(
            identity,
            extra_headers=(("Set-Cookie", admin_session_cookie(session.session_token)),),
        )

    def handle_admin_logout(self, fields: dict[str, str]) -> None:
        identity = self.authenticate_admin(fields)
        if not self.csrf.validate(fields.get("csrf_token", ""), identity.telegram_user_id):
            raise WebAppValidationError("CSRF invalido.")
        session_token = self.admin_session_cookie()
        if session_token:
            self.admin_browser_auth.revoke_session(session_token)
        self.send_json(
            {"ok": True},
            extra_headers=(("Set-Cookie", clear_admin_session_cookie()),),
        )

    def send_admin_dashboard(
        self,
        identity: AdminIdentity,
        *,
        extra_headers: tuple[tuple[str, str], ...] = (),
    ) -> None:
        payload = self.admin_panel.dashboard()
        payload.update(
            {
                "ok": True,
                "csrf_token": self.csrf.issue(identity.telegram_user_id),
                "admin": {
                    "telegram_user_id": identity.telegram_user_id,
                    "username": identity.username,
                },
            }
        )
        safe_log("admin_session_accepted", user_id=str(identity.telegram_user_id))
        self.send_json(payload, extra_headers=extra_headers)

    def handle_admin_user_status(self, fields: dict[str, str]) -> None:
        identity = self.authenticate_admin_mutation(fields)
        try:
            target_user_id = int(fields.get("user_id", ""))
        except ValueError as exc:
            raise ValueError("Cliente inválido.") from exc
        result = self.admin_panel.set_user_status(
            admin_telegram_user_id=identity.telegram_user_id,
            target_user_id=target_user_id,
            status=fields.get("status", ""),
        )
        safe_log(
            "admin_user_status_changed",
            admin_id=str(identity.telegram_user_id),
            target_id=str(target_user_id),
            status=str(result["status"]),
        )
        self.send_json({"ok": True, "user": result})

    def handle_admin_billing_update(self, fields: dict[str, str]) -> None:
        identity = self.authenticate_admin_mutation(fields)
        target_user_id = parsed_user_id(fields)
        result = self.admin_panel.update_billing(
            admin_telegram_user_id=identity.telegram_user_id,
            target_user_id=target_user_id,
            customer_name=fields.get("customer_name", ""),
            email=fields.get("email", ""),
            phone=fields.get("phone", ""),
            plan_name=fields.get("plan_name", ""),
            monthly_amount=fields.get("monthly_amount", ""),
            due_date=fields.get("due_date", ""),
            billing_status=fields.get("billing_status", ""),
            notes=fields.get("notes", ""),
        )
        safe_log(
            "admin_billing_updated",
            admin_id=str(identity.telegram_user_id),
            target_id=str(target_user_id),
        )
        self.send_json({"ok": True, "billing": result})

    def handle_admin_payment(self, fields: dict[str, str]) -> None:
        identity = self.authenticate_admin_mutation(fields)
        target_user_id = parsed_user_id(fields)
        result = self.admin_panel.record_payment(
            admin_telegram_user_id=identity.telegram_user_id,
            target_user_id=target_user_id,
            amount=fields.get("amount", ""),
            paid_at=fields.get("paid_at", ""),
            method=fields.get("method", ""),
            reference=fields.get("reference", ""),
            next_due_date=fields.get("next_due_date", ""),
        )
        safe_log(
            "admin_payment_recorded",
            admin_id=str(identity.telegram_user_id),
            target_id=str(target_user_id),
        )
        self.send_json({"ok": True, "payment": result})

    def handle_admin_approve(self, fields: dict[str, str]) -> None:
        identity = self.authenticate_admin_mutation(fields)
        target_user_id = parsed_user_id(fields)
        result = self.admin_panel.approve_paid_access(
            admin_telegram_user_id=identity.telegram_user_id,
            target_user_id=target_user_id,
            amount=fields.get("amount", ""),
            paid_at=fields.get("paid_at", ""),
            method=fields.get("method", ""),
            reference=fields.get("reference", ""),
            expires_on=fields.get("expires_on", ""),
        )
        safe_log(
            "admin_access_approved",
            admin_id=str(identity.telegram_user_id),
            target_id=str(target_user_id),
        )
        self.send_json({"ok": True, "approval": result})

    def handle_admin_channel_action(self, fields: dict[str, str], action: str) -> None:
        identity = self.authenticate_admin_mutation(fields)
        try:
            request_id = int(fields.get("request_id", ""))
        except ValueError as exc:
            raise ValueError("Solicitação de canal inválida.") from exc
        if action == "approve":
            result = self.admin_panel.approve_channel_request(
                admin_telegram_user_id=identity.telegram_user_id,
                request_id=request_id,
            )
        elif action == "reject":
            result = self.admin_panel.reject_channel_request(
                admin_telegram_user_id=identity.telegram_user_id,
                request_id=request_id,
                notes=fields.get("notes", ""),
            )
        else:
            result = self.admin_panel.revalidate_channel_request(
                admin_telegram_user_id=identity.telegram_user_id,
                request_id=request_id,
            )
        safe_log(
            f"admin_channel_{action}",
            admin_id=str(identity.telegram_user_id),
            request_id=str(request_id),
        )
        self.send_json({"ok": True, "channel_request": result})

    def handle_admin_channel_display_name(self, fields: dict[str, str]) -> None:
        identity = self.authenticate_admin_mutation(fields)
        try:
            channel_id = int(fields.get("channel_id", ""))
        except ValueError as exc:
            raise ValueError("Canal inválido.") from exc
        result = self.admin_panel.update_channel_display_name(
            admin_telegram_user_id=identity.telegram_user_id,
            channel_id=channel_id,
            display_name=fields.get("display_name", ""),
        )
        safe_log(
            "admin_channel_display_name_changed",
            admin_id=str(identity.telegram_user_id),
            channel_id=str(channel_id),
        )
        self.send_json({"ok": True, "channel": result})

    def authenticate_admin_mutation(self, fields: dict[str, str]) -> AdminIdentity:
        identity = self.authenticate_admin(fields)
        if not self.csrf.validate(fields.get("csrf_token", ""), identity.telegram_user_id):
            raise WebAppValidationError("CSRF invalido.")
        return identity

    def authenticate_admin(self, fields: dict[str, str]) -> AdminIdentity:
        init_data = fields.get("init_data", "")
        if init_data:
            return self.admin_panel.authenticate(init_data)
        session_token = self.admin_session_cookie()
        try:
            admin_id = self.admin_browser_auth.authenticate_session(session_token)
        except ValueError as exc:
            raise WebAppValidationError(str(exc)) from exc
        return AdminIdentity(admin_id, None)

    def admin_session_cookie(self) -> str:
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
        except Exception:
            return ""
        morsel = cookie.get("admin_session")
        return morsel.value if morsel is not None else ""

    def route_path(self) -> str:
        return urlsplit(self.path).path

    def send_html(
        self,
        body: str,
        status: int = 200,
        *,
        script_nonce: str = "",
        head_only: bool = False,
    ) -> None:
        encoded = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_common_security_headers(script_nonce=script_nonce)
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        if head_only:
            return
        self.wfile.write(encoded)

    def send_json(
        self,
        payload: dict[str, object],
        status: int = 200,
        *,
        head_only: bool = False,
        extra_headers: tuple[tuple[str, str], ...] = (),
    ) -> None:
        encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_common_security_headers()
        for name, value in extra_headers:
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        if head_only:
            return
        self.wfile.write(encoded)

    def send_javascript(self, body: str, status: int = 200, *, head_only: bool = False) -> None:
        encoded = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/javascript; charset=utf-8")
        self.send_common_security_headers()
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        if head_only:
            return
        self.wfile.write(encoded)

    def send_empty(self, status: int = 204) -> None:
        self.send_response(status)
        self.send_common_security_headers()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def send_common_security_headers(self, *, script_nonce: str = "") -> None:
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", content_security_policy(script_nonce))


def main() -> int:
    try:
        config = AppConfig.load(create_dirs=True)
        if not config.telegram_bot_token:
            raise ValueError("TELEGRAM_BOT_TOKEN nao configurado.")
        if not config.mt5_credential_key:
            raise ValueError("MT5_CREDENTIAL_KEY nao configurada.")

        users = UserRepository(config.database_path)
        credential_service = CredentialService(config.mt5_credential_key)
        terminal_manager = TerminalManager(
            config.mt5_base_dir,
            config.mt5_template_path,
            config.mt5_broker_template_paths,
        )
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
        admin_panel = AdminPanelService(
            config.database_path,
            bot_token=config.telegram_bot_token,
            admin_ids=config.bot_admin_ids,
        )
        admin_browser_auth = AdminBrowserAuthService(
            config.database_path,
            admin_ids=config.bot_admin_ids,
        )
        OnboardingHandler.bot_token = config.telegram_bot_token
        OnboardingHandler.broker_options = terminal_manager.available_brokers()
        OnboardingHandler.brand_name = config.brand_name
        OnboardingHandler.instance_id = config.instance_id
        OnboardingHandler.csrf = csrf
        OnboardingHandler.onboarding = onboarding
        OnboardingHandler.admin_panel = admin_panel
        OnboardingHandler.admin_browser_auth = admin_browser_auth

        server = ThreadingHTTPServer(
            (config.onboarding_host, config.onboarding_port),
            OnboardingHandler,
        )
        print(
            f"Mini App MT5 da instancia {config.instance_id} ouvindo em "
            f"{config.local_onboarding_url}. Publique atras de HTTPS."
        )
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
    if "ja utilizado" in lowered:
        return "init_data_replayed"
    if "expirada" in lowered:
        return "init_data_expired"
    if "auth_date" in lowered:
        return "init_data_clock"
    if "hash" in lowered or "initdata invalido" in lowered:
        return "init_data_signature"
    if "usuario do web app" in lowered:
        return "init_data_user"
    if "token do bot" in lowered:
        return "bot_token"
    if "initdata ausente" in lowered:
        return "init_data_missing"
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
    allowed = {
        "csrf",
        "connect",
        "/api/admin/session",
        "/api/admin/browser-login",
        "/api/admin/logout",
        "/api/admin/user-status",
        "/api/admin/billing-update",
        "/api/admin/payment",
        "/api/admin/approve",
        "/api/admin/channel-approve",
        "/api/admin/channel-reject",
        "/api/admin/channel-revalidate",
        "/api/admin/channel-display-name",
    }
    return value if value in allowed else ""


def generate_script_nonce() -> str:
    return secrets.token_urlsafe(24)


def parsed_user_id(fields: dict[str, str]) -> int:
    try:
        return int(fields.get("user_id", ""))
    except ValueError as exc:
        raise ValueError("Cliente inválido.") from exc


def admin_session_cookie(token: str) -> str:
    return (
        f"admin_session={token}; Path=/; Max-Age=43200; "
        "Secure; HttpOnly; SameSite=Strict"
    )


def clear_admin_session_cookie() -> str:
    return (
        "admin_session=; Path=/; Max-Age=0; "
        "Secure; HttpOnly; SameSite=Strict"
    )


def content_security_policy(script_nonce: str = "") -> str:
    nonce_directive = f" 'nonce-{script_nonce}'" if script_nonce else ""
    return (
        "default-src 'self'; "
        f"script-src 'self' https://telegram.org{nonce_directive}; "
        "connect-src 'self'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; "
        "base-uri 'self'; "
        "form-action 'self'"
    )


if __name__ == "__main__":
    raise SystemExit(main())
