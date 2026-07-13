from __future__ import annotations

import argparse
import logging
from logging.handlers import RotatingFileHandler
import signal
import sys
from threading import Event

from .config import AppConfig


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="telegram-mt5-copier")
    parser.add_argument("--check", action="store_true", help="Valida configuracao e cria pastas.")
    parser.add_argument(
        "--telegram-login",
        action="store_true",
        help="Executa login interativo no Telegram e salva a sessao em SESSION_DIR.",
    )
    parser.add_argument(
        "--interval-seconds",
        type=int,
        default=60,
        help="Intervalo do loop principal do servico.",
    )
    args = parser.parse_args(argv)

    try:
        config = AppConfig.load(create_dirs=True)
    except Exception as exc:
        print(f"Falha ao carregar configuracao: {exc}", file=sys.stderr)
        return 2

    if args.check:
        return run_check(config)

    if args.telegram_login:
        return run_telegram_login(config)

    return run_service(config, interval_seconds=args.interval_seconds)


def run_check(config: AppConfig) -> int:
    missing = config.missing_required_telegram_values()

    print("Configuracao carregada com sucesso.")
    print(f"DRY_RUN={str(config.dry_run).lower()}")
    print(f"DATA_DIR={config.data_dir}")
    print(f"SESSION_DIR={config.session_dir}")
    print(f"LOG_DIR={config.log_dir}")
    print(f"SQLITE_DB={config.database_path}")

    if missing and not config.dry_run:
        print("Variaveis obrigatorias ausentes: " + ", ".join(missing), file=sys.stderr)
        return 2

    if missing:
        print("Valores do Telegram ainda nao configurados; permitido porque DRY_RUN=true.")

    return 0


def run_telegram_login(config: AppConfig) -> int:
    try:
        from .telegram_login import interactive_login

        interactive_login(config)
    except Exception as exc:
        print(f"Falha no login do Telegram: {exc}", file=sys.stderr)
        return 2

    return 0


def run_service(config: AppConfig, interval_seconds: int) -> int:
    logger = build_logger(config)
    missing = config.missing_required_telegram_values()

    if missing and not config.dry_run:
        logger.error("Variaveis obrigatorias ausentes: %s", ", ".join(missing))
        return 2

    if missing:
        logger.warning("Valores do Telegram ausentes; servico iniciado em DRY_RUN.")

    logger.info("Servico iniciado. DRY_RUN=%s", str(config.dry_run).lower())
    logger.info("DATA_DIR=%s", config.data_dir)
    logger.info("SESSION_DIR=%s", config.session_dir)
    logger.info("LOG_DIR=%s", config.log_dir)
    logger.info("SQLITE_DB=%s", config.database_path)

    stop_event = Event()

    def handle_stop(signum: int, _frame: object) -> None:
        logger.info("Sinal recebido para encerrar: %s", signum)
        stop_event.set()

    signal.signal(signal.SIGINT, handle_stop)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, handle_stop)

    interval = max(1, interval_seconds)
    while not stop_event.wait(interval):
        logger.info("Servico ativo.")

    logger.info("Servico encerrado.")
    return 0


def build_logger(config: AppConfig) -> logging.Logger:
    logger = logging.getLogger("telegram_mt5_copier")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    logger.propagate = False

    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)

    log_file = config.log_dir / "telegram-mt5-copier.log"
    file_handler = RotatingFileHandler(
        log_file,
        maxBytes=5 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    return logger
