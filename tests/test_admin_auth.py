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


class AdminPasswordLoginTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp_dir.name) / "admin-auth.sqlite3"
        self.service = AdminBrowserAuthService(
            self.database_path,
            admin_ids=(9001,),
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_admin_configura_senha_e_faz_login(self) -> None:
        self.service.set_password_for_admin(
            9001, email="admin@example.com", password="SenhaAdmin123"
        )

        session = self.service.login(email="admin@example.com", password="SenhaAdmin123")

        self.assertEqual(9001, session.admin_telegram_user_id)
        self.assertEqual(
            9001, self.service.authenticate_session(session.session_token)
        )

    def test_nao_admin_nao_pode_configurar_senha(self) -> None:
        with self.assertRaises(ValueError):
            self.service.set_password_for_admin(
                101, email="intruso@example.com", password="SenhaAdmin123"
            )

    def test_login_com_senha_errada_e_rejeitado(self) -> None:
        self.service.set_password_for_admin(
            9001, email="admin@example.com", password="SenhaAdmin123"
        )

        with self.assertRaisesRegex(ValueError, "E-mail ou senha"):
            self.service.login(email="admin@example.com", password="SenhaErrada123")

    def test_email_desconhecido_e_rejeitado_sem_revelar_existencia(self) -> None:
        with self.assertRaisesRegex(ValueError, "E-mail ou senha"):
            self.service.login(email="naoexiste@example.com", password="Qualquer123")

    def test_bloqueio_apos_varias_tentativas(self) -> None:
        self.service.set_password_for_admin(
            9001, email="admin@example.com", password="SenhaAdmin123"
        )
        for _ in range(5):
            with self.assertRaises(ValueError):
                self.service.login(email="admin@example.com", password="SenhaErrada123")

        with self.assertRaisesRegex(ValueError, "Muitas tentativas"):
            self.service.login(email="admin@example.com", password="SenhaAdmin123")

    def test_login_falha_se_telegram_id_sair_do_allowlist_mesmo_com_senha_certa(self) -> None:
        """Revogar em BOT_ADMIN_IDS revoga o acesso mesmo que a senha continue certa —
        a config do .env e quem manda, a tabela admin_credentials nunca sozinha."""
        self.service.set_password_for_admin(
            9001, email="admin@example.com", password="SenhaAdmin123"
        )
        service_sem_esse_admin = AdminBrowserAuthService(
            self.database_path,
            admin_ids=(),
        )

        with self.assertRaisesRegex(ValueError, "E-mail ou senha"):
            service_sem_esse_admin.login(email="admin@example.com", password="SenhaAdmin123")

    def test_email_ja_usado_por_outro_admin_e_rejeitado(self) -> None:
        service = AdminBrowserAuthService(self.database_path, admin_ids=(9001, 9002))
        service.set_password_for_admin(9001, email="admin@example.com", password="SenhaAdmin123")

        with self.assertRaisesRegex(ValueError, "já está em uso"):
            service.set_password_for_admin(9002, email="admin@example.com", password="OutraSenha123")


if __name__ == "__main__":
    unittest.main()
