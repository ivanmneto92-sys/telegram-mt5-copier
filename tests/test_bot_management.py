from __future__ import annotations

from decimal import Decimal
from pathlib import Path
import tempfile
import unittest

from telegram_mt5_copier.bot_service import BotService
from telegram_mt5_copier.command_queue import CommandQueue
from telegram_mt5_copier.credential_service import CredentialService
from telegram_mt5_copier.database import (
    SIGNAL_MONITOR_SERVICE_NAME,
    connect_database,
    update_service_heartbeat,
)
from telegram_mt5_copier.mt5.account_service import MT5AccountForm, MT5AccountService
from telegram_mt5_copier.mt5.client import SimulatedMT5Client
from telegram_mt5_copier.mt5.models import ENTRY_PRICE_MIDDLE
from telegram_mt5_copier.mt5.terminal_manager import TerminalManager
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
        for service in (self.service, self.users, self.settings, self.commands):
            if hasattr(service, "close"):
                service.close()
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

    def test_painel_pausado(self) -> None:
        response = self.service.start(101, "alice", "Alice")

        self.assertIn("🤖 INSTITUTO TRADER", response.text)
        self.assertIn("Olá, Alice!", response.text)
        self.assertIn("Status do copiador: 🟡 Pausado", response.text)
        self.assertIn("Novas operações: ⏸️ Bloqueadas", response.text)

    def test_painel_ativo(self) -> None:
        self.service.start(101, "alice", "Alice")
        self.service.handle_callback(101, "alice", "v1:act:ok")

        response = self.service.menu(101, "alice", "Alice")

        self.assertIn("Status do copiador: 🟢 Ativo", response.text)
        self.assertIn("Novas operações: ▶️ Liberadas", response.text)

    def test_menu_principal(self) -> None:
        response = self.service.menu(101, "alice", "Alice")
        button_texts = [button.text for row in response.keyboard for button in row]

        self.assertIn("💼 Minha conta", button_texts)
        self.assertIn("📈 Operações", button_texts)
        self.assertIn("📡 Status da conexão", button_texts)
        self.assertIn("🔗 Conectar conta MT5", button_texts)
        self.assertIn("🖥️ Minhas contas", button_texts)
        self.assertIn("⚙️ Execução dos sinais", button_texts)

    def test_submenu_execucao_dos_sinais_salva_preferencias(self) -> None:
        accounts = MT5AccountService(
            self.database_path,
            credential_service=CredentialService(CredentialService.generate_key()),
            terminal_manager=TerminalManager(Path(self.temp_dir.name) / "mt5"),
            client_factory=lambda: SimulatedMT5Client(),
        )
        service = BotService(self.database_path, mt5_account_service=accounts)
        try:
            service.start(101, "alice")
            user = self.users.get_by_telegram_user_id(101)
            account = accounts.register_account(
                user.id,
                MT5AccountForm("Broker", "Broker-Demo", "12345678", "secret", "Demo"),
            )

            response = service.handle_callback(101, "alice", "v1:ex")
            service.handle_callback(101, "alice", "v1:ex:price:middle")
            service.handle_callback(101, "alice", "v1:ex:exp:60")
            service.handle_callback(101, "alice", "v1:ex:split")
            profile = accounts.get_execution_profile(user.id, account.id)

            self.assertIn("EXECUÇÃO DOS SINAIS", response.text)
            self.assertEqual(profile.entry_price_mode, ENTRY_PRICE_MIDDLE)
            self.assertEqual(profile.pending_expiration_minutes, 60)
            self.assertFalse(profile.split_tps)
        finally:
            service.close()
            accounts.close()

    def test_tela_conectar_conta_mt5_sem_conta(self) -> None:
        self.service.start(101, "alice")

        response = self.service.handle_callback(101, "alice", "v1:mt5:connect")
        button_texts = [button.text for row in response.keyboard for button in row]

        self.assertIn("🔗 CONECTAR CONTA MT5", response.text)
        self.assertIn("Nenhuma conta conectada.", response.text)
        self.assertIn("🔐 Abrir conexão segura", button_texts)

    def test_minha_contas_sem_conta_abre_onboarding(self) -> None:
        self.service.start(101, "alice")

        response = self.service.handle_callback(101, "alice", "v1:mt5:accounts")

        self.assertEqual(response.screen, "mt5_connect")
        self.assertIn("Cadastre uma conta MT5", response.text)

    def test_tela_principal_e_minha_conta_exibem_mt5_conectado(self) -> None:
        accounts = MT5AccountService(
            self.database_path,
            credential_service=CredentialService(CredentialService.generate_key()),
            terminal_manager=TerminalManager(Path(self.temp_dir.name) / "mt5"),
            client_factory=lambda: SimulatedMT5Client(),
        )
        service = BotService(self.database_path, mt5_account_service=accounts)
        try:
            service.start(101, "alice")
            user = self.users.get_by_telegram_user_id(101)
            accounts.register_account(
                user.id,
                MT5AccountForm("Broker", "Broker-Demo", "12345678", "secret", "Demo"),
            )

            main = service.menu(101, "alice", "Alice")
            account = service.handle_callback(101, "alice", "v1:a")

            self.assertIn("MetaTrader 5: 🟢 Conectada", main.text)
            self.assertIn("MetaTrader 5: 🟢 Conectada", account.text)
            self.assertIn("Saldo: 10000.00", account.text)
            self.assertIn("Equity: 10000.00", account.text)
            self.assertIn("Informações atualizadas pela conexão", account.text)
        finally:
            service.close()
            accounts.close()

    def test_gestao_de_risco_atualiza_perfil_usado_pelo_mt5(self) -> None:
        accounts = MT5AccountService(
            self.database_path,
            credential_service=CredentialService(CredentialService.generate_key()),
            terminal_manager=TerminalManager(Path(self.temp_dir.name) / "mt5-risk"),
            client_factory=lambda: SimulatedMT5Client(),
        )
        service = BotService(self.database_path, mt5_account_service=accounts)
        try:
            service.start(101, "alice")
            user = self.users.get_by_telegram_user_id(101)
            account = accounts.register_account(
                user.id,
                MT5AccountForm("Broker", "Broker-Demo", "12345678", "secret", "Demo"),
            )

            service.handle_callback(101, "alice", "v1:r:lot:0.10")
            service.handle_callback(101, "alice", "v1:r:mode:risk_percent")
            service.handle_callback(101, "alice", "v1:r:risk:0.50")
            service.handle_callback(101, "alice", "v1:r:loss:50")
            service.handle_callback(101, "alice", "v1:r:spread:100")
            service.handle_callback(101, "alice", "v1:r:slippage:20")
            profile = accounts.get_execution_profile(user.id, account.id)

            self.assertEqual(profile.fixed_lot, Decimal("0.10"))
            self.assertEqual(profile.risk_mode, "risk_percent")
            self.assertEqual(profile.risk_percent, Decimal("0.50"))
            self.assertEqual(profile.daily_loss_limit, Decimal("50"))
            self.assertEqual(profile.max_spread_points, 100)
            self.assertEqual(profile.max_slippage_points, 20)
        finally:
            service.close()
            accounts.close()

    def test_tela_minha_conta_sem_mt5(self) -> None:
        self.service.start(101, "alice")

        response = self.service.handle_callback(101, "alice", "v1:a")

        self.assertIn("💼 MINHA CONTA", response.text)
        self.assertIn("MetaTrader 5: ⚪ Não conectado", response.text)
        self.assertIn("Saldo: Aguardando conexão", response.text)
        self.assertIn("Equity: Aguardando conexão", response.text)

    def test_tela_operacoes_sem_mt5(self) -> None:
        self.service.start(101, "alice")

        response = self.service.handle_callback(101, "alice", "v1:o")

        self.assertIn("📈 OPERAÇÕES", response.text)
        self.assertIn("Nenhuma operação do copiador registrada como ativa.", response.text)
        self.assertIn("MetaTrader 5: ⚪ Não conectado", response.text)

    def test_confirmacao_de_ativacao_visual(self) -> None:
        self.service.start(101, "alice")

        response = self.service.handle_callback(101, "alice", "v1:act")

        self.assertIn("▶️ ATIVAR COPIADOR", response.text)
        self.assertIn("Deseja continuar?", response.text)
        self.assertIn("✅ Confirmar ativação", [button.text for row in response.keyboard for button in row])

    def test_confirmacao_de_pausa_visual(self) -> None:
        self.service.start(101, "alice")

        response = self.service.handle_callback(101, "alice", "v1:pause")

        self.assertIn("⏸️ PAUSAR NOVAS ENTRADAS", response.text)
        self.assertIn("Operações já abertas não serão fechadas automaticamente.", response.text)
        self.assertIn("⏸️ Confirmar pausa", [button.text for row in response.keyboard for button in row])

    def test_cancelamento(self) -> None:
        self.service.start(101, "alice")

        response = self.service.handle_callback(101, "alice", "v1:x")

        self.assertIn("cancelada", response.text)

    def test_voltar_ao_menu(self) -> None:
        self.service.start(101, "alice")

        response = self.service.handle_callback(101, "alice", "v1:m", "Alice")

        self.assertIn("🤖 INSTITUTO TRADER", response.text)
        self.assertEqual(response.screen, "main")

    def test_gestao_de_risco(self) -> None:
        self.service.start(101, "alice")

        response = self.service.handle_callback(101, "alice", "v1:r")

        self.assertIn("⚙️ GESTÃO DE RISCO", response.text)
        self.assertIn("Modo de gestão:", response.text)
        self.assertIn("Máximo de operações:", response.text)

    def test_protecoes(self) -> None:
        self.service.start(101, "alice")

        response = self.service.handle_callback(101, "alice", "v1:p")

        self.assertIn("🛡️ PROTEÇÕES", response.text)
        self.assertIn("Breakeven:", response.text)
        self.assertIn("Trailing Stop:", response.text)

    def test_historico(self) -> None:
        self.service.start(101, "alice")
        self.service.handle_callback(101, "alice", "v1:act:ok")

        response = self.service.handle_callback(101, "alice", "v1:h")

        self.assertIn("📋 HISTÓRICO", response.text)
        self.assertIn("Nenhuma execução registrada", response.text)

    def test_status_da_conexao(self) -> None:
        self.service.start(101, "alice")

        response = self.service.status(101, "alice")

        self.assertIn("📡 STATUS DA CONEXÃO", response.text)
        self.assertIn("Bot de gestão:\n🟢 Online", response.text)
        self.assertIn("Monitor de sinais:\n⚪ Aguardando inicialização", response.text)
        self.assertIn("MetaTrader 5:\n⚪ Não conectado", response.text)

    def test_atualizacao_de_telas(self) -> None:
        self.service.start(101, "alice")

        response = self.service.handle_callback(101, "alice", "v1:ref:account")

        self.assertEqual(response.screen, "account")
        self.assertIn("💼 MINHA CONTA", response.text)

    def test_persistencia_das_configuracoes(self) -> None:
        self.service.start(101, "alice")
        user = self.users.get_by_telegram_user_id(101)
        self.service.handle_callback(101, "alice", "set:fixed_lot:0.30")

        new_settings_service = SettingsService(self.database_path)
        try:
            settings = new_settings_service.get_settings(user.id)
        finally:
            new_settings_service.close()

        self.assertEqual(settings.fixed_lot, Decimal("0.3"))

    def test_ausencia_de_saldo_ficticio(self) -> None:
        self.service.start(101, "alice")

        response = self.service.handle_callback(101, "alice", "v1:a")

        self.assertIn("Aguardando conexão", response.text)
        self.assertNotIn("Saldo: 0", response.text)
        self.assertNotIn("Equity: 0", response.text)

    def test_ausencia_de_status_online_sem_comprovacao(self) -> None:
        self.service.start(101, "alice")

        response = self.service.handle_callback(101, "alice", "v1:c")

        self.assertIn("Monitor de sinais:\n⚪ Aguardando inicialização", response.text)
        self.assertNotIn("Monitor de sinais:\n🟢 Online", response.text)

    def test_monitor_online_com_heartbeat_recente(self) -> None:
        self.service.start(101, "alice")
        update_service_heartbeat(
            self.database_path,
            SIGNAL_MONITOR_SERVICE_NAME,
            details="telegram_authorized",
        )

        response = self.service.handle_callback(101, "alice", "v1:c")

        self.assertIn("Monitor de sinais:\n🟢 Online", response.text)

    def test_monitor_offline_com_heartbeat_vencido(self) -> None:
        self.service.start(101, "alice")
        update_service_heartbeat(self.database_path, SIGNAL_MONITOR_SERVICE_NAME)
        with connect_database(self.database_path) as connection:
            connection.execute(
                "UPDATE service_heartbeats SET heartbeat_at = ? WHERE service_name = ?",
                ("2020-01-01T00:00:00+00:00", SIGNAL_MONITOR_SERVICE_NAME),
            ).close()

        response = self.service.handle_callback(101, "alice", "v1:c")

        self.assertIn("Monitor de sinais:\n🔴 Offline", response.text)

    def test_nenhum_token_ou_credencial_exposto(self) -> None:
        self.service.start(101, "alice")
        responses = [
            self.service.menu(101, "alice"),
            self.service.handle_callback(101, "alice", "v1:a"),
            self.service.handle_callback(101, "alice", "v1:c"),
        ]
        forbidden = ("TELEGRAM_BOT_TOKEN", "API_HASH", "123456:secret", "token")

        for response in responses:
            for value in forbidden:
                self.assertNotIn(value, response.text)


if __name__ == "__main__":
    unittest.main()
