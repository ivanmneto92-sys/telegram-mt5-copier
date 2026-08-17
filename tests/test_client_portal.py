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


if __name__ == "__main__":
    unittest.main()
