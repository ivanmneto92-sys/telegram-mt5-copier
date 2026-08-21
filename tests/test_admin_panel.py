from __future__ import annotations

import json
from pathlib import Path
import tempfile
import time
import unittest
from datetime import date, timedelta

from telegram_mt5_copier.admin_panel import (
    AdminPanelService,
    render_admin_panel,
    render_admin_script,
)
from telegram_mt5_copier.database import connect_database
from telegram_mt5_copier.mt5.account_service import MT5AccountService
from telegram_mt5_copier.users import USER_STATUS_ACTIVE, USER_STATUS_PAUSED, UserRepository
from telegram_mt5_copier.web_app import WebAppValidationError, build_signed_init_data
from tests.access_helpers import grant_paid_access


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
        grant_paid_access(self.database_path, self.bob.id)
        self.users.set_status(self.bob.id, USER_STATUS_PAUSED)
        result = self.service.set_user_status(
            admin_telegram_user_id=9001,
            target_user_id=self.bob.id,
            status=USER_STATUS_ACTIVE,
        )

        self.assertEqual(result["status"], USER_STATUS_ACTIVE)
        self.assertEqual(self.users.get_by_id(self.bob.id).status, USER_STATUS_ACTIVE)
        with connect_database(self.database_path) as connection:
            action = connection.execute(
                """
                SELECT action_type, admin_telegram_user_id, target_user_id
                FROM admin_actions
                WHERE action_type = 'admin_panel_set_user_status'
                """
            ).fetchone()
        self.assertEqual(
            tuple(action),
            ("admin_panel_set_user_status", 9001, self.bob.id),
        )

    def _insert_account_with_profile(self, user_id: int, login: str) -> int:
        now = "2026-01-01T00:00:00+00:00"
        with connect_database(self.database_path) as connection:
            cursor = connection.execute(
                """
                INSERT INTO mt5_accounts (
                    user_id, broker_name, server_name, login, encrypted_password,
                    account_alias, account_type, account_mode, connection_status,
                    created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (user_id, "Broker", "Broker-Live", login, "encrypted", "Principal",
                 "real", "hedging", "disconnected", now, now),
            )
            account_id = int(cursor.lastrowid)
            cursor.close()
            connection.execute(
                """
                INSERT INTO execution_profiles (
                    user_id, mt5_account_id, enabled, risk_mode, fixed_lot, risk_percent,
                    max_spread_points, max_slippage_points, daily_profit_target,
                    daily_loss_limit, max_open_signals, split_tps, breakeven_enabled,
                    trailing_enabled, entry_execution_mode, entry_price_mode,
                    pending_expiration_minutes, take_profit_limit,
                    tp1_breakeven_enabled, updated_at
                )
                VALUES (?, ?, 1, 'fixed_lot', '0.01', '1', 300, 30, '0', '0', 1, 1, 0, 0,
                        'pending_order', 'first_touch', 120, 0, 1, ?)
                """,
                (user_id, account_id, now),
            ).close()
        return account_id

    def test_pausar_cliente_libera_vaga_e_reativar_devolve(self) -> None:
        grant_paid_access(self.database_path, self.bob.id)
        self.users.set_status(self.bob.id, USER_STATUS_ACTIVE)
        self._insert_account_with_profile(self.bob.id, "445566")
        accounts = MT5AccountService(self.database_path)

        self.assertEqual(accounts.count_accounts(), 1)

        self.service.set_user_status(
            admin_telegram_user_id=9001,
            target_user_id=self.bob.id,
            status=USER_STATUS_PAUSED,
        )
        self.assertEqual(accounts.count_accounts(), 0)
        with connect_database(self.database_path) as connection:
            enabled = connection.execute(
                "SELECT enabled FROM execution_profiles WHERE user_id = ?",
                (self.bob.id,),
            ).fetchone()[0]
        self.assertEqual(enabled, 0)

        self.service.set_user_status(
            admin_telegram_user_id=9001,
            target_user_id=self.bob.id,
            status=USER_STATUS_ACTIVE,
        )
        self.assertEqual(accounts.count_accounts(), 1)

    def test_status_invalido_e_rejeitado(self) -> None:
        with self.assertRaises(ValueError):
            self.service.set_user_status(
                admin_telegram_user_id=9001,
                target_user_id=self.alice.id,
                status="deleted",
            )
        self.assertEqual(self.users.get_by_id(self.alice.id).status, USER_STATUS_PAUSED)

    def test_aprovacao_exige_pagamento_e_validade(self) -> None:
        with self.assertRaises(ValueError):
            self.service.set_user_status(
                admin_telegram_user_id=9001,
                target_user_id=self.alice.id,
                status=USER_STATUS_ACTIVE,
            )

        result = self.service.approve_paid_access(
            admin_telegram_user_id=9001,
            target_user_id=self.alice.id,
            amount="250.00",
            paid_at=date.today().isoformat(),
            method="PIX",
            reference="PAG-1",
            expires_on=(date.today() + timedelta(days=30)).isoformat(),
        )
        dashboard = self.service.dashboard()
        alice = next(item for item in dashboard["users"] if item["id"] == self.alice.id)

        self.assertEqual(result["status"], USER_STATUS_ACTIVE)
        self.assertTrue(alice["access"]["allowed"])
        self.assertEqual(alice["access"]["amount_paid"], "250.00")
        self.assertEqual(dashboard["summary"]["approved_accesses"], 1)

    def test_cadastro_financeiro_aparece_no_dashboard(self) -> None:
        due_date = (date.today() + timedelta(days=5)).isoformat()
        self.service.update_billing(
            admin_telegram_user_id=9001,
            target_user_id=self.alice.id,
            customer_name="Alice Silva",
            email="alice@example.com",
            phone="+55 11 99999-9999",
            plan_name="Plano Gold",
            monthly_amount="149,90",
            due_date=due_date,
            billing_status="pending",
            notes="Cliente mensal.",
        )

        payload = self.service.dashboard()
        alice = next(item for item in payload["users"] if item["id"] == self.alice.id)

        self.assertEqual(alice["billing"]["customer_name"], "Alice Silva")
        self.assertEqual(alice["billing"]["monthly_amount"], "149.90")
        self.assertEqual(alice["billing"]["due_date"], due_date)
        self.assertEqual(payload["summary"]["billing_due_soon"], 1)
        self.assertEqual(payload["summary"]["monthly_total"], "149.90")

    def test_vencido_e_classificado_como_em_atraso(self) -> None:
        self.service.update_billing(
            admin_telegram_user_id=9001,
            target_user_id=self.alice.id,
            customer_name="Alice",
            email="",
            phone="",
            plan_name="Mensal",
            monthly_amount="100",
            due_date=(date.today() - timedelta(days=1)).isoformat(),
            billing_status="paid",
            notes="",
        )

        payload = self.service.dashboard()
        alice = next(item for item in payload["users"] if item["id"] == self.alice.id)

        self.assertEqual(alice["billing"]["effective_status"], "overdue")
        self.assertEqual(payload["summary"]["billing_overdue"], 1)

    def test_pagamento_e_registrado_e_avanca_vencimento(self) -> None:
        self.service.update_billing(
            admin_telegram_user_id=9001,
            target_user_id=self.alice.id,
            customer_name="Alice",
            email="",
            phone="",
            plan_name="Mensal",
            monthly_amount="200",
            due_date="2026-01-31",
            billing_status="pending",
            notes="",
        )

        payment = self.service.record_payment(
            admin_telegram_user_id=9001,
            target_user_id=self.alice.id,
            amount="200",
            paid_at="2026-01-31",
            method="PIX",
            reference="TX-123",
            next_due_date="",
        )
        payload = self.service.dashboard()
        alice = next(item for item in payload["users"] if item["id"] == self.alice.id)

        self.assertEqual(payment["next_due_date"], "2026-02-28")
        self.assertEqual(alice["billing"]["last_paid_at"], "2026-01-31")
        self.assertEqual(alice["billing"]["payments"][0]["method"], "PIX")
        self.assertEqual(alice["billing"]["payments"][0]["amount"], "200.00")

    def test_interface_tem_busca_filtros_e_acoes_sem_segredos(self) -> None:
        html = render_admin_panel("nonce")
        script = render_admin_script()

        self.assertIn("Central de clientes", html)
        self.assertIn('id="search"', html)
        self.assertIn('data-filter="attention"', html)
        self.assertIn("/api/admin/session", script)
        self.assertIn("/api/admin/user-status", script)
        self.assertIn("/api/admin/billing-update", script)
        self.assertIn("/api/admin/approve", script)
        self.assertIn('data-finance-filter="overdue"', html)
        self.assertIn('data-view="overview"', html)
        self.assertIn('data-view="clients"', html)
        self.assertIn('data-view="finance"', html)
        self.assertIn('data-view="channels"', html)
        self.assertIn('id="finance-list"', html)
        self.assertNotIn("TELEGRAM_BOT_TOKEN", html + script)


if __name__ == "__main__":
    unittest.main()
