from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from urllib.parse import parse_qs, urlsplit

from telegram_mt5_copier.client_auth import ClientBrowserAuthService
from telegram_mt5_copier.database import connect_database, initialize_database, utc_now


APP_URL = "https://app.example.com/"


class PasswordResetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = TemporaryDirectory()
        self.database_path = Path(self.temp.name) / "auth.sqlite3"
        initialize_database(self.database_path)
        self.auth = ClientBrowserAuthService(self.database_path)
        self.session = self.auth.register(
            customer_name="Cliente Teste",
            email="cliente@example.com",
            phone="11999990000",
            password="SenhaAntiga123",
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _extract_token(self, url: str, param: str) -> str:
        query = parse_qs(urlsplit(url).fragment)
        return query[param][0]

    def test_reset_flow_changes_password_and_allows_new_login(self) -> None:
        url = self.auth.request_password_reset("CLIENTE@example.com", APP_URL)
        self.assertIsNotNone(url)
        token = self._extract_token(url, "reset_token")

        affected_user_id = self.auth.reset_password(token, "SenhaNova123")
        self.assertEqual(self.session.user_id, affected_user_id)

        with self.assertRaisesRegex(ValueError, "E-mail ou senha"):
            self.auth.login(email="cliente@example.com", password="SenhaAntiga123")
        session = self.auth.login(email="cliente@example.com", password="SenhaNova123")
        self.assertEqual(self.session.user_id, session.user_id)

    def test_reset_token_is_single_use(self) -> None:
        url = self.auth.request_password_reset("cliente@example.com", APP_URL)
        token = self._extract_token(url, "reset_token")

        self.auth.reset_password(token, "SenhaNova123")

        with self.assertRaisesRegex(ValueError, "expirado"):
            self.auth.reset_password(token, "OutraSenha123")

    def test_reset_token_expires(self) -> None:
        url = self.auth.request_password_reset("cliente@example.com", APP_URL)
        token = self._extract_token(url, "reset_token")
        with connect_database(self.database_path) as db:
            db.execute(
                "UPDATE client_password_reset_tokens SET expires_at = ?",
                ("2000-01-01T00:00:00+00:00",),
            )

        with self.assertRaisesRegex(ValueError, "expirado"):
            self.auth.reset_password(token, "SenhaNova123")

    def test_reset_password_revokes_existing_sessions(self) -> None:
        other_session = self.auth.login(email="cliente@example.com", password="SenhaAntiga123")
        self.assertEqual(
            other_session.user_id,
            self.auth.authenticate_session(other_session.session_token),
        )
        url = self.auth.request_password_reset("cliente@example.com", APP_URL)
        token = self._extract_token(url, "reset_token")

        self.auth.reset_password(token, "SenhaNova123")

        with self.assertRaises(ValueError):
            self.auth.authenticate_session(other_session.session_token)

    def test_unknown_email_returns_none_without_revealing_existence(self) -> None:
        url = self.auth.request_password_reset("naoexiste@example.com", APP_URL)
        self.assertIsNone(url)

    def test_reset_token_from_another_hash_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "expirado"):
            self.auth.reset_password("token-forjado-que-nao-existe", "SenhaNova123")

    def test_request_password_reset_requires_https_app_url(self) -> None:
        with self.assertRaisesRegex(ValueError, "HTTPS"):
            self.auth.request_password_reset("cliente@example.com", "http://app.example.com/")


class EmailConfirmationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = TemporaryDirectory()
        self.database_path = Path(self.temp.name) / "auth.sqlite3"
        initialize_database(self.database_path)
        self.auth = ClientBrowserAuthService(self.database_path)
        self.session = self.auth.register(
            customer_name="Cliente Teste",
            email="cliente@example.com",
            phone="11999990000",
            password="SenhaAntiga123",
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _extract_token(self, url: str, param: str) -> str:
        query = parse_qs(urlsplit(url).fragment)
        return query[param][0]

    def test_new_registration_starts_unconfirmed(self) -> None:
        with connect_database(self.database_path) as db:
            row = db.execute(
                "SELECT email_confirmed_at FROM client_credentials WHERE user_id = ?",
                (self.session.user_id,),
            ).fetchone()
        self.assertIsNone(row[0])

    def test_confirm_flow_marks_email_confirmed(self) -> None:
        url = self.auth.request_email_confirmation(self.session.user_id, APP_URL)
        token = self._extract_token(url, "confirm_token")

        self.auth.confirm_email(token)

        with connect_database(self.database_path) as db:
            row = db.execute(
                "SELECT email_confirmed_at FROM client_credentials WHERE user_id = ?",
                (self.session.user_id,),
            ).fetchone()
        self.assertIsNotNone(row[0])

    def test_confirm_token_is_single_use(self) -> None:
        url = self.auth.request_email_confirmation(self.session.user_id, APP_URL)
        token = self._extract_token(url, "confirm_token")
        self.auth.confirm_email(token)

        with self.assertRaisesRegex(ValueError, "expirado"):
            self.auth.confirm_email(token)

    def test_confirm_token_expires(self) -> None:
        url = self.auth.request_email_confirmation(self.session.user_id, APP_URL)
        token = self._extract_token(url, "confirm_token")
        with connect_database(self.database_path) as db:
            db.execute(
                "UPDATE client_email_confirmation_tokens SET expires_at = ?",
                ("2000-01-01T00:00:00+00:00",),
            )

        with self.assertRaisesRegex(ValueError, "expirado"):
            self.auth.confirm_email(token)

    def test_stale_token_does_not_confirm_a_changed_email(self) -> None:
        url = self.auth.request_email_confirmation(self.session.user_id, APP_URL)
        token = self._extract_token(url, "confirm_token")
        now = utc_now()
        with connect_database(self.database_path) as db:
            db.execute(
                "UPDATE client_credentials SET email = ?, updated_at = ? WHERE user_id = ?",
                ("outroemail@example.com", now, self.session.user_id),
            )

        self.auth.confirm_email(token)

        with connect_database(self.database_path) as db:
            row = db.execute(
                "SELECT email_confirmed_at FROM client_credentials WHERE user_id = ?",
                (self.session.user_id,),
            ).fetchone()
        self.assertIsNone(row[0])


if __name__ == "__main__":
    unittest.main()
