from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path


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
        try:
            self._fd = os.open(str(self.lock_path), os.O_CREAT | os.O_EXCL | os.O_RDWR)
        except FileExistsError as exc:
            raise WorkerAlreadyRunningError("Worker MT5 ja esta ativo para esta conta.") from exc
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
