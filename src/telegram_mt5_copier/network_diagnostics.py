from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from pathlib import Path
import socket
import sqlite3
import subprocess
import time
from urllib import error, request

from .config import AppConfig

PROBE_ATTEMPTS = 8
CONNECT_TIMEOUT_SECONDS = 3.0
PROBE_DELAY_SECONDS = 0.5
NETWORK_ERROR_MARKERS = (
    "server closed the connection",
    "connecting failed",
    "timeouterror",
    "winerror 121",
    "semaphore timeout",
    "connectionerror",
)


@dataclass(frozen=True)
class TelegramDataCenter:
    dc_id: int
    server_address: str
    port: int


@dataclass(frozen=True)
class ProbeSummary:
    host: str
    port: int
    attempts: int
    successes: int
    min_ms: float | None
    average_ms: float | None
    max_ms: float | None
    errors: tuple[tuple[str, int], ...]


def main() -> int:
    try:
        config = AppConfig.load(create_dirs=False)
    except Exception as exc:
        print(f"ERRO configuracao: {exc}")
        return 2

    print("DIAGNOSTICO DE REDE TELEGRAM")
    print(f"Horario local: {time.strftime('%Y-%m-%d %H:%M:%S %Z')}")
    print(f"Sessao: {config.telegram_session_name}.session")
    print()

    show_windows_time_status()
    print()
    show_dns_status()
    print()
    show_https_status()
    print()

    try:
        data_center = read_telegram_data_center(config.telegram_session_name)
    except Exception as exc:
        print(f"ERRO sessao Telegram: {exc}")
        return 2

    print(
        f"Data center da sessao: DC={data_center.dc_id} "
        f"endereco={data_center.server_address} porta={data_center.port}"
    )
    summary = probe_tcp(
        data_center.server_address,
        data_center.port,
        attempts=PROBE_ATTEMPTS,
        timeout_seconds=CONNECT_TIMEOUT_SECONDS,
        delay_seconds=PROBE_DELAY_SECONDS,
    )
    print_probe_summary(summary)
    print()
    show_recent_network_errors(config.signal_monitor_log_path)
    print()

    if summary.successes == summary.attempts:
        print("RESULTADO: conectividade TCP com o Telegram esta estavel neste teste.")
        return 0
    if summary.successes > 0:
        print("RESULTADO: conectividade intermitente; ha perda ou timeout entre a VPS e o Telegram.")
        return 1
    print("RESULTADO: nenhuma conexao TCP com o data center do Telegram foi concluida.")
    return 2


def read_telegram_data_center(session_name: Path) -> TelegramDataCenter:
    session_path = Path(f"{session_name}.session").resolve()
    if not session_path.is_file():
        raise FileNotFoundError(f"arquivo de sessao nao encontrado: {session_path}")

    uri = f"{session_path.as_uri()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=3)
    try:
        cursor = connection.execute(
            "SELECT dc_id, server_address, port FROM sessions ORDER BY dc_id LIMIT 1"
        )
        try:
            row = cursor.fetchone()
        finally:
            cursor.close()
    finally:
        connection.close()

    if row is None:
        raise RuntimeError("sessao nao contem data center.")
    return TelegramDataCenter(int(row[0]), str(row[1]), int(row[2]))


def probe_tcp(
    host: str,
    port: int,
    *,
    attempts: int,
    timeout_seconds: float,
    delay_seconds: float = 0,
) -> ProbeSummary:
    latencies: list[float] = []
    failures: Counter[str] = Counter()
    for attempt in range(attempts):
        started = time.perf_counter()
        try:
            with socket.create_connection((host, port), timeout=timeout_seconds):
                latencies.append((time.perf_counter() - started) * 1000)
        except OSError as exc:
            error_code = getattr(exc, "winerror", None) or getattr(exc, "errno", None)
            label = f"{type(exc).__name__}:{error_code or 'sem_codigo'}"
            failures[label] += 1
        if delay_seconds and attempt + 1 < attempts:
            time.sleep(delay_seconds)

    return ProbeSummary(
        host=host,
        port=port,
        attempts=attempts,
        successes=len(latencies),
        min_ms=min(latencies) if latencies else None,
        average_ms=sum(latencies) / len(latencies) if latencies else None,
        max_ms=max(latencies) if latencies else None,
        errors=tuple(sorted(failures.items())),
    )


def print_probe_summary(summary: ProbeSummary) -> None:
    print(
        f"Teste TCP: {summary.host}:{summary.port} "
        f"sucessos={summary.successes}/{summary.attempts}"
    )
    if summary.average_ms is not None:
        print(
            "Latencia TCP (ms): "
            f"min={summary.min_ms:.1f} media={summary.average_ms:.1f} max={summary.max_ms:.1f}"
        )
    if summary.errors:
        print("Falhas TCP: " + ", ".join(f"{name}={count}" for name, count in summary.errors))


def show_dns_status() -> None:
    for host in ("telegram.org", "api.telegram.org"):
        try:
            results = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
            addresses = sorted({str(item[4][0]) for item in results})
            print(f"DNS {host}: OK ({', '.join(addresses[:4])})")
        except OSError as exc:
            print(f"DNS {host}: FALHA ({type(exc).__name__})")


def show_https_status() -> None:
    api_request = request.Request(
        "https://api.telegram.org",
        headers={"User-Agent": "telegram-mt5-copier-network-check"},
    )
    try:
        with request.urlopen(api_request, timeout=CONNECT_TIMEOUT_SECONDS) as response:
            print(f"HTTPS api.telegram.org: OK (HTTP {response.status})")
    except error.HTTPError as exc:
        print(f"HTTPS api.telegram.org: OK (HTTP {exc.code})")
    except OSError as exc:
        error_code = getattr(exc, "winerror", None) or getattr(exc, "errno", None)
        print(f"HTTPS api.telegram.org: FALHA ({type(exc).__name__}:{error_code or 'sem_codigo'})")


def show_windows_time_status() -> None:
    try:
        result = subprocess.run(
            ["w32tm", "/query", "/status"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"Horario Windows: indisponivel ({type(exc).__name__})")
        return

    selected = [
        line.strip()
        for line in result.stdout.splitlines()
        if any(marker in line.lower() for marker in ("source:", "last successful sync", "offset:"))
    ]
    if selected:
        print("Horario Windows: " + " | ".join(selected))
    else:
        print(f"Horario Windows: consulta concluida (codigo={result.returncode})")


def show_recent_network_errors(log_path: Path, *, line_limit: int = 10000) -> None:
    if not log_path.is_file():
        print(f"Log do Monitor: nao encontrado ({log_path})")
        return
    try:
        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-line_limit:]
    except OSError as exc:
        print(f"Log do Monitor: falha de leitura ({type(exc).__name__})")
        return

    matches = [
        line
        for line in lines
        if any(marker in line.lower() for marker in NETWORK_ERROR_MARKERS)
    ]
    print(f"Erros de rede encontrados nas ultimas {len(lines)} linhas do log: {len(matches)}")
    for line in matches[-8:]:
        print(f"  {line[:300]}")


if __name__ == "__main__":
    raise SystemExit(main())
