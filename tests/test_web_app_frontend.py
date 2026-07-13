from __future__ import annotations

from http.server import ThreadingHTTPServer
import json
import threading
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
    render_onboarding_form,
)
from telegram_mt5_copier.web_server import OnboardingHandler


class MiniAppFrontendTests(unittest.TestCase):
    def test_carrega_script_oficial_antes_do_javascript_da_aplicacao(self) -> None:
        html = render_onboarding_form()

        telegram_script_index = html.index('src="https://telegram.org/js/telegram-web-app.js"')
        app_script_index = html.index("(function ()")

        self.assertLess(telegram_script_index, app_script_index)

    def test_nao_existe_loading_infinito(self) -> None:
        html = render_onboarding_form().lower()

        self.assertNotIn("loading", html)
        self.assertNotIn("carregamento infinito", html)

    def test_formulario_ou_mensagem_aparece_sem_telegram(self) -> None:
        html = render_onboarding_form()

        self.assertIn('id="connect-form"', html)
        self.assertIn(OUTSIDE_TELEGRAM_MESSAGE, html)

    def test_init_data_vazio_gera_mensagem(self) -> None:
        html = render_onboarding_form()

        self.assertIn(EMPTY_INIT_DATA_MESSAGE, html)

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

        self.assertEqual(body, {"status": "ok"})
        self.assertIn("application/json", headers.get("Content-Type", ""))
        self.assertEqual(headers.get("Cache-Control"), "no-store")

    def test_fetch_nao_aponta_para_localhost(self) -> None:
        html = render_onboarding_form()

        self.assertIn('fetch(path, {', html)
        self.assertIn('postApi("/api/csrf"', html)
        self.assertIn('postApi("/api/connect"', html)
        self.assertIn('postApi("/api/log"', html)
        self.assertNotIn('fetch("http', html)
        self.assertNotIn("localhost", html)
        self.assertNotIn("127.0.0.1", html)

    def test_headers_html_sao_compativeis_com_telegram(self) -> None:
        with mini_app_server() as base_url:
            with urlopen(f"{base_url}/", timeout=5) as response:
                headers = response.headers
                html = response.read().decode("utf-8")

        self.assertIn("text/html", headers.get("Content-Type", ""))
        self.assertEqual(headers.get("Cache-Control"), "no-store")
        self.assertIsNone(headers.get("X-Frame-Options"))
        self.assertIn("https://telegram.org", headers.get("Content-Security-Policy", ""))
        self.assertIn("Conectar conta MT5", html)


class mini_app_server:
    def __init__(self, bot_token: str = "123456:bot-token") -> None:
        self.bot_token = bot_token
        self.server: ThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None

    def __enter__(self) -> str:
        OnboardingHandler.bot_token = self.bot_token
        OnboardingHandler.csrf = CSRFTokenService("csrf-secret")
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


if __name__ == "__main__":
    unittest.main()
