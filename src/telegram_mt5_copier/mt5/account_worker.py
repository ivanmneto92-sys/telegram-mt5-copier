from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path
from typing import Callable

from ..command_queue import Command, CommandQueue
from .account_service import MT5AccountService
from .models import MT5Account
from .ipc_lock import clear_stale_lock


class WorkerAlreadyRunningError(RuntimeError):
    pass


class AccountWorkerLock:
    def __init__(self, account_dir: Path) -> None:
        self.account_dir = account_dir
        self.lock_path = account_dir / "worker.lock"
        self.heartbeat_path = account_dir / "heartbeat.txt"
        self._fd: int | None = None

    def acquire(self) -> None:
        self.account_dir.mkdir(parents=True, exist_ok=True)
        if not clear_stale_lock(self.lock_path):
            raise WorkerAlreadyRunningError("Worker MT5 ja esta ativo para esta conta.")
        try:
            self._fd = os.open(str(self.lock_path), os.O_CREAT | os.O_EXCL | os.O_RDWR)
        except FileExistsError as exc:
            raise WorkerAlreadyRunningError("Worker MT5 ja esta ativo para esta conta.") from exc
        os.write(self._fd, str(os.getpid()).encode("ascii"))
        self.heartbeat()

    def heartbeat(self) -> None:
        self.heartbeat_path.write_text(datetime.now(tz=timezone.utc).isoformat(), encoding="utf-8")

    def close(self) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None
        if self.lock_path.exists():
            try:
                self.lock_path.unlink()
            except FileNotFoundError:
                pass

    def __enter__(self) -> "AccountWorkerLock":
        self.acquire()
        return self

    def __exit__(self, _exc_type: object, _exc: object, _traceback: object) -> None:
        self.close()




class MT5AccountWorker:
    def __init__(
        self,
        *,
        accounts: MT5AccountService,
        account: MT5Account,
        backoff_sequence: tuple[int, ...] = (1, 2, 5, 10, 30),
        command_queue: CommandQueue | None = None,
        on_error: Callable[[Exception], None] | None = None,
    ) -> None:
        if account.terminal_path is None:
            raise ValueError("Conta MT5 sem terminal provisionado.")
        self.accounts = accounts
        self.account = account
        self.account_dir = account.terminal_path.parent
        self.lock = AccountWorkerLock(self.account_dir)
        self.backoff_sequence = backoff_sequence
        self.command_queue = command_queue
        self.on_error = on_error
        self._backoff_index = 0
        self._started = False

    @property
    def current_backoff_seconds(self) -> int:
        return self.backoff_sequence[self._backoff_index]

    def start(self) -> None:
        if self._started:
            return
        self.lock.acquire()
        self._started = True
        self.heartbeat()

    def heartbeat(self) -> None:
        self.lock.heartbeat()
        self.accounts.update_worker_heartbeat(self.account.user_id, self.account.id)

    def reconnect_once(self) -> bool:
        if not self._started:
            self.start()
        try:
            # Retry through the startup recovery path. Besides retrying
            # transient IPC failures, this lets the terminal manager remove
            # instance-local files copied from a template (for example the MCP
            # assistant ports) before reconnecting an account marked failed.
            self.accounts.test_connection(
                self.account.user_id,
                self.account.id,
                startup_retry=True,
            )
            self.heartbeat()
            self._backoff_index = 0
            return True
        except Exception as exc:
            if self.on_error is not None:
                self.on_error(exc)
            self._backoff_index = min(self._backoff_index + 1, len(self.backoff_sequence) - 1)
            return False

    def process_once(self) -> bool:
        connected = self.reconnect_once()
        self.process_commands()
        return connected

    def process_commands(self) -> int:
        if self.command_queue is None:
            return 0

        processed = 0
        for command in self.command_queue.pending_for_account(self.account.user_id, self.account.id):
            try:
                self.process_command(command)
                self.command_queue.mark_done(command.id)
            except Exception as exc:
                self.command_queue.mark_failed(command.id, str(exc))
                if self.on_error is not None:
                    self.on_error(exc)
            processed += 1
        return processed

    def process_command(self, command: Command) -> None:
        if command.command_type in {"test_mt5_connection", "refresh_mt5_connection"}:
            self.accounts.test_connection(self.account.user_id, self.account.id)
            self.heartbeat()
            return
        if command.command_type == "remove_mt5_account":
            self.stop()
            return
        raise ValueError(f"Comando MT5 nao suportado: {command.command_type}")

    def stop(self) -> None:
        self.lock.close()
        self._started = False

    def close(self) -> None:
        self.stop()

    def __enter__(self) -> "MT5AccountWorker":
        self.start()
        return self

    def __exit__(self, _exc_type: object, _exc: object, _traceback: object) -> None:
        self.close()
