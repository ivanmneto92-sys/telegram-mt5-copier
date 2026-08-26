from __future__ import annotations

import logging
from pathlib import Path
import tempfile
import unittest

from telegram_mt5_copier.supervisor import (
    PlatformSupervisor,
    ServiceSpec,
    SupervisorAlreadyRunningError,
    SupervisorLock,
    restart_delay,
)


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


class SupervisorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.now = 0.0
        self.created: list[FakeProcess] = []

        def factory(*_args: object, **_kwargs: object) -> FakeProcess:
            process = FakeProcess()
            self.created.append(process)
            return process

        self.supervisor = PlatformSupervisor(
            project_root=self.root,
            log_dir=self.root / "logs",
            services=(ServiceSpec("bot", ("fake-bot",)),),
            logger=logging.getLogger("test-supervisor"),
            popen_factory=factory,
            clock=lambda: self.now,
            sleeper=lambda _seconds: None,
        )

    def tearDown(self) -> None:
        self.supervisor.stop_all()
        self.temp_dir.cleanup()

    def test_reinicia_processo_com_backoff_progressivo(self) -> None:
        self.supervisor.tick()
        first = self.created[0]
        first.return_code = 2
        self.supervisor.tick()

        self.assertEqual(len(self.created), 1)
        self.now = 0.9
        self.supervisor.tick()
        self.assertEqual(len(self.created), 1)
        self.now = 1.0
        self.supervisor.tick()
        self.assertEqual(len(self.created), 2)

        second = self.created[1]
        second.return_code = 2
        self.supervisor.tick()
        self.now = 2.9
        self.supervisor.tick()
        self.assertEqual(len(self.created), 2)
        self.now = 3.0
        self.supervisor.tick()
        self.assertEqual(len(self.created), 3)

    def test_processo_estavel_reinicia_backoff(self) -> None:
        self.supervisor.tick()
        self.now = 301
        self.created[0].return_code = 1
        self.supervisor.tick()

        state = self.supervisor.states["bot"]
        self.assertEqual(state.consecutive_failures, 1)
        self.assertEqual(state.next_start_at, 302)

    def test_encerramento_para_filhos(self) -> None:
        self.supervisor.tick()
        process = self.created[0]

        self.supervisor.stop_all()

        self.assertTrue(process.terminated)
        self.assertIsNone(self.supervisor.states["bot"].process)

    def test_backoff_tem_limite(self) -> None:
        self.assertEqual(restart_delay(1), 1)
        self.assertEqual(restart_delay(3), 5)
        self.assertEqual(restart_delay(100), 60)

    def test_lock_rejeita_segunda_instancia(self) -> None:
        first = SupervisorLock(self.root / "supervisor.lock")
        second = SupervisorLock(self.root / "supervisor.lock")
        first.acquire()
        try:
            with self.assertRaises(SupervisorAlreadyRunningError):
                second.acquire()
        finally:
            first.close()

    def test_lock_com_pid_morto_nao_bloqueia_nova_instancia(self) -> None:
        # A leftover lock file naming a PID that isn't running anymore (the
        # previous owner crashed, or the whole machine rebooted) must not
        # block a fresh acquire -- that produced exactly this on a real VPS
        # after a reboot: SupervisorAlreadyRunningError forever, because the
        # stale PID from before the reboot happened to already be reused by
        # an unrelated process by the time the service came back up.
        lock_path = self.root / "supervisor.lock"
        lock_path.write_text("999999999", encoding="ascii")

        lock = SupervisorLock(lock_path)
        try:
            lock.acquire()
        finally:
            lock.close()

    def test_alerta_falha_uma_vez_e_recuperacao_apos_estabilidade(self) -> None:
        alerts: list[str] = []
        self.supervisor.alert_callback = lambda message: not alerts.append(message)
        self.supervisor.tick()
        self.created[0].return_code = 2
        self.supervisor.tick()
        self.assertEqual(len(alerts), 1)
        self.assertIn("ALERTA OPERACIONAL", alerts[0])

        self.now = 1
        self.supervisor.tick()
        self.created[1].return_code = 2
        self.supervisor.tick()
        self.assertEqual(len(alerts), 1)

        self.now = 3
        self.supervisor.tick()
        self.now = 304
        self.supervisor.tick()
        self.assertEqual(len(alerts), 2)
        self.assertIn("SERVIÇO RECUPERADO", alerts[1])
