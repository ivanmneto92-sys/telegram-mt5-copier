from __future__ import annotations

from pathlib import Path
import shutil
import tempfile
import unittest

from telegram_mt5_copier.bot_service import BotService
from telegram_mt5_copier.command_queue import CommandQueue
from telegram_mt5_copier.credential_service import CredentialService
from telegram_mt5_copier.database import SignalDatabase
from telegram_mt5_copier.mt5.account_service import MT5AccountForm, MT5AccountService
from telegram_mt5_copier.mt5.client import SimulatedMT5Client
from telegram_mt5_copier.mt5.pending_order_executor import PendingOrderExecutor
from telegram_mt5_copier.mt5.pending_order_monitor import PendingOrderMonitor
from telegram_mt5_copier.mt5.terminal_manager import TerminalManager
from telegram_mt5_copier.parser import parse_signal_text
from telegram_mt5_copier.settings_service import SettingsService
from telegram_mt5_copier.users import USER_STATUS_ACTIVE, UserRepository
from tests.access_helpers import grant_paid_access


class SQLiteLifecycleTests(unittest.TestCase):
    def test_signal_database_close_allows_immediate_file_delete(self) -> None:
        temp_dir = tempfile.TemporaryDirectory()
        db_path = Path(temp_dir.name) / "signals.sqlite3"
        database = SignalDatabase(db_path)
        try:
            database.initialize()
            database.record_event(status_dummy(), "test")
        finally:
            database.close()

        database.close()

        db_path.unlink()
        temp_dir.cleanup()

    def test_bot_services_close_allow_immediate_directory_delete(self) -> None:
        root = Path(tempfile.mkdtemp())
        db_path = root / "bot.sqlite3"
        service = BotService(db_path)
        users = UserRepository(db_path)
        settings = SettingsService(db_path)
        commands = CommandQueue(db_path)

        try:
            service.start(telegram_user_id=101, telegram_username="alice")
            user = users.get_by_telegram_user_id(101)
            settings.update_fixed_lot(user.id, "0.10")
            commands.enqueue(user.id, "update_fixed_lot", {"fixed_lot": "0.10"})
        finally:
            service.close()
            users.close()
            settings.close()
            commands.close()

        service.close()
        users.close()
        settings.close()
        commands.close()

        db_path.unlink()
        shutil.rmtree(root)

    def test_signal_database_context_manager_allows_immediate_directory_delete(self) -> None:
        root = Path(tempfile.mkdtemp())
        db_path = root / "signals.sqlite3"

        with SignalDatabase(db_path) as database:
            database.initialize()
            database.record_event(status_dummy(), "test")

        db_path.unlink()
        shutil.rmtree(root)

    def test_pending_execution_services_allow_immediate_directory_delete(self) -> None:
        root = Path(tempfile.mkdtemp())
        db_path = root / "bot.sqlite3"
        users = UserRepository(db_path)
        accounts = MT5AccountService(
            db_path,
            credential_service=CredentialService(CredentialService.generate_key()),
            terminal_manager=TerminalManager(root / "mt5"),
            client_factory=lambda: SimulatedMT5Client(),
        )
        executor = PendingOrderExecutor(db_path, accounts, global_kill_switch=False)
        monitor = PendingOrderMonitor(db_path)

        try:
            user = users.get_or_create_user(101, "alice")
            grant_paid_access(db_path, user.id)
            account = accounts.register_account(
                user.id,
                MT5AccountForm("Broker", "Broker-Demo", "12345678", "secret", "Demo"),
            )
            accounts.update_execution_profile_fixed_lot(user.id, account.id, "0.04")
            signal = parse_signal_text(
                "XAUUSD BUY\nENTRY 4059-4061\nSL 4044\nTP 4066\nTP 4071\nTP 4076\nTP 4096"
            ).signal

            executor.execute_for_signal(signal)
            monitor.expire_orders()
        finally:
            executor.close()
            monitor.close()
            accounts.close()
            users.close()

        executor.close()
        monitor.close()
        accounts.close()
        users.close()

        db_path.unlink()
        shutil.rmtree(root)


def status_dummy():
    from telegram_mt5_copier.models import DecisionStatus

    return DecisionStatus.IGNORED


if __name__ == "__main__":
    unittest.main()
