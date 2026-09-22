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


class TelegramClientMigrationTests(unittest.TestCase):
    """Cliente ja cadastrado pelo Telegram ganha acesso web na MESMA conta,
    sem criar cliente duplicado nem permitir que outra pessoa reivindique o
    e-mail de alguem mais -- o fluxo usado pra migrar os clientes existentes.
    """

    def setUp(self) -> None:
        self.temp = TemporaryDirectory()
        self.database_path = Path(self.temp.name) / "auth.sqlite3"
        initialize_database(self.database_path)
        self.auth = ClientBrowserAuthService(self.database_path)
        now = utc_now()
        with connect_database(self.database_path) as db:
            self.telegram_user_id = int(
                db.execute(
                    """
                    INSERT INTO users (
                        telegram_user_id, telegram_username, status, created_at, updated_at
                    ) VALUES (?, ?, 'active', ?, ?)
                    """,
                    (555111, "cliente_antigo", now, now),
                ).lastrowid
            )
            self.mt5_account_id = int(
                db.execute(
                    """
                    INSERT INTO mt5_accounts (
                        user_id, account_alias, broker_name, terminal_path, server_name,
                        login, encrypted_password, account_type, account_mode,
                        connection_status, created_at, updated_at
                    ) VALUES (?, 'Conta antiga', 'HFM', 'terminal64.exe', 'HFM-Live',
                              '999999', 'encrypted', 'real', 'hedging', 'connected', ?, ?)
                    """,
                    (self.telegram_user_id, now, now),
                ).lastrowid
            )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _extract_token(self, url: str) -> str:
        return parse_qs(urlsplit(url).fragment)["token"][0]

    def test_cliente_telegram_vincula_email_senha_na_mesma_conta(self) -> None:
        url = self.auth.create_login_url(self.telegram_user_id, APP_URL)
        session = self.auth.consume_login_token(self._extract_token(url))
        self.assertEqual(self.telegram_user_id, session.user_id)
        self.assertEqual(
            self.telegram_user_id, self.auth.authenticate_session(session.session_token)
        )

        self.auth.set_password_for_user(
            self.telegram_user_id,
            email="cliente.antigo@example.com",
            password="SenhaMigrada123",
        )

        login = self.auth.login(email="cliente.antigo@example.com", password="SenhaMigrada123")
        self.assertEqual(self.telegram_user_id, login.user_id)

        with connect_database(self.database_path) as db:
            self.assertEqual(1, db.execute("SELECT COUNT(*) FROM users").fetchone()[0])
            self.assertEqual(
                self.telegram_user_id,
                db.execute(
                    "SELECT user_id FROM client_credentials WHERE email = 'cliente.antigo@example.com'"
                ).fetchone()[0],
            )
            self.assertEqual(
                self.mt5_account_id,
                db.execute(
                    "SELECT id FROM mt5_accounts WHERE user_id = ?", (self.telegram_user_id,)
                ).fetchone()[0],
            )

    def test_login_token_e_uso_unico(self) -> None:
        url = self.auth.create_login_url(self.telegram_user_id, APP_URL)
        token = self._extract_token(url)
        self.auth.consume_login_token(token)

        with self.assertRaises(ValueError):
            self.auth.consume_login_token(token)

    def test_ninguem_consegue_reivindicar_email_de_outro_cliente(self) -> None:
        self.auth.set_password_for_user(
            self.telegram_user_id, email="original@example.com", password="SenhaOriginal123"
        )
        now = utc_now()
        with connect_database(self.database_path) as db:
            other_user_id = int(
                db.execute(
                    """
                    INSERT INTO users (
                        telegram_user_id, telegram_username, status, created_at, updated_at
                    ) VALUES (?, NULL, 'active', ?, ?)
                    """,
                    (555222, now, now),
                ).lastrowid
            )

        with self.assertRaisesRegex(ValueError, "já está em uso"):
            self.auth.set_password_for_user(
                other_user_id, email="ORIGINAL@example.com", password="TentativaInvasao1"
            )

        # A conta original continua intacta -- a tentativa nao mexeu na senha dela.
        login = self.auth.login(email="original@example.com", password="SenhaOriginal123")
        self.assertEqual(self.telegram_user_id, login.user_id)

    def test_cadastro_publico_nao_duplica_cliente_ja_registrado_pelo_telegram(self) -> None:
        # Cliente ja tem cadastro/financeiro feito pelo bot, mesmo sem senha
        # web configurada ainda -- o cadastro publico nao pode criar uma
        # segunda conta pra esse e-mail.
        now = utc_now()
        with connect_database(self.database_path) as db:
            db.execute(
                """
                INSERT INTO customer_billing (
                    user_id, customer_name, email, phone, plan_name, monthly_amount,
                    due_date, billing_status, last_paid_at, notes, created_at, updated_at
                ) VALUES (?, 'Cliente Antigo', 'cliente.telegram@example.com', '11988887777',
                          'Mensal', '297', '2026-12-31', 'paid', ?, '', ?, ?)
                """,
                (self.telegram_user_id, now, now, now),
            )

        with self.assertRaisesRegex(ValueError, "já possui cadastro"):
            self.auth.register(
                customer_name="Impostor",
                email="cliente.telegram@example.com",
                phone="11900000000",
                password="SenhaImpostor1",
            )

        with connect_database(self.database_path) as db:
            self.assertEqual(1, db.execute("SELECT COUNT(*) FROM users").fetchone()[0])

    def test_reconfigurar_senha_atualiza_credencial_existente_sem_duplicar(self) -> None:
        self.auth.set_password_for_user(
            self.telegram_user_id, email="primeiro@example.com", password="SenhaUm12345"
        )
        self.auth.set_password_for_user(
            self.telegram_user_id, email="segundo@example.com", password="SenhaDois12345"
        )

        with connect_database(self.database_path) as db:
            self.assertEqual(
                1,
                db.execute(
                    "SELECT COUNT(*) FROM client_credentials WHERE user_id = ?",
                    (self.telegram_user_id,),
                ).fetchone()[0],
            )
        with self.assertRaises(ValueError):
            self.auth.login(email="primeiro@example.com", password="SenhaUm12345")
        login = self.auth.login(email="segundo@example.com", password="SenhaDois12345")
        self.assertEqual(self.telegram_user_id, login.user_id)


if __name__ == "__main__":
    unittest.main()
