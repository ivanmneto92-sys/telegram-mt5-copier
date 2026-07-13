from __future__ import annotations

from pathlib import Path
import shutil
import tempfile
import unittest

from telegram_mt5_copier.bot_service import BotService
from telegram_mt5_copier.command_queue import CommandQueue
from telegram_mt5_copier.database import SignalDatabase
from telegram_mt5_copier.settings_service import SettingsService
from telegram_mt5_copier.users import UserRepository


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


def status_dummy():
    from telegram_mt5_copier.models import DecisionStatus

    return DecisionStatus.IGNORED


if __name__ == "__main__":
    unittest.main()
