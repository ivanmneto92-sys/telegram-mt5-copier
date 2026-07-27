from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest
from urllib.parse import urlsplit

from telegram_mt5_copier.admin_auth import AdminBrowserAuthService, token_hash
from telegram_mt5_copier.database import connect_database


class AdminBrowserAuthTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp_dir.name) / "admin-auth.sqlite3"
        self.service = AdminBrowserAuthService(
            self.database_path,
            admin_ids=(9001,),
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_link_tem_token_no_fragmento_e_nao_na_query(self) -> None:
        url = self.service.create_login_url(
            9001,
            "https://institutotrader.online/admin?v=6",
        )
        parsed = urlsplit(url)

        self.assertEqual(parsed.query, "v=6")
        self.assertTrue(parsed.fragment.startswith("token="))
        self.assertNotIn("token=", parsed.query)
        raw_token = parsed.fragment.removeprefix("token=")
        with connect_database(self.database_path) as connection:
            stored = connection.execute(
                "SELECT token_hash FROM admin_login_tokens"
            ).fetchone()[0]
        self.assertEqual(stored, token_hash(raw_token))
        self.assertNotEqual(stored, raw_token)

    def test_link_e_de_uso_unico_e_cria_sessao(self) -> None:
        url = self.service.create_login_url(9001, "https://example.com/admin")
        raw_token = urlsplit(url).fragment.removeprefix("token=")

        session = self.service.consume_login_token(raw_token)

        self.assertEqual(self.service.authenticate_session(session.session_token), 9001)
        with self.assertRaises(ValueError):
            self.service.consume_login_token(raw_token)

    def test_sessao_revogada_nao_autentica(self) -> None:
        url = self.service.create_login_url(9001, "https://example.com/admin")
        session = self.service.consume_login_token(
            urlsplit(url).fragment.removeprefix("token=")
        )

        self.service.revoke_session(session.session_token)

        with self.assertRaises(ValueError):
            self.service.authenticate_session(session.session_token)

    def test_link_expirado_e_rejeitado(self) -> None:
        url = self.service.create_login_url(9001, "https://example.com/admin")
        raw_token = urlsplit(url).fragment.removeprefix("token=")
        expired = (datetime.now(tz=timezone.utc) - timedelta(seconds=1)).isoformat()
        with connect_database(self.database_path) as connection:
            connection.execute(
                "UPDATE admin_login_tokens SET expires_at = ?",
                (expired,),
            )

        with self.assertRaises(ValueError):
            self.service.consume_login_token(raw_token)

    def test_nao_admin_nao_pode_gerar_link(self) -> None:
        with self.assertRaises(ValueError):
            self.service.create_login_url(101, "https://example.com/admin")


if __name__ == "__main__":
    unittest.main()
