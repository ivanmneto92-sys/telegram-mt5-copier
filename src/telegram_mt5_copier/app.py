from __future__ import annotations

import argparse
import logging
from logging.handlers import RotatingFileHandler
import sys

from .config import AppConfig
from .listener import run_listener
from .supervisor import SupervisorAlreadyRunningError, SupervisorLock


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="telegram-mt5-copier")
    parser.add_argument("--check", action="store_true", help="Valida configuracao e cria pastas.")
    parser.add_argument(
        "--sync-channels",
        action="store_true",
        help=(
            "Reconciliacao unica: replica o estado atual dos canais ativos desta "
            "instancia para as instancias listadas em PEER_CHANNEL_SYNC_DATABASES."
        ),
    )
    parser.add_argument(
        "--telegram-login",
        action="store_true",
        help="Executa login interativo no Telegram e salva a sessao em SESSION_DIR.",
    )
    parser.add_argument("--provision-mt5-account", type=int, help="Provisiona terminal isolado para uma conta MT5.")
    parser.add_argument("--user-id", type=int, help="ID interno do usuario dono da conta MT5.")
    args = parser.parse_args(argv)

    try:
        config = AppConfig.load(create_dirs=True)
    except Exception as exc:
        print(f"Falha ao carregar configuracao: {exc}", file=sys.stderr)
        return 2

    if args.check:
        return run_check(config)

    if args.sync_channels:
        return run_sync_channels(config)

    if args.telegram_login:
        return run_telegram_login(config)

    if args.provision_mt5_account is not None:
        if args.user_id is None:
            print("--user-id e obrigatorio com --provision-mt5-account.", file=sys.stderr)
            return 2
        return run_mt5_provision(config, args.user_id, args.provision_mt5_account)

    return run_service(config)


def run_check(config: AppConfig) -> int:
    missing = config.missing_required_telegram_values()

    print("Configuracao carregada com sucesso.")
    print(f"INSTANCE_ID={config.instance_id}")
    print(f"BRAND_NAME={config.brand_name}")
    print(f"DRY_RUN={str(config.dry_run).lower()}")
    print(f"DATA_DIR={config.data_dir}")
    print(f"SESSION_DIR={config.session_dir}")
    print(f"LOG_DIR={config.log_dir}")
    print(f"SQLITE_DB={config.database_path}")
    print(f"MT5_BASE_DIR={config.mt5_base_dir}")
    print(f"MT5_EXECUTION_MODE={config.mt5_execution_mode}")
    print(f"MT5_MAX_ACCOUNTS_PER_VPS={config.mt5_max_accounts_per_vps}")
    print(f"ALLOW_LIVE_ACCOUNTS={str(config.allow_live_accounts).lower()}")
    print(f"DEFAULT_PENDING_EXPIRATION_MINUTES={config.default_pending_expiration_minutes}")
    print(f"GLOBAL_EXECUTION_KILL_SWITCH={str(config.global_execution_kill_switch).lower()}")
    print(f"ONBOARDING_LOCAL_URL={config.local_onboarding_url}")
    print(f"CLIENT_APP_URL={config.client_app_url or 'ausente'}")
    print(f"MARKET_NEWS_ENABLED={str(config.market_news_enabled).lower()}")
    print(f"TELEGRAM_IMAGE_OCR_ENABLED={str(config.telegram_image_ocr_enabled).lower()}")
    print(f"TELEGRAM_IMAGE_OCR_CHAT_IDS={','.join(config.telegram_image_ocr_chat_ids) or 'nenhum'}")
    peer_databases = ",".join(str(path) for path in config.peer_channel_sync_database_paths)
    print(f"PEER_CHANNEL_SYNC_DATABASES={peer_databases or 'nenhum'}")
    print(
        "ECONOMIC_CALENDAR_API_KEY="
        + ("configurada" if config.economic_calendar_api_key else "ausente")
    )

    if missing and not config.dry_run:
        print("Variaveis obrigatorias ausentes: " + ", ".join(missing), file=sys.stderr)
        return 2

    if missing:
        print("Valores do Telegram ainda nao configurados; permitido porque DRY_RUN=true.")

    return 0


def run_sync_channels(config: AppConfig) -> int:
    if not config.peer_channel_sync_database_paths:
        print(
            "PEER_CHANNEL_SYNC_DATABASES nao configurado nesta instancia; nada para sincronizar.",
            file=sys.stderr,
        )
        return 2

    from .channel_catalog import ChannelCatalogService

    catalog = ChannelCatalogService(
        config.database_path,
        peer_database_paths=config.peer_channel_sync_database_paths,
    )
    report = catalog.resync_active_channels_with_peers()

    print(f"Canais ativos nesta instancia ({config.instance_id}): {len(report)}")
    for item in report:
        print(f"- {item['display_name']} ({item['telegram_chat_id']})")
        for peer_result in item["peers"]:
            outcome = peer_result["outcome"]
            label = {
                "activated": "sincronizado e ativado no peer",
                "pending_confirmation": "peer ainda nao confirmou acesso; so o apelido foi sincronizado",
                "not_found": "peer ainda nao conhece este canal (conta tecnica precisa entrar la primeiro)",
                "error": "falha ao conectar no banco peer",
            }.get(str(outcome), str(outcome))
            print(f"    peer {peer_result['peer']}: {label}")

    return 0


def run_telegram_login(config: AppConfig) -> int:
    try:
        from .telegram_login import interactive_login

        interactive_login(config)
    except Exception as exc:
        print(f"Falha no login do Telegram: {exc}", file=sys.stderr)
        return 2

    return 0


def run_mt5_provision(config: AppConfig, user_id: int, account_id: int) -> int:
    try:
        from .mt5.account_service import MT5AccountService
        from .mt5.terminal_manager import TerminalManager

        terminal_manager = TerminalManager(
            config.mt5_base_dir,
            config.mt5_template_path,
            config.mt5_broker_template_paths,
            config.mt5_broker_servers,
        )
        accounts = MT5AccountService(config.database_path)
        try:
            account = accounts.get_account(user_id, account_id)
            provisioned = terminal_manager.provision_account(
                account_id, broker_name=account.broker_name
            )
            accounts.update_terminal_path(user_id, account_id, provisioned.terminal_path)
        finally:
            accounts.close()
        print(f"Terminal isolado provisionado em: {provisioned.account_dir}")
        return 0
    except Exception as exc:
        print(f"Falha ao provisionar terminal MT5: {exc}", file=sys.stderr)
        return 2


def run_service(config: AppConfig) -> int:
    logger = build_logger(config)
    missing = config.missing_required_telegram_values()

    if missing:
        logger.error("Variaveis obrigatorias ausentes: %s", ", ".join(missing))
        return 2

    logger.info("Servico iniciado. DRY_RUN=%s", str(config.dry_run).lower())
    logger.info("INSTANCE_ID=%s BRAND_NAME=%s", config.instance_id, config.brand_name)
    logger.info("DATA_DIR=%s", config.data_dir)
    logger.info("SESSION_DIR=%s", config.session_dir)
    logger.info("LOG_DIR=%s", config.log_dir)
    logger.info("SQLITE_DB=%s", config.database_path)

    try:
        with SupervisorLock(config.signal_monitor_lock_path):
            return run_listener(config, logger)
    except SupervisorAlreadyRunningError as exc:
        logger.error("Monitor de sinais duplicado bloqueado: %s", exc)
        return 3
    except Exception as exc:
        logger.error("Falha no monitoramento: %s", exc)
        return 2


def build_logger(config: AppConfig) -> logging.Logger:
    logger = logging.getLogger("telegram_mt5_copier")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    logger.propagate = False

    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)

    log_file = config.signal_monitor_log_path
    file_handler = RotatingFileHandler(
        log_file,
        maxBytes=5 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    return logger
