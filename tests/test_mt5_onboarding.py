from __future__ import annotations

from decimal import Decimal
import json
from pathlib import Path
import tempfile
import time
import unittest

from telegram_mt5_copier.credential_service import CredentialService
from telegram_mt5_copier.database import connect_database
from telegram_mt5_copier.mt5.account_service import MT5AccountForm, MT5AccountService
from telegram_mt5_copier.mt5.account_worker import AccountWorkerLock, WorkerAlreadyRunningError
from telegram_mt5_copier.mt5.client import SimulatedMT5Client
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

    def test_cadastro_de_conta_mascara_login_e_criptografa_senha(self) -> None:
        user = self.create_user()
        form = MT5AccountForm("Broker", "Broker-Demo", "12345678", "mt5-secret-password", "Demo")

        account = self.accounts.register_account(user.id, form)

        self.assertEqual(account.masked_login, "••••5678")
        self.assertEqual(account.connection_status, CONNECTION_STATUS_CONNECTED)
        self.assertNotEqual(account.encrypted_password, "mt5-secret-password")
        self.assertNotIn("mt5-secret-password", repr(account))
        self.assertEqual(form.password, "")

    def test_isolamento_entre_usuarios(self) -> None:
        alice = self.create_user(101)
        bob = self.users.get_or_create_user(202, "bob")
        account = self.create_account(alice.id)

        with self.assertRaises(ValueError):
            self.accounts.get_account(bob.id, account.id)

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
        self.assertTrue(provisioned.account_dir.is_dir())

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

    def test_execucao_simulada_divide_tps(self) -> None:
        user = self.create_user()
        self.users.set_status(user.id, USER_STATUS_ACTIVE)
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
        self.users.set_status(user.id, USER_STATUS_ACTIVE)
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
        self.users.set_status(user.id, USER_STATUS_ACTIVE)
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


if __name__ == "__main__":
    unittest.main()
