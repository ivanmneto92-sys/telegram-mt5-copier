from __future__ import annotations

from dataclasses import dataclass
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import signal
import subprocess
import sys
import time
from typing import BinaryIO, Callable

from .config import AppConfig
from .mt5.account_service import MT5AccountService
from .supervisor import restart_delay, rotate_subprocess_log

POOL_POLL_SECONDS = 5
# Generous: covers normal terminal startup/login plus the worker's own
# ACCOUNT_HEARTBEAT_SECONDS=15 cadence with margin. A child that goes this
# long without its DB heartbeat moving is not "still starting up" anymore --
# it is stuck inside a blocking MT5 IPC call with no exception to catch, so
# nothing short of killing the process from outside will unstick it.
HANG_TIMEOUT_SECONDS = 90

LOGGER_NAME = "telegram_mt5_worker_pool"


@dataclass
class ChildState:
    process: object | None = None
    log_handle: BinaryIO | None = None
    started_at: float = 0.0
    consecutive_failures: int = 0
    next_start_at: float = 0.0
    last_heartbeat_seen: str | None = None
    last_progress_at: float = 0.0


class MT5WorkerPool:
    """Runs one telegram-mt5-worker subprocess per active MT5 account.

    The MetaTrader5 package only supports one live terminal connection per
    process, so accounts can never truly run in parallel inside a single
    worker process -- process_account() isolates exceptions, but a hang
    inside a blocking native IPC call (a slow/unresponsive terminal) still
    freezes every other account sharing that process, since nothing raises
    and there is no exception to catch. Giving each account its own OS
    process removes that shared point of failure: a hung account only
    blocks its own process, which this pool detects (via a stalled DB
    heartbeat) and restarts, without touching any other account.
    """

    def __init__(
        self,
        *,
        project_root: Path,
        log_dir: Path,
        database_path: Path,
        python_executable: Path,
        logger: logging.Logger,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
        popen_factory: Callable[..., object] = subprocess.Popen,
    ) -> None:
        self.project_root = project_root
        self.log_dir = log_dir
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.database_path = database_path
        self.python_executable = python_executable
        self.logger = logger
        self.clock = clock
        self.sleeper = sleeper
        self.popen_factory = popen_factory
        self.children: dict[int, ChildState] = {}
        self.stopping = False

    def run(self) -> None:
        self.logger.info("Pool de workers MT5 iniciado.")
        while not self.stopping:
            self.tick()
            self.sleeper(POOL_POLL_SECONDS)
        self.stop_all()

    def tick(self) -> None:
        accounts_and_heartbeats = self._active_accounts()
        active_ids = set(accounts_and_heartbeats)
        now = self.clock()

        for account_id in tuple(self.children):
            if account_id not in active_ids:
                self.logger.info(
                    "Conta %s nao esta mais ativa; encerrando o worker dela.",
                    account_id,
                )
                self._kill(account_id)
                del self.children[account_id]

        for account_id in active_ids:
            state = self.children.get(account_id)
            if state is None:
                state = ChildState()
                self.children[account_id] = state
            if state.process is None:
                if now >= state.next_start_at:
                    self._start(account_id, state, now)
                continue
            self._check_health(account_id, state, accounts_and_heartbeats[account_id], now)

    def _active_accounts(self) -> dict[int, str | None]:
        accounts = MT5AccountService(self.database_path)
        try:
            return {
                account.id: account.worker_heartbeat_at
                for account, _ in accounts.accounts_for_active_users()
            }
        finally:
            accounts.close()

    def _start(self, account_id: int, state: ChildState, now: float) -> None:
        log_path = self.log_dir / f"mt5-worker-account-{account_id}.log"
        rotate_subprocess_log(log_path)
        state.log_handle = log_path.open("ab", buffering=0)
        creation_flags = int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
        try:
            state.process = self.popen_factory(
                [str(self.python_executable), "-m", "telegram_mt5_copier.mt5_worker", "--account-id", str(account_id)],
                cwd=str(self.project_root),
                stdin=subprocess.DEVNULL,
                stdout=state.log_handle,
                stderr=subprocess.STDOUT,
                creationflags=creation_flags,
            )
        except Exception as exc:
            state.consecutive_failures += 1
            delay = restart_delay(state.consecutive_failures)
            state.next_start_at = now + delay
            self.logger.error(
                "Falha ao iniciar worker da conta %s. nova_tentativa=%ss erro=%s",
                account_id,
                delay,
                exc,
            )
            return
        state.started_at = now
        state.last_heartbeat_seen = None
        state.last_progress_at = now
        self.logger.info(
            "Worker iniciado. conta=%s pid=%s",
            account_id,
            getattr(state.process, "pid", "desconhecido"),
        )

    def _check_health(
        self,
        account_id: int,
        state: ChildState,
        heartbeat: str | None,
        now: float,
    ) -> None:
        return_code = state.process.poll()
        if return_code is not None:
            self._handle_exit(account_id, state, int(return_code), now)
            return

        if heartbeat != state.last_heartbeat_seen:
            state.last_heartbeat_seen = heartbeat
            state.last_progress_at = now
            return

        if now - state.last_progress_at >= HANG_TIMEOUT_SECONDS:
            self.logger.error(
                "Worker da conta %s sem heartbeat ha mais de %ss; forcando reinicio.",
                account_id,
                HANG_TIMEOUT_SECONDS,
            )
            self._kill(account_id)
            state.consecutive_failures += 1
            delay = restart_delay(state.consecutive_failures)
            state.next_start_at = now + delay
            state.process = None

    def _handle_exit(self, account_id: int, state: ChildState, return_code: int, now: float) -> None:
        runtime = now - state.started_at
        if runtime >= 300:
            state.consecutive_failures = 0
        state.consecutive_failures += 1
        delay = restart_delay(state.consecutive_failures)
        state.next_start_at = now + delay
        self._close_log(state)
        state.process = None
        self.logger.warning(
            "Worker encerrado. conta=%s codigo=%s duracao=%.1fs reinicio=%ss",
            account_id,
            return_code,
            runtime,
            delay,
        )

    def _kill(self, account_id: int) -> None:
        state = self.children.get(account_id)
        if state is None or state.process is None:
            return
        if state.process.poll() is not None:
            self._close_log(state)
            state.process = None
            return
        try:
            state.process.terminate()
            state.process.wait(timeout=10)
        except Exception:
            try:
                state.process.kill()
                state.process.wait(timeout=5)
            except Exception as exc:
                self.logger.error(
                    "Falha ao encerrar worker. conta=%s erro=%s", account_id, exc
                )
        self._close_log(state)
        state.process = None

    def _close_log(self, state: ChildState) -> None:
        if state.log_handle is not None:
            try:
                state.log_handle.close()
            except Exception:
                pass
            state.log_handle = None

    def stop_all(self) -> None:
        for account_id in tuple(self.children):
            self._kill(account_id)


def build_pool_logger(log_dir: Path) -> logging.Logger:
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    logger.propagate = False
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    stream = logging.StreamHandler()
    stream.setFormatter(formatter)
    logger.addHandler(stream)
    rotating = RotatingFileHandler(
        log_dir / "mt5-worker-pool.log",
        maxBytes=5 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    rotating.setFormatter(formatter)
    logger.addHandler(rotating)
    return logger


def main() -> int:
    try:
        config = AppConfig.load(create_dirs=True)
        logger = build_pool_logger(config.log_dir)
        pool = MT5WorkerPool(
            project_root=config.project_root,
            log_dir=config.log_dir,
            database_path=config.database_path,
            python_executable=Path(sys.executable),
            logger=logger,
        )

        def request_stop(_signal_number: int, _frame: object) -> None:
            pool.stopping = True

        signal.signal(signal.SIGINT, request_stop)
        if hasattr(signal, "SIGTERM"):
            signal.signal(signal.SIGTERM, request_stop)
        pool.run()
        return 0
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        print(f"Falha no pool de workers MT5: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
