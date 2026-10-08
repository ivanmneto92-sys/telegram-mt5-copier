from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from http.cookies import SimpleCookie
import json
import re
import secrets
import sys
from urllib.parse import parse_qs, urljoin, urlsplit

from .admin_auth import AdminBrowserAuthService
from .admin_panel import AdminIdentity, AdminPanelService, render_admin_panel, render_admin_script
from .client_auth import CLIENT_SESSION_TTL_HOURS, ClientBrowserAuthService, normalize_email
from .client_portal import AccountNotFoundError, ClientPortalService
from .config import AppConfig
from .credential_service import CredentialService
from .email_service import (
    EmailSendError,
    EmailService,
    NullEmailService,
    ResendEmailService,
    email_changed_email,
    email_confirmation_email,
    mt5_account_connected_email,
    mt5_account_removed_email,
    password_changed_email,
    password_reset_email,
)
from .mt5.account_service import MT5AccountService
from .mt5.terminal_manager import TerminalManager
from .users import UserRepository
from .web_app import (
    CSRFTokenService,
    MT5OnboardingService,
    SimpleRateLimiter,
    WebAppValidationError,
    render_miniapp_script,
    render_onboarding_form,
    validate_telegram_web_app_init_data,
)


class InvalidAccountIdError(ValueError):
    """account_id com formato invalido; vira HTTP 400 (nao 401)."""


def client_csrf_identity(user_id: int) -> str:
    """Namespace do token CSRF do cliente, separado do namespace do admin
    (que usa o telegram_user_id puro) -- evita que um users.id pequeno
    colida com um telegram_user_id de admin que tenha o mesmo valor."""
    return f"client:{user_id}"


def parse_account_id(raw: str | None) -> int | None:
    if raw is None or raw == "":
        return None
    if not raw.isascii() or not raw.isdigit() or int(raw) <= 0:
        raise InvalidAccountIdError("Identificador de conta invalido.")
    return int(raw)


class OnboardingHandler(BaseHTTPRequestHandler):
    onboarding: MT5OnboardingService
    admin_panel: AdminPanelService
    admin_browser_auth: AdminBrowserAuthService
    client_browser_auth: ClientBrowserAuthService
    client_portal: ClientPortalService
    csrf: CSRFTokenService
    password_reset_rate_limiter: SimpleRateLimiter = SimpleRateLimiter(limit=3, window_seconds=900)
    bot_token: str
    broker_options: tuple[str, ...] = ()
    broker_servers: dict[str, tuple[str, ...]] = {}
    brand_name: str = "Instituto Trader"
    instance_id: str = "main"
    client_app_url: str | None = None
    email_service: EmailService = NullEmailService()

    def log_message(self, format: str, *args: object) -> None:
        if self.command == "POST":
            sys.stderr.write("%s - - %s\n" % (self.address_string(), format % args))
            return
        super().log_message(format, *args)

    def do_GET(self) -> None:
        path = self.route_path()
        if path.startswith("/api/v1/"):
            self.handle_client_api_get(path)
            return
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
                broker_servers=self.broker_servers,
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
                broker_servers=self.broker_servers,
            ),
            script_nonce=script_nonce,
            head_only=True,
        )

    def do_POST(self) -> None:
        path = ""
        try:
            path = self.route_path()
            length = int(self.headers.get("Content-Length", "0"))
            if length < 0 or length > 65_536:
                self.send_json({"ok": False, "error": "Requisição muito grande."}, status=413)
                return
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
            if path == "/api/admin/login":
                self.handle_admin_password_login(fields)
                return
            if path == "/api/admin/password":
                self.handle_admin_password_setup(fields)
                return
            if path == "/api/admin/logout":
                self.handle_admin_logout(fields)
                return
            if path == "/api/admin/user-status":
                self.handle_admin_user_status(fields)
                return
            if path == "/api/admin/mt5-account-delete":
                self.handle_admin_mt5_account_delete(fields)
                return
            if path == "/api/admin/client-delete":
                self.handle_admin_client_delete(fields)
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
            if path == "/api/admin/channel-status":
                self.handle_admin_channel_status(fields)
                return
            if path == "/api/admin/admin-add":
                self.handle_admin_add_admin(fields)
                return
            if path == "/api/admin/admin-revoke":
                self.handle_admin_revoke_admin(fields)
                return
            if path == "/api/admin/mt5-account-queue-pilot-status":
                self.handle_admin_mt5_account_queue_pilot_status(fields)
                return
            if path == "/api/v1/auth/browser-login":
                self.handle_client_browser_login(fields)
                return
            if path == "/api/v1/auth/login":
                self.handle_client_password_login(fields)
                return
            if path == "/api/v1/auth/register":
                self.handle_client_registration(fields)
                return
            if path == "/api/v1/auth/password":
                self.handle_client_password_setup(fields)
                return
            if path == "/api/v1/auth/password/forgot":
                self.handle_client_password_forgot(fields)
                return
            if path == "/api/v1/auth/password/reset":
                self.handle_client_password_reset(fields)
                return
            if path == "/api/v1/auth/email/confirm":
                self.handle_client_email_confirm(fields)
                return
            if path == "/api/v1/auth/email/resend":
                self.handle_client_email_resend(fields)
                return
            if path == "/api/v1/auth/logout":
                self.handle_client_logout()
                return
            if path == "/api/v1/profile":
                self.handle_client_profile_update(fields)
                return
            if path == "/api/v1/risk":
                self.handle_client_risk_update(fields)
                return
            if path == "/api/v1/accounts":
                self.handle_client_account_create(fields)
                return
            if path == "/api/v1/accounts/remove":
                self.handle_client_account_remove(fields)
                return
            if path == "/api/v1/accounts/test-connection":
                self.handle_client_account_test_connection(fields)
                return
            if path == "/api/v1/channels/toggle":
                self.handle_client_channel_toggle(fields)
                return
            if path == "/api/v1/channels/mode":
                self.handle_client_channel_mode_update(fields)
                return
            if path == "/api/v1/channels/suggest":
                self.handle_client_channel_suggest(fields)
                return
            if path == "/api/v1/copier/pause-toggle":
                self.handle_client_copier_pause_toggle(fields)
                return
            if path == "/api/v1/copier/signal-pause":
                self.handle_client_signal_pause(fields)
                return
            if path == "/api/v1/copier/daily-stop-toggle":
                self.handle_client_daily_stop_toggle(fields)
                return
            if path == "/api/v1/settings":
                self.handle_client_settings_update(fields)
                return
            if path == "/api/v1/push/subscribe":
                self.handle_client_push_subscribe(fields)
                return
            if path == "/api/v1/push/unsubscribe":
                self.handle_client_push_unsubscribe(fields)
                return
            self.send_error(404)
        except WebAppValidationError as exc:
            safe_log("validation_rejected", reason=safe_reason(str(exc)))
            if path.startswith("/api/admin/"):
                error = "Acesso administrativo não autorizado."
            elif path.startswith("/api/v1/"):
                error = "Link de acesso inválido ou expirado."
            else:
                error = "Não foi possível validar sua sessão do Telegram."
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
            custom_server_name=fields.get("custom_server_name", ""),
            login=fields.get("login", ""),
            password=fields.get("password", ""),
            account_alias=fields.get("account_alias", ""),
            product_kind=fields.get("product_kind", ""),
        )
        self.send_json(
            {
                "ok": True,
                "account_id": result.account_id,
                "masked_login": result.masked_login,
                "connection_status": result.connection_status,
                "product_kind": result.product_kind,
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

    def handle_client_api_get(self, path: str) -> None:
        try:
            user_id = self.authenticate_client()
            account_id = parse_account_id(
                parse_qs(urlsplit(self.path).query).get("account_id", [None])[0]
            )
            if path in {"/api/v1/session", "/api/v1/dashboard"}:
                payload = self.client_portal.dashboard(user_id, account_id)
                payload["csrf_token"] = self.csrf.issue(client_csrf_identity(user_id))
            elif path == "/api/v1/accounts":
                payload = self.client_portal.accounts(user_id)
            elif path == "/api/v1/brokers":
                payload = self.client_portal.broker_catalog()
            elif path == "/api/v1/channels":
                payload = self.client_portal.channels(user_id)
            elif path == "/api/v1/operations":
                date = parse_qs(urlsplit(self.path).query).get("date", [None])[0]
                payload = self.client_portal.operations(user_id, account_id=account_id, date=date)
            elif path == "/api/v1/performance-calendar":
                month = parse_qs(urlsplit(self.path).query).get("month", [None])[0]
                if not month or not re.fullmatch(r"\d{4}-\d{2}", month):
                    self.send_json(
                        {"ok": False, "error": "Informe o mes no formato AAAA-MM."}, status=400
                    )
                    return
                payload = self.client_portal.performance_calendar(
                    user_id, month=month, account_id=account_id
                )
            elif path == "/api/v1/profile":
                payload = self.client_portal.profile(user_id)
            elif path == "/api/v1/financial":
                payload = self.client_portal.financial(user_id)
            elif path == "/api/v1/risk":
                payload = self.client_portal.risk(user_id, account_id)
            elif path == "/api/v1/settings":
                payload = self.client_portal.news_preference(user_id)
            elif path == "/api/v1/push/vapid-public-key":
                payload = self.client_portal.push_vapid_public_key()
            else:
                self.send_error(404)
                return
            # Renova tambem o cookie (a sessao no banco ja foi renovada em
            # authenticate_client), pra quem usa o app nao cair no login.
            extra_headers: tuple[tuple[str, str], ...] = ()
            if path in {"/api/v1/session", "/api/v1/dashboard"}:
                extra_headers = (
                    ("Set-Cookie", client_session_cookie(self.client_session_cookie())),
                )
            self.send_json({"ok": True, **payload}, extra_headers=extra_headers)
        except InvalidAccountIdError as exc:
            self.send_json({"ok": False, "error": str(exc)}, status=400)
        except AccountNotFoundError as exc:
            self.send_json({"ok": False, "error": str(exc)}, status=404)
        except ValueError as exc:
            safe_log("client_session_rejected", reason=safe_reason(str(exc)))
            self.send_json({"ok": False, "error": "Sessao expirada."}, status=401)

    def handle_client_browser_login(self, fields: dict[str, str]) -> None:
        try:
            session = self.client_browser_auth.consume_login_token(fields.get("token", ""))
        except ValueError as exc:
            raise WebAppValidationError(str(exc)) from exc
        self.send_client_session(session.user_id, session.session_token)

    def handle_client_password_login(self, fields: dict[str, str]) -> None:
        try:
            session = self.client_browser_auth.login(
                email=fields.get("email", ""),
                password=fields.get("password", ""),
            )
        except ValueError as exc:
            safe_log("client_password_login_rejected", reason="credentials")
            self.send_json({"ok": False, "error": str(exc)}, status=401)
            return
        safe_log("client_password_login_accepted")
        self.send_client_session(session.user_id, session.session_token)

    def handle_client_registration(self, fields: dict[str, str]) -> None:
        if fields.get("accepted_terms") != "true":
            raise ValueError("Confirme os Termos de Uso e a Política de Privacidade.")
        session = self.client_browser_auth.register(
            customer_name=fields.get("customer_name", ""),
            email=fields.get("email", ""),
            phone=fields.get("phone", ""),
            password=fields.get("password", ""),
        )
        safe_log("client_registration_accepted")
        self.send_email_confirmation_best_effort(session.user_id)
        self.send_client_session(session.user_id, session.session_token)

    def send_email_confirmation_best_effort(self, user_id: int) -> None:
        """Envia o e-mail de confirmacao sem nunca derrubar o fluxo chamador.

        Falha de e-mail (chave ausente, provedor fora do ar) nao pode impedir
        o cadastro nem o reenvio manual — o cliente so tenta de novo depois.
        """
        if not self.client_app_url:
            return
        try:
            confirm_url = self.client_browser_auth.request_email_confirmation(
                user_id, urljoin(self.client_app_url, "confirmar-email")
            )
            email = str(self.client_portal.profile(user_id)["profile"]["email"])
            subject, html = email_confirmation_email(brand_name=self.brand_name, confirm_url=confirm_url)
            self.email_service.send(to=email, subject=subject, html=html)
        except (ValueError, EmailSendError) as exc:
            safe_log("email_confirmation_send_failed", reason=safe_reason(str(exc)))

    def send_password_changed_notification_best_effort(self, user_id: int) -> None:
        """Avisa por e-mail que a senha da conta acabou de mudar.

        Best-effort (nunca derruba o fluxo de troca de senha em si) — mesmo
        padrao de send_email_confirmation_best_effort.
        """
        if not self.client_app_url:
            return
        try:
            email = str(self.client_portal.profile(user_id)["profile"]["email"])
            subject, html = password_changed_email(
                brand_name=self.brand_name,
                security_url=urljoin(self.client_app_url, "esqueci-senha"),
            )
            self.email_service.send(to=email, subject=subject, html=html)
        except (ValueError, EmailSendError) as exc:
            safe_log("password_changed_notification_failed", reason=safe_reason(str(exc)))

    def send_email_changed_notification_best_effort(self, *, old_email: str, new_email: str) -> None:
        """Avisa o e-mail ANTIGO que o e-mail de acesso da conta foi trocado."""
        if not self.client_app_url or not old_email:
            return
        try:
            subject, html = email_changed_email(
                brand_name=self.brand_name,
                new_email=new_email,
                profile_url=urljoin(self.client_app_url, "perfil"),
            )
            self.email_service.send(to=old_email, subject=subject, html=html)
        except EmailSendError as exc:
            safe_log("email_changed_notification_failed", reason=safe_reason(str(exc)))

    def send_mt5_account_connected_notification_best_effort(
        self, user_id: int, account: dict[str, object]
    ) -> None:
        if not self.client_app_url:
            return
        try:
            email = str(self.client_portal.profile(user_id)["profile"]["email"])
            subject, html = mt5_account_connected_email(
                brand_name=self.brand_name,
                broker=str(account.get("broker", "")),
                server=str(account.get("server", "")),
                masked_login=str(account.get("masked_login", "")),
                accounts_url=urljoin(self.client_app_url, "conta-mt5"),
            )
            self.email_service.send(to=email, subject=subject, html=html)
        except (ValueError, EmailSendError) as exc:
            safe_log("mt5_account_connected_notification_failed", reason=safe_reason(str(exc)))

    def send_mt5_account_removed_notification_best_effort(
        self, user_id: int, account: dict[str, object]
    ) -> None:
        if not self.client_app_url:
            return
        try:
            email = str(self.client_portal.profile(user_id)["profile"]["email"])
            subject, html = mt5_account_removed_email(
                brand_name=self.brand_name,
                broker=str(account.get("broker", "")),
                masked_login=str(account.get("masked_login", "")),
                accounts_url=urljoin(self.client_app_url, "conta-mt5"),
            )
            self.email_service.send(to=email, subject=subject, html=html)
        except (ValueError, EmailSendError) as exc:
            safe_log("mt5_account_removed_notification_failed", reason=safe_reason(str(exc)))

    def handle_client_password_forgot(self, fields: dict[str, str]) -> None:
        email = fields.get("email", "")
        try:
            rate_limit_key: str | None = normalize_email(email)
        except ValueError:
            rate_limit_key = None
        # Limita por e-mail normalizado (nao por IP): o risco real e alguem
        # martelar reenvios contra a caixa de entrada de UM cliente, nao um
        # unico atacante testando varios enderecos. Um e-mail mal formado
        # nunca gera envio de qualquer forma, entao pula o limitador pra ele.
        if rate_limit_key is None or self.password_reset_rate_limiter.allow(rate_limit_key):
            try:
                if self.client_app_url:
                    reset_url = self.client_browser_auth.request_password_reset(
                        email, urljoin(self.client_app_url, "redefinir-senha")
                    )
                    if reset_url is not None:
                        subject, html = password_reset_email(
                            brand_name=self.brand_name, reset_url=reset_url
                        )
                        self.email_service.send(to=normalize_email(email), subject=subject, html=html)
            except (ValueError, EmailSendError) as exc:
                # Nunca revela ao chamador se o e-mail existe ou se o envio falhou.
                safe_log("password_reset_send_failed", reason=safe_reason(str(exc)))
        else:
            safe_log("password_reset_rate_limited")
        safe_log("password_reset_requested")
        self.send_json({"ok": True})

    def handle_client_password_reset(self, fields: dict[str, str]) -> None:
        user_id = self.client_browser_auth.reset_password(
            fields.get("token", ""), fields.get("password", "")
        )
        safe_log("password_reset_completed")
        self.send_password_changed_notification_best_effort(user_id)
        self.send_json({"ok": True})

    def handle_client_email_confirm(self, fields: dict[str, str]) -> None:
        self.client_browser_auth.confirm_email(fields.get("token", ""))
        safe_log("email_confirmed")
        self.send_json({"ok": True})

    def handle_client_email_resend(self, fields: dict[str, str]) -> None:
        user_id = self.authenticate_client_mutation(fields)
        self.send_email_confirmation_best_effort(user_id)
        safe_log("email_confirmation_resent", user_id=str(user_id))
        self.send_json({"ok": True})

    def handle_client_password_setup(self, fields: dict[str, str]) -> None:
        user_id = self.authenticate_client_mutation(fields)
        self.client_browser_auth.set_password_for_user(
            user_id,
            email=fields.get("email", ""),
            password=fields.get("password", ""),
        )
        safe_log("client_password_configured", user_id=str(user_id))
        self.send_password_changed_notification_best_effort(user_id)
        self.send_json({"ok": True})

    def send_client_session(self, user_id: int, session_token: str) -> None:
        payload = self.client_portal.dashboard(user_id)
        payload["csrf_token"] = self.csrf.issue(client_csrf_identity(user_id))
        self.send_json(
            {"ok": True, **payload},
            extra_headers=(("Set-Cookie", client_session_cookie(session_token)),),
        )

    def handle_client_logout(self) -> None:
        session_token = self.client_session_cookie()
        if session_token:
            self.client_browser_auth.revoke_session(session_token)
        self.send_json(
            {"ok": True},
            extra_headers=(("Set-Cookie", clear_client_session_cookie()),),
        )

    def handle_client_profile_update(self, fields: dict[str, str]) -> None:
        user_id = self.authenticate_client_mutation(fields)
        old_email = str(self.client_portal.profile(user_id)["profile"]["email"] or "")
        payload = self.client_portal.update_profile(
            user_id,
            customer_name=fields.get("customer_name", ""),
            email=fields.get("email", ""),
            phone=fields.get("phone", ""),
        )
        safe_log("client_profile_updated", user_id=str(user_id))
        new_email = str(payload["profile"]["email"] or "")
        if old_email and new_email and old_email.casefold() != new_email.casefold():
            self.send_email_changed_notification_best_effort(old_email=old_email, new_email=new_email)
            self.send_email_confirmation_best_effort(user_id)
        self.send_json({"ok": True, **payload})

    def handle_client_risk_update(self, fields: dict[str, str]) -> None:
        user_id = self.authenticate_client_mutation(fields)
        risk_fields = dict(fields)
        risk_fields.pop("csrf_token", None)
        account_id = parse_account_id(risk_fields.pop("account_id", None))
        try:
            payload = self.client_portal.update_risk(user_id, risk_fields, account_id)
        except AccountNotFoundError as exc:
            self.send_json({"ok": False, "error": str(exc)}, status=404)
            return
        safe_log("client_risk_updated", user_id=str(user_id))
        self.send_json({"ok": True, **payload})

    def handle_client_account_create(self, fields: dict[str, str]) -> None:
        user_id = self.authenticate_client_mutation(fields)
        payload = self.client_portal.add_account(
            user_id,
            broker_name=fields.get("broker_name", ""),
            server_name=fields.get("server_name", ""),
            custom_server_name=fields.get("custom_server_name", ""),
            login=fields.get("login", ""),
            password=fields.get("password", ""),
            account_alias=fields.get("account_alias", ""),
            product_kind=fields.get("product_kind", "") or "signal_copier",
        )
        safe_log("client_account_created", user_id=str(user_id))
        account = payload.get("account")
        if isinstance(account, dict):
            self.send_mt5_account_connected_notification_best_effort(user_id, account)
        self.send_json({"ok": True, **payload})

    def handle_client_account_remove(self, fields: dict[str, str]) -> None:
        user_id = self.authenticate_client_mutation(fields)
        account_id = parse_account_id(fields.get("account_id"))
        if account_id is None:
            raise InvalidAccountIdError("Identificador de conta invalido.")
        try:
            removed = self.client_portal.remove_account(user_id, account_id)
        except AccountNotFoundError as exc:
            self.send_json({"ok": False, "error": str(exc)}, status=404)
            return
        safe_log("client_account_removed", user_id=str(user_id))
        account = removed.get("account")
        if isinstance(account, dict):
            self.send_mt5_account_removed_notification_best_effort(user_id, account)
        self.send_json({"ok": True})

    def handle_client_account_test_connection(self, fields: dict[str, str]) -> None:
        user_id = self.authenticate_client_mutation(fields)
        account_id = parse_account_id(fields.get("account_id"))
        if account_id is None:
            raise InvalidAccountIdError("Identificador de conta invalido.")
        try:
            result = self.client_portal.test_mt5_connection(user_id, account_id)
        except AccountNotFoundError as exc:
            self.send_json({"ok": False, "error": str(exc)}, status=404)
            return
        except ValueError as exc:
            self.send_json({"ok": False, "error": str(exc)}, status=400)
            return
        safe_log("client_mt5_connection_tested", user_id=str(user_id), account_id=str(account_id))
        self.send_json({"ok": True, **result})

    def handle_client_channel_toggle(self, fields: dict[str, str]) -> None:
        user_id = self.authenticate_client_mutation(fields)
        try:
            channel_id = int(fields.get("channel_id", ""))
        except (TypeError, ValueError):
            raise ValueError("Identificador de canal invalido.") from None
        payload = self.client_portal.toggle_channel(user_id, channel_id)
        safe_log(
            "client_channel_toggled",
            user_id=str(user_id),
            channel_id=str(channel_id),
            enabled=str(payload["enabled"]).lower(),
        )
        self.send_json({"ok": True, **payload})

    def handle_client_channel_mode_update(self, fields: dict[str, str]) -> None:
        user_id = self.authenticate_client_mutation(fields)
        mode = fields.get("mode", "")
        payload = self.client_portal.set_channel_mode(user_id, mode)
        safe_log("client_channel_mode_updated", user_id=str(user_id), mode=mode)
        self.send_json({"ok": True, **payload})

    def handle_client_channel_suggest(self, fields: dict[str, str]) -> None:
        user_id = self.authenticate_client_mutation(fields)
        link = fields.get("link", "")
        payload = self.client_portal.suggest_channel(user_id, link)
        safe_log("client_channel_suggested", user_id=str(user_id))
        self.send_json({"ok": True, **payload})

    def handle_client_copier_pause_toggle(self, fields: dict[str, str]) -> None:
        user_id = self.authenticate_client_mutation(fields)
        payload = self.client_portal.toggle_copier_pause(user_id)
        safe_log("client_copier_status_toggled", user_id=str(user_id), status=str(payload["status"]))
        self.send_json({"ok": True, **payload})

    def handle_client_signal_pause(self, fields: dict[str, str]) -> None:
        user_id = self.authenticate_client_mutation(fields)
        mode = fields.get("mode", "")
        payload = self.client_portal.set_signal_pause(user_id, mode)
        safe_log("client_signal_pause", user_id=str(user_id), mode=mode)
        self.send_json({"ok": True, **payload})

    def handle_client_daily_stop_toggle(self, fields: dict[str, str]) -> None:
        user_id = self.authenticate_client_mutation(fields)
        payload = self.client_portal.toggle_daily_stop(user_id)
        safe_log(
            "client_daily_stop_toggled",
            user_id=str(user_id),
            active=str(payload["daily_signal_pause_active"]).lower(),
        )
        self.send_json({"ok": True, **payload})

    def handle_client_settings_update(self, fields: dict[str, str]) -> None:
        user_id = self.authenticate_client_mutation(fields)
        payload: dict[str, object] = {}
        if "avoid_high_impact_news" in fields:
            avoid_high_impact_news = fields.get("avoid_high_impact_news") == "1"
            payload = self.client_portal.set_news_preference(user_id, avoid_high_impact_news)
            safe_log(
                "client_news_preference_updated",
                user_id=str(user_id),
                avoid_high_impact_news=str(avoid_high_impact_news).lower(),
            )
        if "result_alerts_enabled" in fields:
            result_alerts_enabled = fields.get("result_alerts_enabled") == "1"
            payload = self.client_portal.set_result_alerts(user_id, result_alerts_enabled)
            safe_log(
                "client_result_alerts_updated",
                user_id=str(user_id),
                result_alerts_enabled=str(result_alerts_enabled).lower(),
            )
        if not payload:
            raise ValueError("Nenhuma preferencia informada.")
        self.send_json({"ok": True, **payload})

    def handle_client_push_subscribe(self, fields: dict[str, str]) -> None:
        user_id = self.authenticate_client_mutation(fields)
        payload = self.client_portal.push_subscribe(
            user_id,
            endpoint=fields.get("endpoint", ""),
            p256dh_key=fields.get("p256dh", ""),
            auth_key=fields.get("auth", ""),
            user_agent=fields.get("user_agent") or None,
        )
        safe_log("client_push_subscribed", user_id=str(user_id))
        self.send_json({"ok": True, **payload})

    def handle_client_push_unsubscribe(self, fields: dict[str, str]) -> None:
        user_id = self.authenticate_client_mutation(fields)
        payload = self.client_portal.push_unsubscribe(user_id, endpoint=fields.get("endpoint", ""))
        safe_log("client_push_unsubscribed", user_id=str(user_id))
        self.send_json({"ok": True, **payload})

    def handle_admin_browser_login(self, fields: dict[str, str]) -> None:
        try:
            session = self.admin_browser_auth.consume_login_token(fields.get("token", ""))
        except ValueError as exc:
            raise WebAppValidationError(str(exc)) from exc
        identity = AdminIdentity(session.admin_telegram_user_id, None, role=session.role)
        self.send_admin_dashboard(
            identity,
            extra_headers=(("Set-Cookie", admin_session_cookie(session.session_token)),),
        )

    def handle_admin_password_login(self, fields: dict[str, str]) -> None:
        try:
            session = self.admin_browser_auth.login(
                email=fields.get("email", ""),
                password=fields.get("password", ""),
            )
        except ValueError as exc:
            safe_log("admin_password_login_rejected", reason="credentials")
            self.send_json({"ok": False, "error": str(exc)}, status=401)
            return
        safe_log("admin_password_login_accepted", user_id=str(session.admin_telegram_user_id))
        identity = AdminIdentity(session.admin_telegram_user_id, None, role=session.role)
        self.send_admin_dashboard(
            identity,
            extra_headers=(("Set-Cookie", admin_session_cookie(session.session_token)),),
        )

    def handle_admin_password_setup(self, fields: dict[str, str]) -> None:
        # So quem ja provou ser admin (sessao valida, aberta a partir do link do
        # bot na primeira vez) pode configurar e-mail/senha — nunca auto-cadastro.
        identity = self.authenticate_admin_mutation(fields)
        self.admin_browser_auth.set_password_for_admin(
            identity.telegram_user_id,
            email=fields.get("email", ""),
            password=fields.get("password", ""),
        )
        safe_log("admin_password_configured", user_id=str(identity.telegram_user_id))
        self.send_json({"ok": True})

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
                    "role": identity.role,
                },
            }
        )
        if identity.role == "master":
            payload["admins"] = self.admin_panel.list_admin_roster()
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

    def handle_admin_add_admin(self, fields: dict[str, str]) -> None:
        identity = self.authenticate_admin_mutation(fields)
        if identity.role != "master":
            raise ValueError("Apenas administradores master podem adicionar administradores.")
        try:
            target_telegram_user_id = int(fields.get("telegram_user_id", ""))
        except ValueError as exc:
            raise ValueError("Telegram user id inválido.") from exc
        result = self.admin_panel.add_admin(
            actor_telegram_user_id=identity.telegram_user_id,
            target_telegram_user_id=target_telegram_user_id,
            role=fields.get("role", ""),
            label=fields.get("label") or None,
        )
        safe_log(
            "admin_roster_add",
            admin_id=str(identity.telegram_user_id),
            target_id=str(target_telegram_user_id),
        )
        self.send_json({"ok": True, "admin": result})

    def handle_admin_revoke_admin(self, fields: dict[str, str]) -> None:
        identity = self.authenticate_admin_mutation(fields)
        if identity.role != "master":
            raise ValueError("Apenas administradores master podem remover administradores.")
        try:
            target_telegram_user_id = int(fields.get("telegram_user_id", ""))
        except ValueError as exc:
            raise ValueError("Telegram user id inválido.") from exc
        result = self.admin_panel.revoke_admin(
            actor_telegram_user_id=identity.telegram_user_id,
            target_telegram_user_id=target_telegram_user_id,
        )
        safe_log(
            "admin_roster_revoke",
            admin_id=str(identity.telegram_user_id),
            target_id=str(target_telegram_user_id),
        )
        self.send_json({"ok": True, "admin": result})

    def handle_admin_mt5_account_delete(self, fields: dict[str, str]) -> None:
        identity = self.authenticate_admin_mutation(fields)
        try:
            target_user_id = int(fields.get("user_id", ""))
            account_id = int(fields.get("account_id", ""))
        except ValueError as exc:
            raise ValueError("Cliente ou conta inválidos.") from exc
        result = self.admin_panel.delete_single_mt5_account(
            admin_telegram_user_id=identity.telegram_user_id,
            target_user_id=target_user_id,
            account_id=account_id,
        )
        safe_log(
            "admin_mt5_account_deleted",
            admin_id=str(identity.telegram_user_id),
            target_id=str(target_user_id),
            account_id=str(account_id),
        )
        self.send_json({"ok": True, "result": result})

    def handle_admin_client_delete(self, fields: dict[str, str]) -> None:
        identity = self.authenticate_admin_mutation(fields)
        if identity.role != "master":
            raise ValueError("Apenas administradores master podem excluir um cliente.")
        try:
            target_user_id = int(fields.get("user_id", ""))
        except ValueError as exc:
            raise ValueError("Cliente inválido.") from exc
        result = self.admin_panel.delete_client(
            actor_telegram_user_id=identity.telegram_user_id,
            target_user_id=target_user_id,
        )
        safe_log(
            "admin_client_deleted",
            admin_id=str(identity.telegram_user_id),
            target_id=str(target_user_id),
        )
        self.send_json({"ok": True, "result": result})

    def handle_admin_mt5_account_queue_pilot_status(self, fields: dict[str, str]) -> None:
        identity = self.authenticate_admin_mutation(fields)
        if identity.role != "master":
            raise ValueError("Apenas administradores master podem alterar a fila central.")
        try:
            target_user_id = int(fields.get("user_id", ""))
            account_id = int(fields.get("account_id", ""))
        except ValueError as exc:
            raise ValueError("Cliente ou conta inválidos.") from exc
        enabled = fields.get("enabled") == "1"
        result = self.admin_panel.set_queue_pilot_enabled(
            actor_telegram_user_id=identity.telegram_user_id,
            target_user_id=target_user_id,
            account_id=account_id,
            enabled=enabled,
        )
        safe_log(
            "admin_queue_pilot_status_changed",
            admin_id=str(identity.telegram_user_id),
            target_id=str(target_user_id),
            account_id=str(account_id),
            enabled=str(enabled).lower(),
        )
        self.send_json({"ok": True, "result": result})

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
        if fields.get("exempt", "") in {"1", "true"}:
            result = self.admin_panel.approve_exempt_access(
                admin_telegram_user_id=identity.telegram_user_id,
                target_user_id=target_user_id,
            )
            safe_log(
                "admin_access_approved_exempt",
                admin_id=str(identity.telegram_user_id),
                target_id=str(target_user_id),
            )
            self.send_json({"ok": True, "approval": result})
            return
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

    def handle_admin_channel_status(self, fields: dict[str, str]) -> None:
        identity = self.authenticate_admin_mutation(fields)
        try:
            channel_id = int(fields.get("channel_id", ""))
        except ValueError as exc:
            raise ValueError("Canal inválido.") from exc
        result = self.admin_panel.update_channel_status(
            admin_telegram_user_id=identity.telegram_user_id,
            channel_id=channel_id,
            status=fields.get("status", ""),
        )
        safe_log(
            "admin_channel_status_changed",
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
        role = self.admin_browser_auth.resolve_role(admin_id)
        if role is None:
            raise WebAppValidationError("Administrador não autorizado.")
        return AdminIdentity(admin_id, None, role=role)

    def admin_session_cookie(self) -> str:
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
        except Exception:
            return ""
        morsel = cookie.get("admin_session")
        return morsel.value if morsel is not None else ""

    def authenticate_client(self) -> int:
        return self.client_browser_auth.authenticate_session(self.client_session_cookie())

    def authenticate_client_mutation(self, fields: dict[str, str]) -> int:
        user_id = self.authenticate_client()
        if not self.csrf.validate(fields.get("csrf_token", ""), client_csrf_identity(user_id)):
            raise WebAppValidationError("CSRF invalido.")
        return user_id

    def client_session_cookie(self) -> str:
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
        except Exception:
            return ""
        morsel = cookie.get("client_session")
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
            config.mt5_broker_servers,
        )
        accounts = MT5AccountService(
            config.database_path,
            credential_service=credential_service,
            terminal_manager=terminal_manager,
            allow_live_accounts=config.allow_live_accounts,
            max_accounts_per_vps=config.mt5_max_accounts_per_vps,
            daily_performance_timezone=config.daily_performance_timezone,
        )
        csrf = CSRFTokenService(config.mt5_credential_key)
        broker_options = terminal_manager.available_brokers()
        broker_servers = {
            broker: merge_server_names(
                terminal_manager.available_servers(broker),
                accounts.known_server_names(broker),
            )
            for broker in broker_options
        }
        onboarding = MT5OnboardingService(
            bot_token=config.telegram_bot_token,
            users=users,
            accounts=accounts,
            csrf=csrf,
            require_https=True,
            broker_servers=broker_servers,
        )
        admin_panel = AdminPanelService(
            config.database_path,
            bot_token=config.telegram_bot_token,
            admin_ids=config.bot_admin_ids,
            peer_channel_sync_database_paths=config.peer_channel_sync_database_paths,
            mt5_accounts=accounts,
            terminal_manager=terminal_manager,
        )
        admin_browser_auth = AdminBrowserAuthService(
            config.database_path,
            admin_ids=config.bot_admin_ids,
        )
        client_browser_auth = ClientBrowserAuthService(config.database_path)
        client_portal = ClientPortalService(
            config.database_path,
            brand_name=config.brand_name,
            mt5_accounts=accounts,
            broker_servers=broker_servers,
            market_news_enabled=config.market_news_enabled,
            market_news_minutes_before=config.market_news_minutes_before,
            market_news_minutes_after=config.market_news_minutes_after,
            vapid_public_key=config.vapid_public_key,
        )
        OnboardingHandler.bot_token = config.telegram_bot_token
        OnboardingHandler.broker_options = broker_options
        OnboardingHandler.broker_servers = broker_servers
        OnboardingHandler.brand_name = config.brand_name
        OnboardingHandler.instance_id = config.instance_id
        OnboardingHandler.csrf = csrf
        OnboardingHandler.onboarding = onboarding
        OnboardingHandler.admin_panel = admin_panel
        OnboardingHandler.admin_browser_auth = admin_browser_auth
        OnboardingHandler.client_browser_auth = client_browser_auth
        OnboardingHandler.client_portal = client_portal
        OnboardingHandler.password_reset_rate_limiter = SimpleRateLimiter(limit=3, window_seconds=900)
        OnboardingHandler.client_app_url = config.client_app_url
        if config.resend_api_key and config.resend_from_email:
            OnboardingHandler.email_service = ResendEmailService(
                config.resend_api_key, from_address=config.resend_from_email
            )
        else:
            OnboardingHandler.email_service = NullEmailService()

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


def merge_server_names(*groups: tuple[str, ...]) -> tuple[str, ...]:
    merged: dict[str, str] = {}
    for group in groups:
        for name in group:
            cleaned = name.strip()
            if cleaned:
                merged.setdefault(cleaned.casefold(), cleaned)
    return tuple(sorted(merged.values(), key=str.casefold))


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
        "/api/admin/login",
        "/api/admin/password",
        "/api/admin/logout",
        "/api/admin/user-status",
        "/api/admin/mt5-account-delete",
        "/api/admin/client-delete",
        "/api/admin/billing-update",
        "/api/admin/payment",
        "/api/admin/approve",
        "/api/admin/channel-approve",
        "/api/admin/channel-reject",
        "/api/admin/channel-revalidate",
        "/api/admin/channel-display-name",
        "/api/admin/channel-status",
        "/api/admin/admin-add",
        "/api/admin/admin-revoke",
        "/api/admin/mt5-account-queue-pilot-status",
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


def client_session_cookie(token: str) -> str:
    return (
        f"client_session={token}; Path=/; Max-Age={CLIENT_SESSION_TTL_HOURS * 3600}; "
        "Secure; HttpOnly; SameSite=Strict"
    )


def clear_client_session_cookie() -> str:
    return (
        "client_session=; Path=/; Max-Age=0; "
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
