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

    def test_profile_can_be_read_and_updated_without_changing_other_users(self) -> None:
        now = utc_now()
        with connect_database(self.database_path) as db:
            db.execute(
                """
                INSERT INTO customer_billing (
                    user_id, customer_name, email, phone, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (self.user_id, "Nome Antigo", "antigo@example.com", "11999990000", now, now),
            )
        auth = ClientBrowserAuthService(self.database_path)
        auth.set_password_for_user(
            self.user_id, email="antigo@example.com", password="Senha1234"
        )
        portal = ClientPortalService(self.database_path, brand_name="Marca")

        payload = portal.update_profile(
            self.user_id,
            customer_name="Nome Novo",
            email="novo@example.com",
            phone="11988887777",
        )

        self.assertEqual("Nome Novo", payload["profile"]["customer_name"])
        self.assertEqual("novo@example.com", payload["profile"]["email"])
        self.assertEqual(
            self.user_id,
            auth.login(email="novo@example.com", password="Senha1234").user_id,
        )

    def test_financial_returns_real_billing_and_payment_history(self) -> None:
        now = utc_now()
        with connect_database(self.database_path) as db:
            db.execute(
                """
                INSERT INTO customer_billing (
                    user_id, customer_name, email, phone, plan_name, monthly_amount,
                    due_date, billing_status, last_paid_at, created_at, updated_at
                ) VALUES (?, 'Cliente', 'c@example.com', '11999990000', 'Mensal',
                          '300.00', '2026-10-10', 'paid', ?, ?, ?)
                """,
                (self.user_id, now, now, now),
            )
            db.execute(
                """
                INSERT INTO customer_payments (
                    user_id, amount, paid_at, period_start, period_end, method,
                    status, admin_telegram_user_id, created_at
                ) VALUES (?, '300.00', ?, '2026-09-10', '2026-10-10', 'PIX',
                          'paid', 999, ?)
                """,
                (self.user_id, now, now),
            )
        portal = ClientPortalService(self.database_path, brand_name="Marca")

        payload = portal.financial(self.user_id)

        self.assertEqual("300.00", payload["billing"]["monthly_amount"])
        self.assertEqual("PIX", payload["payments"][0]["method"])
        self.assertNotIn("reference", payload["payments"][0])

    def test_risk_update_is_scoped_to_authenticated_user_account(self) -> None:
        now = utc_now()
        with connect_database(self.database_path) as db:
            account_id = int(db.execute(
                """
                INSERT INTO mt5_accounts (
                    user_id, account_alias, broker_name, terminal_path, server_name,
                    login, encrypted_password, account_type, account_mode,
                    connection_status, created_at, updated_at
                ) VALUES (?, 'Principal', 'HFM', 'terminal64.exe', 'HFM-Live',
                          '123456', 'encrypted', 'real', 'hedging', 'connected', ?, ?)
                """,
                (self.user_id, now, now),
            ).lastrowid)
            db.execute(
                """
                INSERT INTO execution_profiles (
                    user_id, mt5_account_id, enabled, risk_mode, fixed_lot, risk_percent,
                    max_spread_points, max_slippage_points, daily_profit_target,
                    daily_loss_limit, max_open_signals, split_tps, breakeven_enabled,
                    trailing_enabled, updated_at
                ) VALUES (?, ?, 1, 'fixed_lot', '0.01', '1', 300, 30, '0', '0',
                          1, 1, 0, 0, ?)
                """,
                (self.user_id, account_id, now),
            )
        portal = ClientPortalService(self.database_path, brand_name="Marca")

        payload = portal.update_risk(
            self.user_id,
            {
                "risk_mode": "risk_percent",
                "risk_percent": "0,5",
                "daily_loss_limit": "30",
                "max_open_signals": "2",
                "tp1_breakeven_enabled": "true",
            },
        )

        self.assertEqual("risk_percent", payload["risk"]["risk_mode"])
        self.assertEqual("0.5", payload["risk"]["risk_percent"])
        self.assertEqual("30", payload["risk"]["daily_loss_limit"])
        self.assertEqual(2, payload["risk"]["max_open_signals"])
        self.assertTrue(payload["risk"]["tp1_breakeven_enabled"])

    def test_risk_rejects_unknown_and_out_of_range_values(self) -> None:
        portal = ClientPortalService(self.database_path, brand_name="Marca")
        with self.assertRaisesRegex(ValueError, "Cadastre uma conta"):
            portal.update_risk(self.user_id, {"risk_percent": "0.5"})


if __name__ == "__main__":
    unittest.main()
