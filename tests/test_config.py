from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from telegram_mt5_copier.config import (
    AppConfig,
    parse_broker_template_paths,
    parse_broker_servers,
    parse_bool,
    parse_instance_id,
    parse_port,
    parse_source_chat_ids,
    project_path,
)


class ConfigTests(unittest.TestCase):
    def test_default_directories_are_relative_to_project_root_and_created(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp)

            config = AppConfig.load(project_root=project_root, env={}, create_dirs=True)

            self.assertEqual(config.data_dir, project_root / "data")
            self.assertEqual(config.session_dir, project_root / "sessions")
            self.assertEqual(config.log_dir, project_root / "logs")
            self.assertEqual(config.database_path, project_root / "data" / "telegram_mt5_copier.sqlite3")
            self.assertEqual(config.telegram_session_name, project_root / "sessions" / "telegram_mt5_copier")
            self.assertTrue(config.data_dir.is_dir())
            self.assertTrue(config.session_dir.is_dir())
            self.assertTrue(config.log_dir.is_dir())
            self.assertEqual(config.instance_id, "main")
            self.assertEqual(config.brand_name, "Instituto Trader")
            self.assertEqual(config.local_onboarding_url, "http://127.0.0.1:8080")

    def test_env_file_is_utf8_and_runtime_environment_wins(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp)
            env_file = project_root / ".env"
            env_file.write_text(
                "DRY_RUN=false\n"
                "DATA_DIR=./dados\n"
                "SESSION_DIR=./sessoes\n"
                "LOG_DIR=./logs-do-arquivo\n",
                encoding="utf-8",
            )

            config = AppConfig.load(
                project_root=project_root,
                env={"LOG_DIR": "./logs-do-ambiente"},
                create_dirs=False,
            )

            self.assertFalse(config.dry_run)
            self.assertEqual(config.data_dir, project_root / "dados")
            self.assertEqual(config.session_dir, project_root / "sessoes")
            self.assertEqual(config.log_dir, project_root / "logs-do-ambiente")

    def test_non_dry_run_requires_telegram_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = AppConfig.load(
                project_root=Path(tmp),
                env={"DRY_RUN": "false"},
                create_dirs=False,
            )

            self.assertEqual(
                config.missing_required_telegram_values(),
                (
                    "TELEGRAM_API_ID",
                    "TELEGRAM_API_HASH",
                    "SOURCE_CHAT_IDS",
                    "DESTINATION_CHAT_ID",
                ),
            )

    def test_config_repr_does_not_expose_telegram_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            secret_values = {
                "TELEGRAM_API_ID": "987654321",
                "TELEGRAM_API_HASH": "super-secret-api-hash",
                "TELEGRAM_BOT_TOKEN": "123456:secret-bot-token",
                "MT5_CREDENTIAL_KEY": "mt5-secret-key",
                "SOURCE_CHAT_ID": "-1001111111111",
                "DESTINATION_CHAT_ID": "-1002222222222",
                "BOT_ADMIN_IDS": "123456789",
            }

            config = AppConfig.load(
                project_root=Path(tmp),
                env=secret_values,
                create_dirs=False,
            )
            rendered_config = repr(config)

            for key, secret_value in secret_values.items():
                if key == "BOT_ADMIN_IDS":
                    continue
                self.assertNotIn(secret_value, rendered_config)

    def test_multiplos_canais_tem_precedencia_sobre_configuracao_antiga(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = AppConfig.load(
                project_root=Path(tmp),
                env={
                    "SOURCE_CHAT_ID": "-1001111111111",
                    "SOURCE_CHAT_IDS": " -1002222222222, -1003333333333, -1002222222222 ",
                },
                create_dirs=False,
            )

            self.assertEqual(
                config.source_chat_ids,
                ("-1002222222222", "-1003333333333"),
            )
            self.assertEqual(config.source_chat_id, "-1002222222222")

    def test_source_chat_id_antigo_continua_compativel(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = AppConfig.load(
                project_root=Path(tmp),
                env={"SOURCE_CHAT_ID": "-1001111111111"},
                create_dirs=False,
            )

            self.assertEqual(config.source_chat_ids, ("-1001111111111",))
            self.assertEqual(config.source_chat_id, "-1001111111111")

    def test_lista_de_canais_rejeita_identificador_nao_numerico(self) -> None:
        with self.assertRaisesRegex(ValueError, "IDs numericos"):
            parse_source_chat_ids("-1001111111111,canal-vip")

    def test_session_directory_is_created_from_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp)

            config = AppConfig.load(
                project_root=project_root,
                env={"SESSION_DIR": "./custom-sessions"},
                create_dirs=True,
            )

            self.assertEqual(config.session_dir, project_root / "custom-sessions")
            self.assertTrue(config.session_dir.is_dir())

    def test_relative_and_absolute_paths_are_supported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp)
            absolute_path = project_root / "custom-data"

            self.assertEqual(project_path("./data", project_root), project_root / "data")
            self.assertEqual(project_path(str(absolute_path), project_root), absolute_path)

    def test_bool_parser_accepts_common_values(self) -> None:
        self.assertTrue(parse_bool("true"))
        self.assertTrue(parse_bool("1"))
        self.assertFalse(parse_bool("false"))
        self.assertFalse(parse_bool("0"))
        with self.assertRaises(ValueError):
            parse_bool("talvez")

    def test_mt5_defaults_are_safe_for_simulation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp)

            config = AppConfig.load(project_root=project_root, env={}, create_dirs=True)

            self.assertEqual(config.mt5_execution_mode, "simulation")
            self.assertEqual(config.mt5_max_accounts_per_vps, 10)
            self.assertFalse(config.allow_live_accounts)
            self.assertEqual(config.mt5_base_dir, project_root / "mt5_accounts")
            self.assertTrue(config.mt5_base_dir.is_dir())
            self.assertTrue(config.operational_alerts_enabled)
            self.assertEqual(config.health_check_interval_seconds, 30)
            self.assertEqual(config.health_stale_after_seconds, 90)
            self.assertEqual(config.operational_alert_repeat_minutes, 360)

    def test_templates_mt5_por_corretora_sao_carregados(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = AppConfig.load(
                project_root=root,
                env={
                    "MT5_BROKER_TEMPLATES": (
                        r"HFM=C:\MT5TemplateHFM;FTMO=./templates/ftmo;"
                        r"EXNESS=C:\MT5TemplateExness"
                    )
                },
                create_dirs=False,
            )

            self.assertEqual(
                config.mt5_broker_template_paths,
                {
                    "HFM": Path(r"C:\MT5TemplateHFM"),
                    "FTMO": root / "templates" / "ftmo",
                    "EXNESS": Path(r"C:\MT5TemplateExness"),
                },
            )

    def test_templates_mt5_rejeitam_formato_invalido(self) -> None:
        with self.assertRaisesRegex(ValueError, "CORRETORA=CAMINHO"):
            parse_broker_template_paths("FTMO", Path.cwd())

    def test_servidores_mt5_por_corretora_sao_carregados(self) -> None:
        self.assertEqual(
            parse_broker_servers(
                "HFM=HFMarketsGlobal-Live1|HFMarketsGlobal-Live3;"
                "FTMO=FTMO-Demo"
            ),
            {
                "HFM": ("HFMarketsGlobal-Live1", "HFMarketsGlobal-Live3"),
                "FTMO": ("FTMO-Demo",),
            },
        )

    def test_servidores_mt5_rejeitam_formato_invalido(self) -> None:
        with self.assertRaisesRegex(ValueError, "CORRETORA=SERVIDOR1"):
            parse_broker_servers("HFMarketsGlobal-Live3")

    def test_instancia_white_label_isola_banco_sessao_porta_e_lock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = AppConfig.load(
                project_root=root,
                env={
                    "INSTANCE_ID": "Mesa-Vip",
                    "BRAND_NAME": "Mesa Alpha",
                    "ONBOARDING_PORT": "8081",
                },
                create_dirs=False,
            )

            self.assertEqual(config.instance_id, "mesa-vip")
            self.assertEqual(config.brand_name, "Mesa Alpha")
            self.assertEqual(
                config.database_path,
                root / "data" / "telegram_mt5_copier_mesa-vip.sqlite3",
            )
            self.assertEqual(
                config.telegram_session_name,
                root / "sessions" / "telegram_mt5_copier_mesa-vip",
            )
            self.assertEqual(
                config.supervisor_lock_path,
                root / "data" / "telegram-mt5-supervisor_mesa-vip.lock",
            )
            self.assertEqual(config.local_onboarding_url, "http://127.0.0.1:8081")

    def test_identificador_e_porta_da_instancia_sao_validados(self) -> None:
        self.assertEqual(parse_instance_id(" Marca_2 "), "marca_2")
        self.assertEqual(parse_port("8082"), 8082)
        with self.assertRaises(ValueError):
            parse_instance_id("marca com espaco")
        with self.assertRaises(ValueError):
            parse_port("70000")


if __name__ == "__main__":
    unittest.main()
