from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from telegram_mt5_copier.admin_auth import AdminBrowserAuthService
from telegram_mt5_copier.app import main, run_set_admin_password
from telegram_mt5_copier.config import AppConfig


class SetAdminPasswordCliTests(unittest.TestCase):
    """Etapa de bootstrap: --set-admin-password roda na propria VPS, sem depender
    do bot nem de uma sessao ja autenticada -- so continua exigindo que o
    telegram_user_id esteja em BOT_ADMIN_IDS, mesma trava de sempre (ver
    admin_auth.AdminBrowserAuthService.set_password_for_admin/_require_admin)."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.config = AppConfig.load(
            project_root=self.root,
            env={"BOT_ADMIN_IDS": "9001"},
            create_dirs=True,
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_bootstrap_com_senhas_coincidentes_cria_login_funcional(self) -> None:
        with patch("getpass.getpass", side_effect=["SenhaForte123", "SenhaForte123"]):
            exit_code = run_set_admin_password(self.config, 9001, "admin@example.com")

        self.assertEqual(exit_code, 0)
        service = AdminBrowserAuthService(self.config.database_path, admin_ids=(9001,))
        session = service.login(email="admin@example.com", password="SenhaForte123")
        self.assertEqual(session.admin_telegram_user_id, 9001)

    def test_senhas_diferentes_nao_grava_nada(self) -> None:
        with patch("getpass.getpass", side_effect=["SenhaForte123", "OutraSenha456"]):
            exit_code = run_set_admin_password(self.config, 9001, "admin@example.com")

        self.assertEqual(exit_code, 2)
        service = AdminBrowserAuthService(self.config.database_path, admin_ids=(9001,))
        with self.assertRaises(ValueError):
            service.login(email="admin@example.com", password="SenhaForte123")

    def test_telegram_user_id_fora_de_bot_admin_ids_e_recusado(self) -> None:
        with patch("getpass.getpass", side_effect=["SenhaForte123", "SenhaForte123"]):
            exit_code = run_set_admin_password(self.config, 999999, "estranho@example.com")

        self.assertEqual(exit_code, 2)

    def test_cli_exige_admin_email_junto_de_set_admin_password(self) -> None:
        with patch("telegram_mt5_copier.app.AppConfig.load", return_value=self.config):
            exit_code = main(["--set-admin-password", "9001"])

        self.assertEqual(exit_code, 2)
