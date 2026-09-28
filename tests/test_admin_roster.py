from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from telegram_mt5_copier.admin_panel import AdminPanelService
from telegram_mt5_copier.database import connect_database


class AdminRosterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp_dir.name) / "admin-roster.sqlite3"
        self.token = "123456:bot-token"
        self.master = 9001
        self.service = AdminPanelService(
            self.database_path,
            bot_token=self.token,
            admin_ids=(self.master,),
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def admin_actions(self) -> list[tuple[object, ...]]:
        with connect_database(self.database_path) as connection:
            return connection.execute(
                "SELECT admin_telegram_user_id, target_user_id, action_type FROM admin_actions"
            ).fetchall()

    def test_master_adiciona_admin_comum(self) -> None:
        result = self.service.add_admin(
            actor_telegram_user_id=self.master,
            target_telegram_user_id=202,
            role="regular",
            label="Suporte",
        )

        self.assertEqual(result["role"], "regular")
        roster = self.service.list_admin_roster()
        roster_entry = next(item for item in roster if item["telegram_user_id"] == 202)
        self.assertEqual(roster_entry["source"], "roster")
        self.assertEqual(roster_entry["role"], "regular")
        actions = self.admin_actions()
        self.assertEqual(actions, [(self.master, 202, "admin_roster_add")])

    def test_admin_comum_nao_pode_adicionar_admin(self) -> None:
        self.service.add_admin(
            actor_telegram_user_id=self.master, target_telegram_user_id=202, role="regular"
        )

        with self.assertRaisesRegex(ValueError, "master"):
            self.service.add_admin(
                actor_telegram_user_id=202, target_telegram_user_id=303, role="regular"
            )

    def test_ator_desconhecido_nao_pode_adicionar_admin(self) -> None:
        with self.assertRaisesRegex(ValueError, "master"):
            self.service.add_admin(
                actor_telegram_user_id=999, target_telegram_user_id=303, role="regular"
            )

    def test_role_invalido_e_rejeitado(self) -> None:
        with self.assertRaisesRegex(ValueError, "inválido"):
            self.service.add_admin(
                actor_telegram_user_id=self.master, target_telegram_user_id=202, role="super"
            )

    def test_adicionar_id_ja_fixo_no_env_e_rejeitado(self) -> None:
        with self.assertRaisesRegex(ValueError, "fixo do .env"):
            self.service.add_admin(
                actor_telegram_user_id=self.master, target_telegram_user_id=self.master, role="master"
            )

    def test_add_admin_e_upsert_reativa_revogado_e_troca_role(self) -> None:
        self.service.add_admin(
            actor_telegram_user_id=self.master, target_telegram_user_id=202, role="regular"
        )
        self.service.revoke_admin(actor_telegram_user_id=self.master, target_telegram_user_id=202)

        self.service.add_admin(
            actor_telegram_user_id=self.master, target_telegram_user_id=202, role="master", label="Novo"
        )

        roster = self.service.list_admin_roster()
        entry = next(item for item in roster if item["telegram_user_id"] == 202)
        self.assertEqual(entry["role"], "master")
        self.assertEqual(entry["label"], "Novo")

    def test_add_admin_sobre_ativo_troca_role_sem_erro(self) -> None:
        self.service.add_admin(
            actor_telegram_user_id=self.master, target_telegram_user_id=202, role="regular"
        )

        self.service.add_admin(
            actor_telegram_user_id=self.master, target_telegram_user_id=202, role="master"
        )

        roster = self.service.list_admin_roster()
        entry = next(item for item in roster if item["telegram_user_id"] == 202)
        self.assertEqual(entry["role"], "master")

    def test_master_remove_admin_comum(self) -> None:
        self.service.add_admin(
            actor_telegram_user_id=self.master, target_telegram_user_id=202, role="regular"
        )

        self.service.revoke_admin(actor_telegram_user_id=self.master, target_telegram_user_id=202)

        roster = self.service.list_admin_roster()
        self.assertFalse(any(item["telegram_user_id"] == 202 for item in roster))
        actions = self.admin_actions()
        self.assertEqual(actions[-1], (self.master, 202, "admin_roster_revoke"))

    def test_remover_id_fixo_no_env_e_rejeitado_com_mensagem_distinta(self) -> None:
        with self.assertRaisesRegex(ValueError, "fixo do .env"):
            self.service.revoke_admin(actor_telegram_user_id=self.master, target_telegram_user_id=self.master)

    def test_remover_id_inexistente_e_rejeitado_com_mensagem_distinta(self) -> None:
        with self.assertRaisesRegex(ValueError, "não encontrado"):
            self.service.revoke_admin(actor_telegram_user_id=self.master, target_telegram_user_id=555)

    def test_admin_comum_nao_pode_remover_admin(self) -> None:
        self.service.add_admin(
            actor_telegram_user_id=self.master, target_telegram_user_id=202, role="regular"
        )
        self.service.add_admin(
            actor_telegram_user_id=self.master, target_telegram_user_id=303, role="regular"
        )

        with self.assertRaisesRegex(ValueError, "master"):
            self.service.revoke_admin(actor_telegram_user_id=202, target_telegram_user_id=303)

    def test_trava_do_ultimo_master_quando_env_vazio(self) -> None:
        service = AdminPanelService(self.database_path, bot_token=self.token, admin_ids=())
        # Bootstrap: primeiro master via uma instancia com admin_ids=(202,), depois
        # reconstroi sem ele no .env pra simular "so restou o roster".
        bootstrap = AdminPanelService(self.database_path, bot_token=self.token, admin_ids=(202,))
        bootstrap.add_admin(actor_telegram_user_id=202, target_telegram_user_id=606, role="master")

        with self.assertRaisesRegex(ValueError, "último administrador master"):
            service.revoke_admin(actor_telegram_user_id=606, target_telegram_user_id=606)

    def test_trava_do_ultimo_master_libera_quando_ha_dois(self) -> None:
        bootstrap = AdminPanelService(self.database_path, bot_token=self.token, admin_ids=(202,))
        bootstrap.add_admin(actor_telegram_user_id=202, target_telegram_user_id=606, role="master")
        bootstrap.add_admin(actor_telegram_user_id=202, target_telegram_user_id=707, role="master")
        service = AdminPanelService(self.database_path, bot_token=self.token, admin_ids=())

        service.revoke_admin(actor_telegram_user_id=606, target_telegram_user_id=606)

        with self.assertRaisesRegex(ValueError, "último administrador master"):
            service.revoke_admin(actor_telegram_user_id=707, target_telegram_user_id=707)

    def test_trava_nao_interfere_quando_env_nao_vazio(self) -> None:
        self.service.add_admin(
            actor_telegram_user_id=self.master, target_telegram_user_id=202, role="master"
        )

        self.service.revoke_admin(actor_telegram_user_id=self.master, target_telegram_user_id=202)

        roster = self.service.list_admin_roster()
        self.assertFalse(any(item["telegram_user_id"] == 202 for item in roster))

    def test_list_admin_roster_traz_env_e_roster_exclui_revogado(self) -> None:
        self.service.add_admin(
            actor_telegram_user_id=self.master, target_telegram_user_id=202, role="regular"
        )
        self.service.add_admin(
            actor_telegram_user_id=self.master, target_telegram_user_id=303, role="regular"
        )
        self.service.revoke_admin(actor_telegram_user_id=self.master, target_telegram_user_id=303)

        roster = self.service.list_admin_roster()

        ids = {item["telegram_user_id"] for item in roster}
        self.assertIn(self.master, ids)
        self.assertIn(202, ids)
        self.assertNotIn(303, ids)
        env_entry = next(item for item in roster if item["telegram_user_id"] == self.master)
        self.assertEqual(env_entry["source"], "env")
        self.assertEqual(env_entry["role"], "master")


if __name__ == "__main__":
    unittest.main()
