from __future__ import annotations

from decimal import Decimal
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from telegram_mt5_copier.credential_service import CredentialService
from telegram_mt5_copier.command_queue import CommandQueue
from telegram_mt5_copier.database import connect_database
from telegram_mt5_copier.mt5.account_service import MT5AccountForm, MT5AccountService
from telegram_mt5_copier.mt5.account_worker import AccountWorkerLock, MT5AccountWorker, WorkerAlreadyRunningError
from telegram_mt5_copier.mt5.client import MT5Client, SimulatedMT5Client
from telegram_mt5_copier.mt5.execution_simulator import ExecutionSimulator
from telegram_mt5_copier.mt5.models import CONNECTION_STATUS_CONNECTED, SymbolInfo
from telegram_mt5_copier.mt5.terminal_manager import TerminalManager
from telegram_mt5_copier.parser import parse_signal_text
from telegram_mt5_copier.users import USER_STATUS_ACTIVE, UserRepository
from telegram_mt5_copier.web_app import (
    CSRFTokenService,
    MT5OnboardingService,
    WebAppValidationError,
    build_signed_init_data,
    validate_telegram_web_app_init_data,
)
from tests.access_helpers import grant_paid_access


BUY_SIGNAL = """XAUUSD BUY
ENTRY 4059-4061
SL 4044
TP 4066
TP 4071
TP 4076
TP 4096
"""


class MT5OnboardingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.database_path = self.root / "bot.sqlite3"
        self.credential_service = CredentialService(CredentialService.generate_key())
        self.users = UserRepository(self.database_path)
        self.terminal_manager = TerminalManager(self.root / "mt5_accounts")
        self.client = SimulatedMT5Client()
        self.accounts = MT5AccountService(
            self.database_path,
            credential_service=self.credential_service,
            terminal_manager=self.terminal_manager,
            client_factory=lambda: self.client,
        )

    def tearDown(self) -> None:
        for service in (self.accounts, self.users):
            service.close()
        self.temp_dir.cleanup()

    def create_user(self, telegram_user_id: int = 101):
        return self.users.get_or_create_user(telegram_user_id, "alice")

    def create_account(self, user_id: int, login: str = "12345678"):
        return self.accounts.register_account(
            user_id,
            MT5AccountForm(
                broker_name="Broker",
                server_name="Broker-Demo",
                login=login,
                password="mt5-secret-password",
                account_alias="Conta demo",
            ),
        )

    def test_criptografia_e_descriptografia(self) -> None:
        encrypted = self.credential_service.encrypt_password("senha-mt5")

        self.assertNotEqual(encrypted, "senha-mt5")
        self.assertEqual(self.credential_service.decrypt_password(encrypted), "senha-mt5")

    def test_senha_nao_aparece_em_repr(self) -> None:
        form = MT5AccountForm("Broker", "Server", "1234", "senha-mt5", "Demo")

        self.assertNotIn("senha-mt5", repr(form))
        self.assertNotIn(self.credential_service.primary_key, repr(self.credential_service))

    def test_validacao_de_web_app_init_data(self) -> None:
        token = "123456:bot-token"
        init_data = build_signed_init_data(
            token,
            {
                "query_id": "abc",
                "auth_date": "1000",
                "user": json.dumps({"id": 101, "username": "alice"}, separators=(",", ":")),
            },
        )

        parsed = validate_telegram_web_app_init_data(init_data, token, now=1001)

        self.assertEqual(parsed.user.id, 101)
        self.assertEqual(parsed.user.username, "alice")

    def test_rejeicao_de_init_data_invalido(self) -> None:
        with self.assertRaises(WebAppValidationError):
            validate_telegram_web_app_init_data("auth_date=1000&hash=bad", "123456:bot-token", now=1001)

    def test_replay_de_init_data_e_bloqueado(self) -> None:
        token = "123456:bot-token"
        init_data = build_signed_init_data(
            token,
            {
                "query_id": "abc",
                "auth_date": "1000",
                "user": json.dumps({"id": 101}, separators=(",", ":")),
            },
        )
        replay_cache: set[str] = set()

        validate_telegram_web_app_init_data(init_data, token, now=1001, replay_cache=replay_cache)
        with self.assertRaises(WebAppValidationError):
            validate_telegram_web_app_init_data(init_data, token, now=1001, replay_cache=replay_cache)

    def test_init_data_com_auth_date_futuro_e_rejeitado(self) -> None:
        token = "123456:bot-token"
        init_data = build_signed_init_data(
            token,
            {
                "query_id": "abc",
                "auth_date": "2000",
                "user": json.dumps({"id": 101}, separators=(",", ":")),
            },
        )

        with self.assertRaises(WebAppValidationError):
            validate_telegram_web_app_init_data(init_data, token, now=1000)

    def test_cadastro_de_conta_mascara_login_e_criptografa_senha(self) -> None:
        user = self.create_user()
        form = MT5AccountForm("Broker", "Broker-Demo", "12345678", "mt5-secret-password", "Demo")

        account = self.accounts.register_account(user.id, form)

        self.assertEqual(account.masked_login, "••••5678")
        self.assertEqual(account.connection_status, CONNECTION_STATUS_CONNECTED)
        self.assertNotEqual(account.encrypted_password, "mt5-secret-password")
        self.assertNotIn("mt5-secret-password", repr(account))
        self.assertEqual(form.password, "")
        self.assertEqual(account.balance, Decimal("10000"))
        self.assertEqual(account.equity, Decimal("10000"))
        self.assertEqual(account.server_name, "Broker-Demo")

    def test_isolamento_entre_usuarios(self) -> None:
        alice = self.create_user(101)
        bob = self.users.get_or_create_user(202, "bob")
        account = self.create_account(alice.id)

        with self.assertRaises(ValueError):
            self.accounts.get_account(bob.id, account.id)

    def test_conta_habilitada_e_conectada_tem_prioridade_no_bot(self) -> None:
        user = self.create_user()
        old_account = self.accounts.register_account(
            user.id,
            MT5AccountForm("Broker", "Broker-Demo", "11111111", "secret", "Antiga"),
        )
        active_account = self.accounts.register_account(
            user.id,
            MT5AccountForm("Broker", "Broker-Demo", "22222222", "secret", "Ativa"),
        )
        with connect_database(self.database_path) as connection:
            connection.execute(
                "UPDATE execution_profiles SET enabled = 0 WHERE mt5_account_id = ?",
                (old_account.id,),
            )

        selected = self.accounts.first_account(user.id)

        self.assertIsNotNone(selected)
        self.assertEqual(selected.id, active_account.id)

    def test_remocao_de_conta_apaga_senha_criptografada(self) -> None:
        user = self.create_user()
        account = self.create_account(user.id)

        self.accounts.remove_account(user.id, account.id)

        self.assertEqual(self.accounts.list_accounts(user.id), [])

    def test_bloqueio_de_conta_real(self) -> None:
        real_accounts = MT5AccountService(
            self.database_path,
            credential_service=self.credential_service,
            terminal_manager=self.terminal_manager,
            client_factory=lambda: SimulatedMT5Client(account_type="real"),
        )
        user = self.create_user()

        try:
            with self.assertRaises(ValueError):
                real_accounts.register_account(
                    user.id,
                    MT5AccountForm("Broker", "Broker-Real", "998877", "secret", "Real"),
                )
            self.assertEqual(real_accounts.list_accounts(user.id), [])
        finally:
            real_accounts.close()

    def test_terminal_isolado_por_account_id(self) -> None:
        provisioned = self.terminal_manager.provision_account(44)

        self.assertEqual(provisioned.account_dir, self.root / "mt5_accounts" / "44")
        self.assertEqual(provisioned.terminal_path, provisioned.account_dir / "terminal64.exe")
        self.assertEqual(provisioned.data_dir, provisioned.account_dir / "data")
        self.assertEqual(provisioned.logs_dir, provisioned.account_dir / "logs")
        self.assertTrue(provisioned.account_dir.is_dir())
        self.assertTrue(provisioned.data_dir.is_dir())
        self.assertTrue(provisioned.logs_dir.is_dir())

    def test_template_copiado_nao_herda_sessao_bases_ou_logs(self) -> None:
        template = self.root / "template"
        (template / "config").mkdir(parents=True)
        (template / "bases" / "Broker").mkdir(parents=True)
        (template / "logs").mkdir(parents=True)
        (template / "MQL5" / "Logs").mkdir(parents=True)
        (template / "terminal64.exe").write_bytes(b"terminal")
        (template / "config" / "accounts.dat").write_bytes(b"admin-session")
        (template / "config" / "common.ini").write_text(
            "\n".join(
                (
                    "[Common]",
                    "Login=87812436",
                    "Password=secret",
                    "KeepPrivate=1",
                    "",
                    "[Experts]",
                    "Enabled=1",
                    "AllowLiveTrading=1",
                    "Account=1",
                    "Profile=1",
                    "DisablePythonAPI=0",
                )
            ),
            encoding="utf-16",
        )
        (template / "config" / "terminal.ini").write_bytes(b"runtime")
        (template / "bases" / "Broker" / "history.dat").write_bytes(b"history")
        (template / "logs" / "old.log").write_text("admin log", encoding="utf-8")
        (template / "MQL5" / "Logs" / "old.log").write_text("expert log", encoding="utf-8")
        manager = TerminalManager(self.root / "isolated", template)

        provisioned = manager.provision_account(77)

        self.assertFalse((provisioned.account_dir / "config" / "accounts.dat").exists())
        self.assertFalse((provisioned.account_dir / "config" / "terminal.ini").exists())
        safe_config = (provisioned.account_dir / "config" / "common.ini").read_text(
            encoding="utf-16"
        )
        self.assertIn("KeepPrivate=0", safe_config)
        self.assertIn("DisablePythonAPI=0", safe_config)
        self.assertIn("Account=0", safe_config)
        self.assertNotIn("87812436", safe_config)
        self.assertNotIn("secret", safe_config)
        self.assertFalse((provisioned.account_dir / "bases").exists())
        self.assertFalse((provisioned.logs_dir / "old.log").exists())
        self.assertFalse((provisioned.account_dir / "MQL5" / "Logs").exists())
        self.assertTrue((template / "config" / "accounts.dat").exists())

    def test_higienizacao_de_legado_acontece_apenas_uma_vez(self) -> None:
        account_dir = self.terminal_manager.account_dir(78)
        (account_dir / "config").mkdir(parents=True)
        (account_dir / "bases").mkdir()
        (account_dir / "config" / "accounts.dat").write_bytes(b"legacy-session")
        (account_dir / "bases" / "legacy.dat").write_bytes(b"legacy")

        self.terminal_manager.provision_account(78, sanitize_legacy=True)

        self.assertFalse((account_dir / "config" / "accounts.dat").exists())
        self.assertFalse((account_dir / "bases").exists())
        marker = account_dir / ".telegram-mt5-sanitized"
        self.assertTrue(marker.exists())

        (account_dir / "bases").mkdir()
        (account_dir / "bases" / "new-account.dat").write_bytes(b"current")
        self.terminal_manager.provision_account(78, sanitize_legacy=True)

        self.assertTrue((account_dir / "bases" / "new-account.dat").exists())

    def test_mt5_client_inicializa_em_modo_portable(self) -> None:
        fake_mt5 = FakeMT5Module()
        client = MT5Client(fake_mt5)

        self.assertTrue(client.initialize(Path("terminal64.exe"), 1234, "secret", "Broker-Demo"))

        self.assertTrue(fake_mt5.initialize_kwargs["portable"])
        self.assertEqual(fake_mt5.initialize_kwargs["path"], "terminal64.exe")
        self.assertEqual(fake_mt5.initialize_kwargs["login"], 1234)
        self.assertEqual(fake_mt5.initialize_kwargs["password"], "secret")
        self.assertEqual(fake_mt5.initialize_kwargs["server"], "Broker-Demo")
        self.assertEqual(fake_mt5.call_order, ["initialize"])
        client.shutdown()

    def test_mt5_client_registra_last_error_quando_login_falha(self) -> None:
        fake_mt5 = FakeMT5Module(initialize_result=False, last_error=(10004, "server not found"))
        client = MT5Client(fake_mt5)

        self.assertFalse(client.initialize(Path("terminal64.exe"), 1234, "secret", "Broker-Demo"))

        self.assertEqual(client.last_error(), (10004, "server not found"))
        client.shutdown()

    def test_cadastro_repete_initialize_apos_ipc_timeout_transitorio(self) -> None:
        user = self.create_user()
        client = TransientIPCClient()
        self.accounts.client_factory = lambda: client

        with patch("telegram_mt5_copier.mt5.account_service.time.sleep") as sleep:
            account = self.create_account(user.id)

        self.assertEqual(account.connection_status, "connected")
        self.assertEqual(client.initialize_attempts, 2)
        sleep.assert_called_once_with(3)

    def test_nova_tentativa_reutiliza_conta_falha_sem_criar_duplicata(self) -> None:
        user = self.create_user()
        accounts = MT5AccountService(
            self.database_path,
            credential_service=self.credential_service,
            terminal_manager=self.terminal_manager,
            client_factory=FailingLoginClient,
        )
        try:
            failed = accounts.register_account(
                user.id,
                MT5AccountForm("HFM", "Servidor digitado errado", "445566", "old", "Beatriz"),
                keep_on_connection_failure=True,
            )
            accounts.client_factory = SimulatedMT5Client

            connected = accounts.register_account(
                user.id,
                MT5AccountForm("HFM", "HFMarketsGlobal-Live3", "445566", "new", "Beatriz"),
                keep_on_connection_failure=True,
            )

            self.assertEqual(connected.id, failed.id)
            self.assertEqual(connected.connection_status, "connected")
            self.assertEqual(connected.server_name, "HFMarketsGlobal-Live3")
            self.assertEqual(accounts.count_accounts(), 1)
            self.assertEqual(
                self.credential_service.decrypt_password(connected.encrypted_password),
                "new",
            )
        finally:
            accounts.close()

    def test_falha_de_conexao_salva_last_error_sem_senha(self) -> None:
        user = self.create_user()
        failing_client = FailingLoginClient()
        accounts = MT5AccountService(
            self.database_path,
            credential_service=self.credential_service,
            terminal_manager=self.terminal_manager,
            client_factory=lambda: failing_client,
        )

        try:
            with self.assertRaises(ValueError) as context:
                accounts.register_account(
                    user.id,
                    MT5AccountForm("Broker", "Broker-Demo", "445566", "mt5-secret-password", "Demo"),
                )
            self.assertIn("server not found", str(context.exception))
            self.assertNotIn("mt5-secret-password", str(context.exception))
        finally:
            accounts.close()

    def test_falha_de_conexao_pode_manter_conta_para_reteste_manual(self) -> None:
        user = self.create_user()
        failing_client = FailingLoginClient()
        accounts = MT5AccountService(
            self.database_path,
            credential_service=self.credential_service,
            terminal_manager=self.terminal_manager,
            client_factory=lambda: failing_client,
        )

        try:
            account = accounts.register_account(
                user.id,
                MT5AccountForm("Broker", "Broker-Demo", "445566", "mt5-secret-password", "Demo"),
                keep_on_connection_failure=True,
            )
            accounts_for_user = accounts.list_accounts(user.id)

            self.assertEqual(len(accounts_for_user), 1)
            self.assertEqual(accounts_for_user[0].id, account.id)
            self.assertEqual(account.connection_status, "failed")
            self.assertIn("server not found", account.last_error or "")
            self.assertNotIn("mt5-secret-password", account.last_error or "")
            self.assertIsNotNone(account.terminal_path)
            self.assertTrue(account.terminal_path.parent.is_dir())
        finally:
            accounts.close()

    def test_lock_de_worker(self) -> None:
        provisioned = self.terminal_manager.provision_account(55)
        first = AccountWorkerLock(provisioned.account_dir)
        second = AccountWorkerLock(provisioned.account_dir)

        try:
            first.acquire()
            with self.assertRaises(WorkerAlreadyRunningError):
                second.acquire()
        finally:
            first.close()
            second.close()

    def test_worker_atualiza_heartbeat(self) -> None:
        user = self.create_user()
        account = self.create_account(user.id)
        worker = MT5AccountWorker(accounts=self.accounts, account=account)

        try:
            self.assertTrue(worker.process_once())
            updated = self.accounts.get_account(user.id, account.id)
        finally:
            worker.close()

        self.assertIsNotNone(updated.worker_heartbeat_at)
        self.assertFalse((account.terminal_path.parent / "worker.lock").exists())

    def test_worker_reconecta_com_backoff(self) -> None:
        user = self.create_user()
        account = self.create_account(user.id)
        attempts = {"count": 0}

        def client_factory():
            attempts["count"] += 1
            return FlakyConnectionClient(fail=attempts["count"] == 1)

        flaky_accounts = MT5AccountService(
            self.database_path,
            credential_service=self.credential_service,
            terminal_manager=self.terminal_manager,
            client_factory=client_factory,
        )
        worker = MT5AccountWorker(accounts=flaky_accounts, account=account, backoff_sequence=(1, 2, 5))

        try:
            self.assertFalse(worker.process_once())
            self.assertEqual(worker.current_backoff_seconds, 2)
            self.assertTrue(worker.process_once())
            self.assertEqual(worker.current_backoff_seconds, 1)
        finally:
            worker.close()
            flaky_accounts.close()

    def test_worker_processa_apenas_comandos_da_propria_conta(self) -> None:
        user = self.create_user()
        first = self.create_account(user.id, "111111")
        second = self.create_account(user.id, "222222")
        queue = CommandQueue(self.database_path)
        first_command = queue.enqueue(user.id, "test_mt5_connection", {"mt5_account_id": first.id})
        second_command = queue.enqueue(user.id, "test_mt5_connection", {"mt5_account_id": second.id})
        worker = MT5AccountWorker(accounts=self.accounts, account=first, command_queue=queue)

        try:
            processed = worker.process_commands()
        finally:
            worker.close()
            queue.close()

        self.assertEqual(processed, 1)
        self.assertEqual(command_status(self.database_path, first_command.id), "done")
        self.assertEqual(command_status(self.database_path, second_command.id), "pending")

    def test_execucao_simulada_divide_tps(self) -> None:
        user = self.create_user()
        grant_paid_access(self.database_path, user.id)
        account = self.create_account(user.id)
        self.accounts.update_execution_profile_fixed_lot(user.id, account.id, Decimal("0.04"))
        signal = parse_signal_text(BUY_SIGNAL).signal
        simulator = ExecutionSimulator(self.database_path, self.accounts, client_factory=lambda: self.client)

        try:
            results = simulator.simulate_for_signal(signal)
        finally:
            simulator.close()

        self.assertEqual(len(results), 1)
        self.assertFalse(results[0].duplicate)
        self.assertEqual(len(results[0].executions), 4)
        self.assertEqual(results[0].executions[0].requested_volume, Decimal("0.01"))
        self.assertIn("Nenhuma ordem foi enviada", results[0].message)
        self.assertFalse(self.client.order_send_called)

    def test_lote_invalido_e_rejeitado(self) -> None:
        user = self.create_user()
        grant_paid_access(self.database_path, user.id)
        account = self.create_account(user.id)
        self.accounts.update_execution_profile_fixed_lot(user.id, account.id, Decimal("0.03"))
        signal = parse_signal_text(BUY_SIGNAL).signal
        simulator = ExecutionSimulator(
            self.database_path,
            self.accounts,
            client_factory=lambda: SimulatedMT5Client(symbol_info=SymbolInfo(name="XAUUSD")),
        )

        try:
            with self.assertRaises(ValueError):
                simulator.simulate_for_signal(signal)
        finally:
            simulator.close()

    def test_sinal_duplicado_nao_cria_novas_execucoes(self) -> None:
        user = self.create_user()
        grant_paid_access(self.database_path, user.id)
        account = self.create_account(user.id)
        self.accounts.update_execution_profile_fixed_lot(user.id, account.id, Decimal("0.04"))
        signal = parse_signal_text(BUY_SIGNAL).signal
        simulator = ExecutionSimulator(self.database_path, self.accounts, client_factory=lambda: self.client)

        try:
            first = simulator.simulate_for_signal(signal)
            second = simulator.simulate_for_signal(signal)
        finally:
            simulator.close()

        self.assertEqual(len(first[0].executions), 4)
        self.assertTrue(second[0].duplicate)
        self.assertEqual(count_executions(self.database_path), 4)

    def test_onboarding_service_cadastra_conta_sem_senha_em_resposta(self) -> None:
        token = "123456:bot-token"
        csrf = CSRFTokenService("csrf-secret")
        now = int(time.time())
        init_data = build_signed_init_data(
            token,
            {
                "query_id": "abc",
                "auth_date": str(now),
                "user": json.dumps({"id": 101, "username": "alice"}, separators=(",", ":")),
            },
        )

        service = MT5OnboardingService(
            bot_token=token,
            users=self.users,
            accounts=self.accounts,
            csrf=csrf,
            require_https=True,
        )
        result = service.submit_account_form(
            init_data=init_data,
            csrf_token=csrf.issue(101, now=now),
            request_scheme="https",
            broker_name="Broker",
            server_name="Broker-Demo",
            login="12345678",
            password="mt5-secret-password",
            account_alias="Demo",
        )

        self.assertEqual(result.masked_login, "••••5678")
        self.assertNotIn("mt5-secret-password", repr(result))

    def test_shutdown_e_close_idempotentes(self) -> None:
        self.client.shutdown()
        self.client.shutdown()
        self.accounts.close()
        self.accounts.close()


def count_executions(database_path: Path) -> int:
    with connect_database(database_path) as connection:
        cursor = connection.execute("SELECT COUNT(*) FROM executions")
        try:
            return int(cursor.fetchone()[0])
        finally:
            cursor.close()


class FlakyConnectionClient(SimulatedMT5Client):
    def __init__(self, *, fail: bool) -> None:
        super().__init__()
        self.fail = fail

    def account_info(self):
        if self.fail:
            return None
        return super().account_info()


class TransientIPCClient(SimulatedMT5Client):
    def __init__(self) -> None:
        super().__init__()
        self.initialize_attempts = 0

    def initialize(
        self,
        terminal_path: Path,
        login: int,
        password: str,
        server: str,
        *,
        timeout_ms: int = 60000,
    ) -> bool:
        self.initialize_attempts += 1
        if self.initialize_attempts == 1:
            self._last_error = (-10005, "IPC timeout")
            return False
        return super().initialize(
            terminal_path,
            login,
            password,
            server,
            timeout_ms=timeout_ms,
        )


class FailingLoginClient(SimulatedMT5Client):
    def initialize(self, terminal_path: Path, login: int, password: str, server: str, *, timeout_ms: int = 60000) -> bool:
        _ = terminal_path
        _ = login
        _ = password
        _ = server
        _ = timeout_ms
        self._last_error = (10004, "server not found")
        return False


class FakeMT5Module:
    def __init__(self, *, initialize_result: bool = True, login_result: bool = True, last_error: object = (0, "ok")) -> None:
        self.initialize_kwargs: dict[str, object] = {}
        self.login_args: tuple[object, ...] = ()
        self.login_kwargs: dict[str, object] = {}
        self.shutdown_called = False
        self.initialize_result = initialize_result
        self.login_result = login_result
        self._last_error = last_error
        self.call_order: list[str] = []

    def initialize(self, **kwargs):
        self.call_order.append("initialize")
        self.initialize_kwargs = kwargs
        return self.initialize_result

    def login(self, *args, **kwargs):
        self.call_order.append("login")
        self.login_args = args
        self.login_kwargs = kwargs
        return self.login_result

    def last_error(self):
        return self._last_error

    def shutdown(self):
        self.shutdown_called = True


def command_status(database_path: Path, command_id: int) -> str:
    with connect_database(database_path) as connection:
        cursor = connection.execute("SELECT status FROM commands WHERE id = ?", (command_id,))
        try:
            row = cursor.fetchone()
        finally:
            cursor.close()
    return str(row[0])


if __name__ == "__main__":
    unittest.main()
