from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from telegram_mt5_copier.credential_service import CredentialService
from telegram_mt5_copier.mt5.account_service import MT5AccountForm, MT5AccountService
from telegram_mt5_copier.mt5.client import SimulatedMT5Client
from telegram_mt5_copier.mt5.terminal_manager import TerminalManager
from telegram_mt5_copier.mt5_worker import process_account
from telegram_mt5_copier.users import UserRepository


class ExplodingWorker:
    def process_once(self) -> bool:
        raise RuntimeError("falha simulada de IPC")

    def close(self) -> None:
        pass


class HealthyWorker:
    def process_once(self) -> bool:
        return True

    def close(self) -> None:
        pass


class RecordingPositionManager:
    def __init__(self) -> None:
        self.managed_account_ids: list[int] = []

    def manage_account(self, account, profile) -> None:
        self.managed_account_ids.append(account.id)


class RecordingSettlementMonitor:
    def __init__(self) -> None:
        self.delivered_account_ids: list[int] = []

    def deliver_pending(self, account) -> None:
        self.delivered_account_ids.append(account.id)


class ProcessAccountIsolationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.database_path = self.root / "worker.sqlite3"
        self.users = UserRepository(self.database_path)
        self.credential_service = CredentialService(CredentialService.generate_key())
        self.accounts = MT5AccountService(
            self.database_path,
            credential_service=self.credential_service,
            terminal_manager=TerminalManager(self.root / "mt5"),
            client_factory=lambda: SimulatedMT5Client(),
        )
        self.broken_user = self.users.get_or_create_user(101, "broken")
        self.broken_account = self.accounts.register_account(
            self.broken_user.id,
            MT5AccountForm("Broker", "Broker-Demo", "11111111", "secret", "Quebrada"),
        )
        self.healthy_user = self.users.get_or_create_user(202, "healthy")
        self.healthy_account = self.accounts.register_account(
            self.healthy_user.id,
            MT5AccountForm("Broker", "Broker-Demo", "22222222", "secret", "Saudavel"),
        )

    def tearDown(self) -> None:
        self.accounts.close()
        self.users.close()
        self.temp_dir.cleanup()

    def profile_for(self, user_id: int, account_id: int):
        profile = self.accounts.get_execution_profile(user_id, account_id)
        assert profile is not None
        return profile

    def test_falha_em_uma_conta_nao_impede_a_proxima_de_ser_processada(self) -> None:
        workers = {
            self.broken_account.id: ExplodingWorker(),
            self.healthy_account.id: HealthyWorker(),
        }
        position_manager = RecordingPositionManager()
        settlement_monitor = RecordingSettlementMonitor()
        last_account_checks: dict[int, float] = {}
        account_connection_states: dict[int, bool] = {}

        for account, profile in (
            (self.broken_account, self.profile_for(self.broken_user.id, self.broken_account.id)),
            (self.healthy_account, self.profile_for(self.healthy_user.id, self.healthy_account.id)),
        ):
            process_account(
                account,
                profile,
                workers=workers,
                accounts=self.accounts,
                command_queue=None,
                position_manager=position_manager,
                settlement_monitor=settlement_monitor,
                last_account_checks=last_account_checks,
                account_connection_states=account_connection_states,
            )

        self.assertEqual(position_manager.managed_account_ids, [self.healthy_account.id])
        self.assertEqual(settlement_monitor.delivered_account_ids, [self.healthy_account.id])
        self.assertNotIn(self.broken_account.id, account_connection_states)


if __name__ == "__main__":
    unittest.main()
