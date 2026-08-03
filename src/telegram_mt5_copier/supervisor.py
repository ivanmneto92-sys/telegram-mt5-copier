from __future__ import annotations

from dataclasses import dataclass
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from typing import BinaryIO, Callable

from .config import AppConfig
from .mt5.ipc_lock import process_is_running
from .telegram_notifier import TelegramAdminNotifier


RESTART_DELAYS_SECONDS = (1, 2, 5, 10, 30, 60)
STABLE_RUNTIME_SECONDS = 300
SUPERVISOR_POLL_SECONDS = 1


@dataclass(frozen=True)
class ServiceSpec:
    name: str
    command: tuple[str, ...]


@dataclass
class ServiceState:
    spec: ServiceSpec
    process: object | None = None
    started_at: float | None = None
    next_start_at: float = 0
    consecutive_failures: int = 0
    log_handle: BinaryIO | None = None
    failure_alerted: bool = False


class SupervisorAlreadyRunningError(RuntimeError):
    pass


class SupervisorLock:
    def __init__(self, lock_path: Path) -> None:
        self.lock_path = lock_path
        self._fd: int | None = None

    def acquire(self) -> None:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        if self.lock_path.exists():
            try:
                existing_pid = int(self.lock_path.read_text(encoding="ascii").strip())
            except (OSError, ValueError):
                existing_pid = 0
            if existing_pid and process_is_running(existing_pid):
                raise SupervisorAlreadyRunningError(
                    f"Supervisor ja esta ativo no processo {existing_pid}."
                )
            self.lock_path.unlink(missing_ok=True)
        self._fd = os.open(
            str(self.lock_path),
            os.O_CREAT | os.O_EXCL | os.O_RDWR,
        )
        os.write(self._fd, str(os.getpid()).encode("ascii"))

    def close(self) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None
        self.lock_path.unlink(missing_ok=True)

    def __enter__(self) -> "SupervisorLock":
        self.acquire()
        return self

    def __exit__(self, _exc_type: object, _exc: object, _traceback: object) -> None:
        self.close()


class PlatformSupervisor:
    def __init__(
        self,
        *,
        project_root: Path,
        log_dir: Path,
        services: tuple[ServiceSpec, ...],
        logger: logging.Logger,
        popen_factory: Callable[..., object] = subprocess.Popen,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
        alert_callback: Callable[[str], bool] | None = None,
    ) -> None:
        self.project_root = project_root
        self.log_dir = log_dir
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.logger = logger
        self.popen_factory = popen_factory
        self.clock = clock
        self.sleeper = sleeper
        self.alert_callback = alert_callback
        self.states = {spec.name: ServiceState(spec) for spec in services}
        self.stopping = False

    def run(self) -> None:
        self.logger.info(
            "Supervisor iniciado. servicos=%s",
            ",".join(self.states),
        )
        while not self.stopping:
            self.tick()
            self.sleeper(SUPERVISOR_POLL_SECONDS)
        self.stop_all()

    def tick(self) -> None:
        now = self.clock()
        for state in self.states.values():
            if state.process is None:
                if now >= state.next_start_at and not self.stopping:
                    self._start(state, now)
                continue
            return_code = state.process.poll()
            if return_code is None:
                runtime = now - state.started_at if state.started_at is not None else 0
                if state.failure_alerted and runtime >= STABLE_RUNTIME_SECONDS:
                    self._alert(
                        "✅ SERVIÇO RECUPERADO\n\n"
                        f"{service_display_name(state.spec.name)} está estável novamente."
                    )
                    state.failure_alerted = False
                continue
            self._handle_exit(state, int(return_code), now)

    def stop_all(self) -> None:
        self.stopping = True
        for state in self.states.values():
            process = state.process
            if process is None or process.poll() is not None:
                self._close_service_log(state)
                state.process = None
                continue
            self.logger.info("Encerrando servico. nome=%s", state.spec.name)
            try:
                process.terminate()
                process.wait(timeout=10)
            except Exception:
                try:
                    process.kill()
                    process.wait(timeout=5)
                except Exception as exc:
                    self.logger.error(
                        "Falha ao encerrar servico. nome=%s erro=%s",
                        state.spec.name,
                        exc,
                    )
            self._close_service_log(state)
            state.process = None

    def _start(self, state: ServiceState, now: float) -> None:
        log_path = self.log_dir / f"supervisor-{state.spec.name}.log"
        rotate_subprocess_log(log_path)
        state.log_handle = log_path.open("ab", buffering=0)
        environment = os.environ.copy()
        environment["PYTHONUNBUFFERED"] = "1"
        creation_flags = int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
        try:
            state.process = self.popen_factory(
                list(state.spec.command),
                cwd=str(self.project_root),
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=state.log_handle,
                stderr=subprocess.STDOUT,
                creationflags=creation_flags,
            )
        except Exception as exc:
            self._close_service_log(state)
            state.consecutive_failures += 1
            delay = restart_delay(state.consecutive_failures)
            state.next_start_at = now + delay
            self.logger.error(
                "Falha ao iniciar servico. nome=%s nova_tentativa=%ss erro=%s",
                state.spec.name,
                delay,
                exc,
            )
            if not state.failure_alerted:
                self._alert(
                    "🚨 ALERTA OPERACIONAL\n\n"
                    f"Serviço: {service_display_name(state.spec.name)}\n"
                    "Problema: não foi possível iniciar o processo.\n"
                    f"Nova tentativa em {delay} segundo(s)."
                )
                state.failure_alerted = True
            return
        state.started_at = now
        self.logger.info(
            "Servico iniciado. nome=%s pid=%s",
            state.spec.name,
            getattr(state.process, "pid", "desconhecido"),
        )

    def _handle_exit(self, state: ServiceState, return_code: int, now: float) -> None:
        runtime = now - state.started_at if state.started_at is not None else 0
        if runtime >= STABLE_RUNTIME_SECONDS:
            state.consecutive_failures = 0
        state.consecutive_failures += 1
        delay = restart_delay(state.consecutive_failures)
        self.logger.warning(
            "Servico encerrado. nome=%s codigo=%s duracao=%.1fs reinicio=%ss",
            state.spec.name,
            return_code,
            runtime,
            delay,
        )
        if not state.failure_alerted:
            self._alert(
                "🚨 ALERTA OPERACIONAL\n\n"
                f"Serviço: {service_display_name(state.spec.name)}\n"
                f"Problema: processo encerrado com código {return_code}.\n"
                f"Reinício automático em {delay} segundo(s)."
            )
            state.failure_alerted = True
        self._close_service_log(state)
        state.process = None
        state.started_at = None
        state.next_start_at = now + delay

    def _alert(self, message: str) -> None:
        if self.alert_callback is None:
            return
        try:
            self.alert_callback(message)
        except Exception as exc:
            self.logger.error("Falha ao enviar alerta do supervisor: %s", exc)

    @staticmethod
    def _close_service_log(state: ServiceState) -> None:
        if state.log_handle is not None:
            state.log_handle.close()
            state.log_handle = None


def restart_delay(consecutive_failures: int) -> int:
    index = min(max(consecutive_failures, 1) - 1, len(RESTART_DELAYS_SECONDS) - 1)
    return RESTART_DELAYS_SECONDS[index]


def default_service_specs(
    python_executable: Path | None = None,
    *,
    include_health_monitor: bool = True,
) -> tuple[ServiceSpec, ...]:
    python_path = python_executable or Path(sys.executable)
    scripts_dir = python_path.parent
    suffix = ".exe" if os.name == "nt" else ""
    executable_names: tuple[tuple[str, str], ...] = (
        ("mini-app", "telegram-mt5-onboarding"),
        ("management-bot", "telegram-management-bot"),
        ("signal-monitor", "telegram-copier"),
        ("mt5-worker", "telegram-mt5-worker"),
    )
    if include_health_monitor:
        executable_names += (("health-monitor", "telegram-mt5-health-monitor"),)
    specs: list[ServiceSpec] = []
    missing: list[str] = []
    for service_name, executable_name in executable_names:
        executable = scripts_dir / f"{executable_name}{suffix}"
        if not executable.is_file():
            missing.append(str(executable))
            continue
        specs.append(ServiceSpec(service_name, (str(executable),)))
    if missing:
        raise ValueError(
            "Executaveis dos servicos nao encontrados: " + ", ".join(missing)
        )
    return tuple(specs)


def service_display_name(service_name: str) -> str:
    return {
        "mini-app": "Mini App / cadastro MT5",
        "management-bot": "Bot de gestão",
        "signal-monitor": "Monitor de sinais",
        "mt5-worker": "Worker MT5",
        "health-monitor": "Monitor operacional",
    }.get(service_name, service_name)


def rotate_subprocess_log(log_path: Path, max_bytes: int = 10 * 1024 * 1024) -> None:
    if not log_path.exists() or log_path.stat().st_size < max_bytes:
        return
    backup_path = log_path.with_suffix(f"{log_path.suffix}.1")
    backup_path.unlink(missing_ok=True)
    log_path.replace(backup_path)


def build_supervisor_logger(log_dir: Path) -> logging.Logger:
    logger = logging.getLogger("telegram_mt5_supervisor")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    logger.propagate = False
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    stream = logging.StreamHandler()
    stream.setFormatter(formatter)
    logger.addHandler(stream)
    rotating = RotatingFileHandler(
        log_dir / "telegram-mt5-supervisor.log",
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
        logger = build_supervisor_logger(config.log_dir)
        logger.info(
            "Instancia carregada. id=%s marca=%s",
            config.instance_id,
            config.brand_name,
        )
        services = default_service_specs(
            include_health_monitor=config.operational_alerts_enabled
        )
        notifier = TelegramAdminNotifier(
            config.telegram_bot_token,
            config.bot_admin_ids,
            logger=logger,
        )
        supervisor = PlatformSupervisor(
            project_root=config.project_root,
            log_dir=config.log_dir,
            services=services,
            logger=logger,
            alert_callback=(
                notifier.send if config.operational_alerts_enabled else None
            ),
        )
        lock = SupervisorLock(config.supervisor_lock_path)

        def request_stop(_signal_number: int, _frame: object) -> None:
            supervisor.stopping = True

        signal.signal(signal.SIGINT, request_stop)
        if hasattr(signal, "SIGTERM"):
            signal.signal(signal.SIGTERM, request_stop)
        with lock:
            supervisor.run()
        return 0
    except SupervisorAlreadyRunningError as exc:
        print(str(exc), file=sys.stderr)
        return 3
    except Exception as exc:
        print(f"Falha no supervisor: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
