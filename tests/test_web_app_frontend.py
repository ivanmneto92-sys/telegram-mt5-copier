from __future__ import annotations

from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import re
import threading
import tempfile
import time
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
import unittest

from telegram_mt5_copier.web_app import (
    EMPTY_INIT_DATA_MESSAGE,
    OUTSIDE_TELEGRAM_MESSAGE,
    VALIDATION_FAILED_MESSAGE,
    CSRFTokenService,
    build_signed_init_data,
    render_miniapp_script,
    render_onboarding_form,
)
from telegram_mt5_copier.admin_panel import AdminPanelService, render_admin_panel, render_admin_script
from telegram_mt5_copier.admin_auth import AdminBrowserAuthService
from telegram_mt5_copier.users import UserRepository
from telegram_mt5_copier.web_server import OnboardingHandler, safe_reason


class MiniAppFrontendTests(unittest.TestCase):
    def test_motivo_de_rejeicao_do_init_data_e_diagnosticavel_sem_expor_dados(self) -> None:
        cases = {
            "initData ja utilizado.": "init_data_replayed",
            "Sessao do Web App expirada.": "init_data_expired",
            "auth_date invalido.": "init_data_clock",
            "Hash do initData ausente.": "init_data_signature",
            "initData invalido.": "init_data_signature",
            "Usuario do Web App invalido.": "init_data_user",
            "Token do bot indisponivel para validacao.": "bot_token",
            "initData ausente.": "init_data_missing",
        }

        for error, expected in cases.items():
            with self.subTest(error=error):
                self.assertEqual(safe_reason(error), expected)

    def test_carrega_script_oficial_antes_do_javascript_da_aplicacao(self) -> None:
        html = render_onboarding_form()

        telegram_script_index = html.index('src="https://telegram.org/js/telegram-web-app.js"')
        app_script_index = html.index('src="/miniapp.js"')

        self.assertLess(telegram_script_index, app_script_index)

    def test_nao_existe_loading_infinito(self) -> None:
        html = render_onboarding_form().lower()

        self.assertNotIn("loading", html)
        self.assertNotIn("carregamento infinito", html)

    def test_formulario_ou_mensagem_aparece_sem_telegram(self) -> None:
        html = render_onboarding_form()

        self.assertIn('id="connect-form"', html)
        self.assertIn(OUTSIDE_TELEGRAM_MESSAGE, html)

    def test_formulario_lista_apenas_corretoras_configuradas(self) -> None:
        html = render_onboarding_form(
            broker_options=("HFM", "FTMO", "FXGlobe", "Exness", "INFINOX")
        )

        self.assertIn('<select name="broker_name" required>', html)
        self.assertIn('<option value="FTMO">FTMO</option>', html)
        self.assertIn('<option value="INFINOX">INFINOX</option>', html)
        self.assertNotIn('<input name="broker_name"', html)

    def test_formulario_lista_servidores_de_acordo_com_a_corretora(self) -> None:
        html = render_onboarding_form(
            broker_options=("HFM", "FTMO"),
            broker_servers={
                "HFM": ("HFMarketsGlobal-Live1", "HFMarketsGlobal-Live3"),
                "FTMO": ("FTMO-Demo",),
            },
        )
        script = render_miniapp_script()

        self.assertIn('<select name="server_name" required disabled>', html)
        self.assertNotIn('<input name="server_name"', html)
        self.assertIn("HFMarketsGlobal-Live3", html)
        self.assertIn("FTMO-Demo", html)
        self.assertIn('name="custom_server_name"', html)
        self.assertIn("Meu servidor não está na lista — digitar", script)
        self.assertIn('customOption.value = "__custom__"', script)
        self.assertIn('brokerInput.addEventListener("change", updateServerOptions)', script)

    def test_identidade_white_label_aparece_sem_injetar_html(self) -> None:
        onboarding = render_onboarding_form(brand_name="Mesa <Alpha>")
        admin = render_admin_panel(brand_name="Mesa <Alpha>")

        self.assertIn("Mesa &lt;Alpha&gt;", onboarding)
        self.assertIn("Mesa &lt;Alpha&gt; · Master", admin)
        self.assertNotIn("Mesa <Alpha>", onboarding + admin)

    def test_init_data_vazio_gera_mensagem(self) -> None:
        script = render_miniapp_script()

        self.assertIn(EMPTY_INIT_DATA_MESSAGE, script)

    def test_init_data_invalido_gera_mensagem_generica(self) -> None:
        with mini_app_server() as base_url:
            response = post_expect_error(
                f"{base_url}/api/csrf",
                {"init_data": "auth_date=1000&hash=bad"},
            )

        self.assertEqual(response["status"], 403)
        self.assertEqual(response["body"]["error"], VALIDATION_FAILED_MESSAGE)

    def test_init_data_valido_retorna_csrf_para_abrir_formulario(self) -> None:
        token = "123456:bot-token"
        now = int(time.time())
        init_data = build_signed_init_data(
            token,
            {
                "query_id": "abc",
                "auth_date": str(now),
                "user": json.dumps({"id": 101, "username": "alice"}, separators=(",", ":")),
            },
        )
        with mini_app_server(bot_token=token) as base_url:
            response = post_json(f"{base_url}/api/csrf", {"init_data": init_data})

        self.assertTrue(response["ok"])
        self.assertIn("csrf_token", response)

    def test_health_retorna_200(self) -> None:
        with mini_app_server() as base_url:
            with urlopen(f"{base_url}/health", timeout=5) as response:
                body = json.loads(response.read().decode("utf-8"))
                headers = response.headers

        self.assertEqual(
            body,
            {"status": "ok", "instance": "main", "brand": "Instituto Trader"},
        )
        self.assertIn("application/json", headers.get("Content-Type", ""))
        self.assertEqual(headers.get("Cache-Control"), "no-store")

    def test_fetch_nao_aponta_para_localhost(self) -> None:
        html = render_onboarding_form()
        script = render_miniapp_script()

        self.assertIn('src="/miniapp.js"', html)
        self.assertIn('fetch(path, {', script)
        self.assertIn('postApi("/api/csrf"', script)
        self.assertIn('postApi("/api/connect"', script)
        self.assertIn('postApi("/api/log"', script)
        self.assertNotIn('fetch("http', html)
        self.assertNotIn('fetch("http', script)
        self.assertNotIn("localhost", html + script)
        self.assertNotIn("127.0.0.1", html + script)

    def test_headers_html_sao_compativeis_com_telegram(self) -> None:
        with mini_app_server() as base_url:
            with urlopen(f"{base_url}/", timeout=5) as response:
                headers = response.headers
                html = response.read().decode("utf-8")

        self.assertIn("text/html", headers.get("Content-Type", ""))
        self.assertEqual(headers.get("Cache-Control"), "no-store")
        self.assertIsNone(headers.get("X-Frame-Options"))
        self.assertIn("https://telegram.org", headers.get("Content-Security-Policy", ""))
        self.assertNotIn("frame-ancestors", headers.get("Content-Security-Policy", ""))
        self.assertIn("Conectar conta MT5", html)
        self.assertIn("web_app_ready", html)

    def test_nonce_do_html_igual_ao_nonce_da_csp(self) -> None:
        with mini_app_server() as base_url:
            with urlopen(f"{base_url}/", timeout=5) as response:
                csp = response.headers.get("Content-Security-Policy", "")
                html = response.read().decode("utf-8")

        html_nonce = extract_inline_script_nonce(html)
        csp_nonce = extract_csp_nonce(csp)

        self.assertTrue(html_nonce)
        self.assertEqual(html_nonce, csp_nonce)

    def test_nonces_diferentes_por_requisicao(self) -> None:
        with mini_app_server() as base_url:
            with urlopen(f"{base_url}/", timeout=5) as first_response:
                first_nonce = extract_inline_script_nonce(first_response.read().decode("utf-8"))
            with urlopen(f"{base_url}/", timeout=5) as second_response:
                second_nonce = extract_inline_script_nonce(second_response.read().decode("utf-8"))

        self.assertNotEqual(first_nonce, second_nonce)

    def test_script_inline_autorizado_por_nonce(self) -> None:
        with mini_app_server() as base_url:
            with urlopen(f"{base_url}/", timeout=5) as response:
                csp = response.headers.get("Content-Security-Policy", "")
                html = response.read().decode("utf-8")

        nonce = extract_inline_script_nonce(html)

        self.assertIn(f"'nonce-{nonce}'", script_src_directive(csp))
        self.assertNotIn("unsafe-inline", script_src_directive(csp))

    def test_csp_sem_unsafe_inline_para_scripts(self) -> None:
        with mini_app_server() as base_url:
            with urlopen(f"{base_url}/", timeout=5) as response:
                csp = response.headers.get("Content-Security-Policy", "")

        self.assertNotIn("unsafe-inline", script_src_directive(csp))

    def test_query_string_na_raiz_retorna_200(self) -> None:
        with mini_app_server() as base_url:
            with urlopen(f"{base_url}/?v=2", timeout=5) as response:
                html = response.read().decode("utf-8")

        self.assertEqual(response.status, 200)
        self.assertIn(OUTSIDE_TELEGRAM_MESSAGE, html)

    def test_query_string_no_health_retorna_200(self) -> None:
        with mini_app_server() as base_url:
            with urlopen(f"{base_url}/health?x=1", timeout=5) as response:
                body = json.loads(response.read().decode("utf-8"))

        self.assertEqual(response.status, 200)
        self.assertEqual(
            body,
            {"status": "ok", "instance": "main", "brand": "Instituto Trader"},
        )

    def test_favicon_retorna_204(self) -> None:
        with mini_app_server() as base_url:
            with urlopen(f"{base_url}/favicon.ico", timeout=5) as response:
                body = response.read()

        self.assertEqual(response.status, 204)
        self.assertEqual(body, b"")

    def test_head_funciona_sem_corpo(self) -> None:
        with mini_app_server() as base_url:
            root = head(f"{base_url}/?v=2")
            health = head(f"{base_url}/health?x=1")
            favicon = head(f"{base_url}/favicon.ico")

        self.assertEqual(root["status"], 200)
        self.assertEqual(root["body"], b"")
        self.assertIn("text/html", root["content_type"])
        self.assertEqual(health["status"], 200)
        self.assertEqual(health["body"], b"")
        self.assertIn("application/json", health["content_type"])
        self.assertEqual(favicon["status"], 204)
        self.assertEqual(favicon["body"], b"")

    def test_miniapp_js_retorna_javascript(self) -> None:
        with mini_app_server() as base_url:
            with urlopen(f"{base_url}/miniapp.js?v=2", timeout=5) as response:
                body = response.read().decode("utf-8")
                headers = response.headers

        self.assertEqual(response.status, 200)
        self.assertIn("application/javascript", headers.get("Content-Type", ""))
        self.assertEqual(headers.get("Cache-Control"), "no-store")
        self.assertIn("tg.ready();", body)
        self.assertIn("TelegramWebviewProxy", body)
        self.assertIn("web_app_ready", body)
        self.assertIn('postApi("/api/csrf"', body)

    def test_painel_admin_e_script_sao_servidos(self) -> None:
        with mini_app_server() as base_url:
            with urlopen(f"{base_url}/admin?v=4", timeout=5) as response:
                html = response.read().decode("utf-8")
            with urlopen(f"{base_url}/admin.js?v=4", timeout=5) as response:
                script = response.read().decode("utf-8")

        self.assertIn("Central de clientes", html)
        self.assertIn('src="/admin.js"', html)
        self.assertIn("/api/admin/session", script)
        self.assertEqual(render_admin_panel("nonce").count('nonce="nonce"'), 1)
        self.assertIn("Abra o Painel Admin pelo botão", render_admin_script())

    def test_api_admin_valida_allowlist_do_telegram(self) -> None:
        token = "123456:bot-token"
        now = int(time.time())
        admin_init_data = build_signed_init_data(
            token,
            {
                "query_id": "admin",
                "auth_date": str(now),
                "user": json.dumps({"id": 9001, "username": "master"}, separators=(",", ":")),
            },
        )
        client_init_data = build_signed_init_data(
            token,
            {
                "query_id": "client",
                "auth_date": str(now),
                "user": json.dumps({"id": 101, "username": "alice"}, separators=(",", ":")),
            },
        )
        with mini_app_server(bot_token=token, admin_ids=(9001,)) as base_url:
            admin = post_json(f"{base_url}/api/admin/session", {"init_data": admin_init_data})
            client = post_expect_error(
                f"{base_url}/api/admin/session",
                {"init_data": client_init_data},
            )

        self.assertTrue(admin["ok"])
        self.assertEqual(admin["admin"]["telegram_user_id"], 9001)
        self.assertIn("csrf_token", admin)
        self.assertEqual(client["status"], 403)
        self.assertEqual(client["body"]["error"], "Acesso administrativo não autorizado.")

    def test_link_temporario_cria_sessao_de_navegador_com_cookie_seguro(self) -> None:
        with mini_app_server(admin_ids=(9001,)) as base_url:
            users = UserRepository(OnboardingHandler.admin_panel.database_path)
            try:
                customer = users.get_or_create_user(101, "alice")
            finally:
                users.close()
            login_url = OnboardingHandler.admin_browser_auth.create_login_url(
                9001,
                "https://institutotrader.online/admin",
            )
            token = login_url.split("#token=", 1)[1]
            request = Request(
                f"{base_url}/api/admin/browser-login",
                data=urlencode({"token": token}).encode("utf-8"),
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                method="POST",
            )
            with urlopen(request, timeout=5) as response:
                login_payload = json.loads(response.read().decode("utf-8"))
                set_cookie = response.headers.get("Set-Cookie", "")
            cookie = set_cookie.split(";", 1)[0]
            session_request = Request(
                f"{base_url}/api/admin/session",
                data=b"",
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Cookie": cookie,
                },
                method="POST",
            )
            with urlopen(session_request, timeout=5) as response:
                session_payload = json.loads(response.read().decode("utf-8"))
            billing_request = Request(
                f"{base_url}/api/admin/billing-update",
                data=urlencode(
                    {
                        "csrf_token": session_payload["csrf_token"],
                        "user_id": str(customer.id),
                        "customer_name": "Alice",
                        "email": "alice@example.com",
                        "phone": "11999999999",
                        "plan_name": "Gold",
                        "monthly_amount": "149.90",
                        "due_date": "2026-08-10",
                        "billing_status": "pending",
                        "notes": "",
                    }
                ).encode("utf-8"),
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Cookie": cookie,
                },
                method="POST",
            )
            with urlopen(billing_request, timeout=5) as response:
                billing_payload = json.loads(response.read().decode("utf-8"))
            approval_request = Request(
                f"{base_url}/api/admin/approve",
                data=urlencode(
                    {
                        "csrf_token": session_payload["csrf_token"],
                        "user_id": str(customer.id),
                        "amount": "149.90",
                        "paid_at": "2026-07-27",
                        "method": "PIX",
                        "reference": "PAG-1",
                        "expires_on": "2026-08-27",
                    }
                ).encode("utf-8"),
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Cookie": cookie,
                },
                method="POST",
            )
            with urlopen(approval_request, timeout=5) as response:
                approval_payload = json.loads(response.read().decode("utf-8"))

        self.assertTrue(login_payload["ok"])
        self.assertTrue(session_payload["ok"])
        self.assertIn("HttpOnly", set_cookie)
        self.assertIn("Secure", set_cookie)
        self.assertIn("SameSite=Strict", set_cookie)
        self.assertEqual(session_payload["admin"]["telegram_user_id"], 9001)
        self.assertTrue(billing_payload["ok"])
        self.assertEqual(billing_payload["billing"]["monthly_amount"], "149.90")
        self.assertTrue(approval_payload["ok"])
        self.assertEqual(approval_payload["approval"]["status"], "active")

    def test_navegador_comum_mostra_mensagem_clara(self) -> None:
        html = render_onboarding_form("test-nonce")

        self.assertIn(f'<div id="message" class="message error" role="alert">{OUTSIDE_TELEGRAM_MESSAGE}</div>', html)
        self.assertIn('id="connect-form"', html)
        self.assertIn("form { display: block; }", html)

    def test_telegram_valido_mostra_formulario(self) -> None:
        html = render_onboarding_form("test-nonce")
        script = render_miniapp_script()

        self.assertIn("var initData = tg.initData || \"\";", script)
        self.assertIn("if (!initData)", script)
        self.assertIn('postApi("/api/csrf"', script)
        self.assertIn("setSubmitEnabled(true);", script)
        self.assertIn("notifyTelegramReady();", script)
        self.assertIn('id="connect-form"', html)


class mini_app_server:
    def __init__(
        self,
        bot_token: str = "123456:bot-token",
        admin_ids: tuple[int, ...] = (),
    ) -> None:
        self.bot_token = bot_token
        self.admin_ids = admin_ids
        self.server: ThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None
        self.temp_dir: tempfile.TemporaryDirectory[str] | None = None

    def __enter__(self) -> str:
        OnboardingHandler.bot_token = self.bot_token
        OnboardingHandler.csrf = CSRFTokenService("csrf-secret")
        self.temp_dir = tempfile.TemporaryDirectory()
        database_path = Path(self.temp_dir.name) / "web.sqlite3"
        OnboardingHandler.admin_panel = AdminPanelService(
            database_path,
            bot_token=self.bot_token,
            admin_ids=self.admin_ids,
        )
        OnboardingHandler.admin_browser_auth = AdminBrowserAuthService(
            database_path,
            admin_ids=self.admin_ids,
        )
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), OnboardingHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        host, port = self.server.server_address
        return f"http://{host}:{port}"

    def __exit__(self, _exc_type: object, _exc: object, _traceback: object) -> None:
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
        if self.thread is not None:
            self.thread.join(timeout=5)
        if self.temp_dir is not None:
            self.temp_dir.cleanup()


def post_json(url: str, fields: dict[str, str]) -> dict[str, object]:
    request = Request(
        url,
        data=urlencode(fields).encode("utf-8"),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urlopen(request, timeout=5) as response:
        return json.loads(response.read().decode("utf-8"))


def post_expect_error(url: str, fields: dict[str, str]) -> dict[str, object]:
    try:
        return {"status": 200, "body": post_json(url, fields)}
    except HTTPError as exc:
        return {
            "status": exc.code,
            "body": json.loads(exc.read().decode("utf-8")),
        }


def head(url: str) -> dict[str, object]:
    request = Request(url, method="HEAD")
    with urlopen(request, timeout=5) as response:
        return {
            "status": response.status,
            "body": response.read(),
            "content_type": response.headers.get("Content-Type", ""),
        }


def extract_inline_script_nonce(html: str) -> str:
    match = re.search(r"<script nonce=\"([^\"]+)\">\s+\(function \(\)", html)
    if match is None:
        raise AssertionError("Nonce do script inline nao encontrado.")
    return match.group(1)


def extract_csp_nonce(csp: str) -> str:
    match = re.search(r"'nonce-([^']+)'", csp)
    if match is None:
        raise AssertionError("Nonce da CSP nao encontrado.")
    return match.group(1)


def script_src_directive(csp: str) -> str:
    for directive in csp.split(";"):
        stripped = directive.strip()
        if stripped.startswith("script-src "):
            return stripped
    raise AssertionError("Diretiva script-src nao encontrada.")


if __name__ == "__main__":
    unittest.main()
