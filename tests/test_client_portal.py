from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from telegram_mt5_copier.client_auth import ClientBrowserAuthService
from telegram_mt5_copier.client_portal import ClientPortalService
from telegram_mt5_copier.database import connect_database, initialize_database, utc_now


class ClientPortalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = TemporaryDirectory()
        self.database_path = Path(self.temp.name) / "portal.sqlite3"
        initialize_database(self.database_path)
        now = utc_now()
        with connect_database(self.database_path) as db:
            self.user_id = int(db.execute(
                "INSERT INTO users (telegram_user_id, telegram_username, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                (123, "cliente", "active", now, now),
            ).lastrowid)
            db.execute(
                """
                INSERT INTO source_channels (
                    telegram_chat_id, title, display_name, status, access_status,
                    created_at, updated_at
                ) VALUES (?, ?, ?, 'active', 'confirmed', ?, ?)
                """,
                ("-1001", "Nome Original", "Gold Alpha", now, now),
            )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_link_is_one_time_and_session_authenticates_user(self) -> None:
        auth = ClientBrowserAuthService(self.database_path)
        url = auth.create_login_url(self.user_id, "https://app.example.com/")
        token = url.split("#token=", 1)[1]
        session = auth.consume_login_token(token)
        self.assertEqual(self.user_id, auth.authenticate_session(session.session_token))
        with self.assertRaises(ValueError):
            auth.consume_login_token(token)

    def test_channels_use_original_title_not_display_name(self) -> None:
        portal = ClientPortalService(self.database_path, brand_name="Marca")
        payload = portal.channels(self.user_id)
        self.assertEqual("Nome Original", payload["channels"][0]["name"])
        self.assertEqual("custom", payload["selection_mode"])
        self.assertFalse(payload["channels"][0]["enabled"])

    def test_web_registration_creates_pending_customer_and_secure_login(self) -> None:
        auth = ClientBrowserAuthService(self.database_path)
        session = auth.register(
            customer_name="Maria da Silva",
            email=" Maria@Example.com ",
            phone="(11) 99999-0000",
            password="Segura123",
        )
        self.assertEqual(session.user_id, auth.authenticate_session(session.session_token))

        with connect_database(self.database_path) as db:
            user = db.execute(
                "SELECT telegram_user_id, status FROM users WHERE id = ?",
                (session.user_id,),
            ).fetchone()
            billing = db.execute(
                "SELECT customer_name, email, phone, billing_status FROM customer_billing WHERE user_id = ?",
                (session.user_id,),
            ).fetchone()
            credential = db.execute(
                "SELECT email, password_hash FROM client_credentials WHERE user_id = ?",
                (session.user_id,),
            ).fetchone()

        self.assertLess(int(user[0]), 0)
        self.assertEqual("paused", user[1])
        self.assertEqual(
            ("Maria da Silva", "maria@example.com", "(11) 99999-0000", "pending"),
            billing,
        )
        self.assertEqual("maria@example.com", credential[0])
        self.assertTrue(str(credential[1]).startswith("scrypt$"))
        self.assertNotIn("Segura123", str(credential[1]))

        login = auth.login(email="MARIA@example.com", password="Segura123")
        self.assertEqual(session.user_id, login.user_id)

    def test_invalid_password_is_rejected_and_account_is_temporarily_locked(self) -> None:
        auth = ClientBrowserAuthService(self.database_path)
        auth.register(
            customer_name="Cliente Teste",
            email="cliente@example.com",
            phone="11999990000",
            password="Correta123",
        )
        for _ in range(5):
            with self.assertRaisesRegex(ValueError, "E-mail ou senha inválidos"):
                auth.login(email="cliente@example.com", password="Errada123")
        with self.assertRaisesRegex(ValueError, "Muitas tentativas"):
            auth.login(email="cliente@example.com", password="Correta123")

    def test_existing_telegram_customer_can_define_web_password(self) -> None:
        auth = ClientBrowserAuthService(self.database_path)
        auth.set_password_for_user(
            self.user_id,
            email="existente@example.com",
            password="NovaSenha123",
        )
        session = auth.login(email="existente@example.com", password="NovaSenha123")
        self.assertEqual(self.user_id, session.user_id)

    def test_registration_does_not_claim_existing_billing_email(self) -> None:
        now = utc_now()
        with connect_database(self.database_path) as db:
            db.execute(
                """
                INSERT INTO customer_billing (
                    user_id, customer_name, email, phone, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (self.user_id, "Cliente Atual", "atual@example.com", "11999990000", now, now),
            )
        auth = ClientBrowserAuthService(self.database_path)
        with self.assertRaisesRegex(ValueError, "já possui cadastro"):
            auth.register(
                customer_name="Pessoa Indevida",
                email="ATUAL@example.com",
                phone="11888880000",
                password="Senha1234",
            )


if __name__ == "__main__":
    unittest.main()
