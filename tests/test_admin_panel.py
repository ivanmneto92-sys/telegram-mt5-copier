from __future__ import annotations

import json
from pathlib import Path
import tempfile
import time
import unittest

from telegram_mt5_copier.admin_panel import (
    AdminPanelService,
    render_admin_panel,
    render_admin_script,
)
from telegram_mt5_copier.database import connect_database
from telegram_mt5_copier.users import USER_STATUS_ACTIVE, USER_STATUS_PAUSED, UserRepository
from telegram_mt5_copier.web_app import WebAppValidationError, build_signed_init_data


class AdminPanelTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp_dir.name) / "admin.sqlite3"
        self.token = "123456:bot-token"
        self.users = UserRepository(self.database_path)
        self.alice = self.users.get_or_create_user(101, "alice")
        self.bob = self.users.get_or_create_user(202, "bob")
        self.service = AdminPanelService(
            self.database_path,
            bot_token=self.token,
            admin_ids=(9001,),
        )

    def tearDown(self) -> None:
        self.users.close()
        self.temp_dir.cleanup()

    def init_data(self, user_id: int, username: str = "master") -> str:
        return build_signed_init_data(
            self.token,
            {
                "query_id": "admin-query",
                "auth_date": str(int(time.time())),
                "user": json.dumps(
                    {"id": user_id, "username": username},
                    separators=(",", ":"),
                ),
            },
        )

    def test_autentica_somente_admin_configurado(self) -> None:
        identity = self.service.authenticate(self.init_data(9001))

        self.assertEqual(identity.telegram_user_id, 9001)
        with self.assertRaises(WebAppValidationError):
            self.service.authenticate(self.init_data(101, "alice"))

    def test_dashboard_resume_clientes_sem_expor_login_completo(self) -> None:
        with connect_database(self.database_path) as connection:
            connection.execute(
                """
                INSERT INTO mt5_accounts (
                    user_id, broker_name, server_name, login, encrypted_password,
                    account_alias, terminal_path, account_type, account_mode,
                    connection_status, last_error, last_connected_at, balance,
                    equity, worker_heartbeat_at, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    self.alice.id,
                    "Broker",
                    "Broker-Live",
                    "12345678",
                    "encrypted",
                    "Principal",
                    None,
                    "real",
                    "hedging",
                    "connected",
                    None,
                    None,
                    "1000.50",
                    "1010.25",
                    None,
                    "2026-01-01T00:00:00+00:00",
                    "2026-01-01T00:00:00+00:00",
                ),
            )

        payload = self.service.dashboard()
        alice = next(item for item in payload["users"] if item["id"] == self.alice.id)

        self.assertEqual(payload["summary"]["users"], 2)
        self.assertEqual(alice["account"]["masked_login"], "••••5678")
        self.assertNotIn("12345678", repr(payload))
        self.assertNotIn("encrypted", repr(payload))

    def test_alteracao_de_status_fica_auditada(self) -> None:
        result = self.service.set_user_status(
            admin_telegram_user_id=9001,
            target_user_id=self.bob.id,
            status=USER_STATUS_ACTIVE,
        )

        self.assertEqual(result["status"], USER_STATUS_ACTIVE)
        self.assertEqual(self.users.get_by_id(self.bob.id).status, USER_STATUS_ACTIVE)
        with connect_database(self.database_path) as connection:
            action = connection.execute(
                "SELECT action_type, admin_telegram_user_id, target_user_id FROM admin_actions"
            ).fetchone()
        self.assertEqual(
            tuple(action),
            ("admin_panel_set_user_status", 9001, self.bob.id),
        )

    def test_status_invalido_e_rejeitado(self) -> None:
        with self.assertRaises(ValueError):
            self.service.set_user_status(
                admin_telegram_user_id=9001,
                target_user_id=self.alice.id,
                status="deleted",
            )
        self.assertEqual(self.users.get_by_id(self.alice.id).status, USER_STATUS_PAUSED)

    def test_interface_tem_busca_filtros_e_acoes_sem_segredos(self) -> None:
        html = render_admin_panel("nonce")
        script = render_admin_script()

        self.assertIn("Central de clientes", html)
        self.assertIn('id="search"', html)
        self.assertIn('data-filter="attention"', html)
        self.assertIn("/api/admin/session", script)
        self.assertIn("/api/admin/user-status", script)
        self.assertNotIn("TELEGRAM_BOT_TOKEN", html + script)


if __name__ == "__main__":
    unittest.main()
