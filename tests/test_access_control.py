from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
import tempfile
import unittest

from telegram_mt5_copier.access_control import (
    ACCESS_AWAITING_APPROVAL,
    ACCESS_EXPIRED,
    ACCESS_PAYMENT_PENDING,
    paid_access_decision,
)
from telegram_mt5_copier.database import connect_database
from telegram_mt5_copier.users import UserRepository
from tests.access_helpers import grant_paid_access


class PaidAccessControlTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp_dir.name) / "access.sqlite3"
        self.users = UserRepository(self.database_path)
        self.user = self.users.get_or_create_user(101, "alice")

    def tearDown(self) -> None:
        self.users.close()
        self.temp_dir.cleanup()

    def test_novo_cliente_aguarda_aprovacao(self) -> None:
        decision = paid_access_decision(self.database_path, self.user.id)

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, ACCESS_AWAITING_APPROVAL)

    def test_pagamento_e_validade_liberam_acesso(self) -> None:
        grant_paid_access(self.database_path, self.user.id)

        decision = paid_access_decision(self.database_path, self.user.id)

        self.assertTrue(decision.allowed)
        self.assertEqual(decision.amount_paid, 100)

    def test_validade_vencida_bloqueia_acesso(self) -> None:
        grant_paid_access(self.database_path, self.user.id)
        with connect_database(self.database_path) as connection:
            connection.execute(
                "UPDATE customer_billing SET due_date = ? WHERE user_id = ?",
                ((date.today() - timedelta(days=1)).isoformat(), self.user.id),
            )

        decision = paid_access_decision(self.database_path, self.user.id)

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, ACCESS_EXPIRED)

    def test_status_financeiro_pendente_bloqueia_acesso(self) -> None:
        grant_paid_access(self.database_path, self.user.id)
        with connect_database(self.database_path) as connection:
            connection.execute(
                "UPDATE customer_billing SET billing_status = 'pending' WHERE user_id = ?",
                (self.user.id,),
            )

        decision = paid_access_decision(self.database_path, self.user.id)

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, ACCESS_PAYMENT_PENDING)


if __name__ == "__main__":
    unittest.main()
