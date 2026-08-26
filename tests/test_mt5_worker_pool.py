from __future__ import annotations

import logging
from pathlib import Path
import tempfile
import unittest

from telegram_mt5_copier.credential_service import CredentialService
from telegram_mt5_copier.mt5.account_service import MT5AccountForm, MT5AccountService
from telegram_mt5_copier.mt5.client import SimulatedMT5Client
from telegram_mt5_copier.mt5.terminal_manager import TerminalManager
import telegram_mt5_copier.mt5_worker_pool as mt5_worker_pool_module
from telegram_mt5_copier.mt5_worker_pool import HANG_TIMEOUT_SECONDS, MT5WorkerPool
from telegram_mt5_copier.users import UserRepository


class FakeProcess:
    next_pid = 1000

    def __init__(self) -> None:
        self.pid = FakeProcess.next_pid
        FakeProcess.next_pid += 1
        self.return_code: int | None = None
        self.terminated = False

    def poll(self) -> int | None:
        return self.return_code

    def terminate(self) -> None:
        self.terminated = True
        self.return_code = 0

    def kill(self) -> None:
        self.return_code = -9

    def wait(self, timeout: int) -> int:
        assert timeout > 0
        return int(self.return_code or 0)


class MT5WorkerPoolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.database_path = self.root / "pool.sqlite3"
        self.users = UserRepository(self.database_path)
        self.accounts = MT5AccountService(
            self.database_path,
            credential_service=CredentialService(CredentialService.generate_key()),
            terminal_manager=TerminalManager(self.root / "mt5"),
            client_factory=lambda: SimulatedMT5Client(),
        )
        self.now = 0.0
        self.created: list[tuple[FakeProcess, tuple]] = []

        def factory(args, **kwargs):
            process = FakeProcess()
            self.created.append((process, tuple(args)))
            return process

        self.pool = MT5WorkerPool(
            project_root=self.root,
            log_dir=self.root / "logs",
            database_path=self.database_path,
            python_executable=Path("python"),
            logger=logging.getLogger("test-mt5-worker-pool"),
            popen_factory=factory,
            clock=lambda: self.now,
            sleeper=lambda _seconds: None,
        )

    def tearDown(self) -> None:
        self.pool.stop_all()
        self.accounts.close()
        self.users.close()
        self.temp_dir.cleanup()

    def test_um_worker_por_conta_ativa(self) -> None:
        from tests.access_helpers import grant_paid_access

        user_a = self.users.get_or_create_user(101, "alice")
        grant_paid_access(self.database_path, user_a.id)
        account_a = self.accounts.register_account(
            user_a.id, MT5AccountForm("Broker", "Broker-Demo", "11111111", "secret", "A")
        )
        user_b = self.users.get_or_create_user(202, "bob")
        grant_paid_access(self.database_path, user_b.id)
        account_b = self.accounts.register_account(
            user_b.id, MT5AccountForm("Broker", "Broker-Demo", "22222222", "secret", "B")
        )

        self.pool.tick()

        self.assertEqual(len(self.created), 2)
        started_ids = {int(args[-1]) for _process, args in self.created}
        self.assertEqual(started_ids, {account_a.id, account_b.id})
        self.assertEqual(set(self.pool.children), {account_a.id, account_b.id})

    def test_worker_travado_sem_heartbeat_e_reiniciado(self) -> None:
        from tests.access_helpers import grant_paid_access

        user = self.users.get_or_create_user(303, "carla")
        grant_paid_access(self.database_path, user.id)
        account = self.accounts.register_account(
            user.id, MT5AccountForm("Broker", "Broker-Demo", "33333333", "secret", "C")
        )

        self.pool.tick()
        self.assertEqual(len(self.created), 1)
        first_process = self.created[0][0]

        # Same heartbeat (None -> None, never progressed) for longer than
        # the hang timeout: the pool must kill and restart it, exactly the
        # scenario a hung MT5 IPC call produces (no exception, no exit).
        self.now += HANG_TIMEOUT_SECONDS + 1
        self.pool.tick()

        self.assertTrue(first_process.terminated)
        self.assertIsNone(self.pool.children[account.id].process)

        # Backoff elapses; the pool spawns a fresh process for the account.
        self.now += 5
        self.pool.tick()
        self.assertEqual(len(self.created), 2)

    def test_worker_com_heartbeat_progredindo_nao_e_reiniciado(self) -> None:
        from tests.access_helpers import grant_paid_access

        user = self.users.get_or_create_user(404, "dora")
        grant_paid_access(self.database_path, user.id)
        account = self.accounts.register_account(
            user.id, MT5AccountForm("Broker", "Broker-Demo", "44444444", "secret", "D")
        )

        self.pool.tick()
        process = self.created[0][0]

        for step in range(1, 6):
            self.now += HANG_TIMEOUT_SECONDS - 1
            self.accounts.update_worker_heartbeat(user.id, account.id)
            self.pool.tick()

        self.assertFalse(process.terminated)
        self.assertEqual(len(self.created), 1)

    def test_reinicio_por_travamento_encerra_o_terminal_orfao(self) -> None:
        from tests.access_helpers import grant_paid_access

        user = self.users.get_or_create_user(606, "fabio")
        grant_paid_access(self.database_path, user.id)
        account = self.accounts.register_account(
            user.id, MT5AccountForm("Broker", "Broker-Demo", "66666666", "secret", "F")
        )
        terminal_path = self.root / "mt5" / "6" / "terminal64.exe"
        self.accounts.update_terminal_path(user.id, account.id, terminal_path)

        terminated_paths: list[Path] = []
        original = mt5_worker_pool_module.terminate_process_by_path
        mt5_worker_pool_module.terminate_process_by_path = terminated_paths.append
        try:
            self.pool.tick()
            self.now += HANG_TIMEOUT_SECONDS + 1
            self.pool.tick()
        finally:
            mt5_worker_pool_module.terminate_process_by_path = original

        # subprocess.terminate()/kill() only reaches the worker's own PID;
        # MetaTrader5 launches terminal64.exe as a separate process outside
        # our tree, so it is left running unless the pool kills it too. A
        # leftover terminal keeps this account's folder locked, so the next
        # worker can never open its own terminal64.exe there and the account
        # is stuck erroring forever -- restart after restart.
        self.assertEqual(terminated_paths, [terminal_path])

    def test_conta_pausada_encerra_o_worker(self) -> None:
        from tests.access_helpers import grant_paid_access
        from telegram_mt5_copier.users import USER_STATUS_PAUSED

        user = self.users.get_or_create_user(505, "erik")
        grant_paid_access(self.database_path, user.id)
        self.accounts.register_account(
            user.id, MT5AccountForm("Broker", "Broker-Demo", "55555555", "secret", "E")
        )

        self.pool.tick()
        self.assertEqual(len(self.created), 1)
        process = self.created[0][0]

        self.users.set_status(user.id, USER_STATUS_PAUSED)
        self.pool.tick()

        self.assertTrue(process.terminated)
        self.assertEqual(self.pool.children, {})


if __name__ == "__main__":
    unittest.main()
