from __future__ import annotations

from decimal import Decimal
from pathlib import Path
import tempfile
import unittest

from telegram_mt5_copier.bot_service import BotService
from telegram_mt5_copier.command_queue import CommandQueue
from telegram_mt5_copier.settings_service import SettingsService
from telegram_mt5_copier.users import USER_STATUS_ACTIVE, USER_STATUS_PAUSED, UserRepository


class BotManagementTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp_dir.name) / "bot.sqlite3"
        self.service = BotService(self.database_path)
        self.users = UserRepository(self.database_path)
        self.settings = SettingsService(self.database_path)
        self.commands = CommandQueue(self.database_path)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_cadastro_do_usuario_no_start(self) -> None:
        response = self.service.start(telegram_user_id=101, telegram_username="alice")
        user = self.users.get_by_telegram_user_id(101)

        self.assertIn("bot privado", response.text)
        self.assertEqual(user.telegram_username, "alice")
        self.assertEqual(user.status, USER_STATUS_PAUSED)
        self.assertIsNotNone(self.settings.get_settings(user.id))

    def test_ativar_exige_confirmacao_e_depois_ativa(self) -> None:
        self.service.start(101, "alice")

        confirm_response = self.service.handle_callback(101, "alice", "confirm:activate")
        user_after_prompt = self.users.get_by_telegram_user_id(101)
        action_response = self.service.handle_callback(101, "alice", "action:activate:confirm")
        user_after_confirm = self.users.get_by_telegram_user_id(101)

        self.assertIn("Confirme", confirm_response.text)
        self.assertEqual(user_after_prompt.status, USER_STATUS_PAUSED)
        self.assertIn("ativado", action_response.text)
        self.assertEqual(user_after_confirm.status, USER_STATUS_ACTIVE)

    def test_pausar(self) -> None:
        self.service.start(101, "alice")
        self.service.handle_callback(101, "alice", "action:activate:confirm")

        response = self.service.handle_callback(101, "alice", "action:pause:confirm")
        user = self.users.get_by_telegram_user_id(101)

        self.assertIn("bloqueadas", response.text)
        self.assertEqual(user.status, USER_STATUS_PAUSED)

    def test_alteracao_de_lote(self) -> None:
        self.service.start(101, "alice")
        user = self.users.get_by_telegram_user_id(101)

        response = self.service.handle_callback(101, "alice", "set:fixed_lot:0.10")
        settings = self.settings.get_settings(user.id)

        self.assertIn("Lote fixo atualizado", response.text)
        self.assertEqual(settings.fixed_lot, Decimal("0.1"))

    def test_validacao_de_lote(self) -> None:
        self.service.start(101, "alice")
        user = self.users.get_by_telegram_user_id(101)

        response = self.service.handle_callback(101, "alice", "set:fixed_lot:0.00")
        settings = self.settings.get_settings(user.id)

        self.assertIn("fora do intervalo", response.text)
        self.assertEqual(settings.fixed_lot, Decimal("0.01"))

    def test_isolamento_entre_usuarios(self) -> None:
        self.service.start(101, "alice")
        self.service.start(202, "bob")
        alice = self.users.get_by_telegram_user_id(101)
        bob = self.users.get_by_telegram_user_id(202)

        self.service.handle_callback(101, "alice", "set:fixed_lot:0.20")

        self.assertEqual(self.settings.get_settings(alice.id).fixed_lot, Decimal("0.2"))
        self.assertEqual(self.settings.get_settings(bob.id).fixed_lot, Decimal("0.01"))

    def test_callback_invalido(self) -> None:
        self.service.start(101, "alice")

        response = self.service.handle_callback(101, "alice", "set:user:202:active")

        self.assertIn("invalida", response.text)

    def test_confirmacao_de_acao_critica(self) -> None:
        self.service.start(101, "alice")

        self.service.handle_callback(101, "alice", "confirm:pause")
        user = self.users.get_by_telegram_user_id(101)

        self.assertEqual(user.status, USER_STATUS_PAUSED)

    def test_criacao_de_comando(self) -> None:
        self.service.start(101, "alice")
        user = self.users.get_by_telegram_user_id(101)

        command = self.commands.enqueue(user.id, "update_fixed_lot", {"fixed_lot": "0.10"})

        self.assertTrue(command.created)
        self.assertEqual(command.command_type, "update_fixed_lot")
        self.assertEqual(self.commands.count(user.id), 1)

    def test_prevencao_de_comando_duplicado(self) -> None:
        self.service.start(101, "alice")
        user = self.users.get_by_telegram_user_id(101)

        first = self.commands.enqueue(user.id, "update_fixed_lot", {"fixed_lot": "0.10"})
        second = self.commands.enqueue(user.id, "update_fixed_lot", {"fixed_lot": "0.10"})

        self.assertTrue(first.created)
        self.assertFalse(second.created)
        self.assertEqual(first.id, second.id)
        self.assertEqual(self.commands.count(user.id), 1)


if __name__ == "__main__":
    unittest.main()
