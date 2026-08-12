from __future__ import annotations

import functools
import sys

from .bot_handlers import (
    callback_handler,
    error_handler,
    menu_handler,
    start_handler,
    status_handler,
    text_handler,
)
from .bot_service import BotService
from .config import AppConfig
from .credential_service import CredentialService
from .mt5.account_service import MT5AccountService
from .mt5.client import MT5Client
from .mt5.terminal_manager import TerminalManager


def main() -> int:
    try:
        config = AppConfig.load(create_dirs=True)
    except Exception as exc:
        print(f"Falha ao carregar configuracao do bot: {exc}", file=sys.stderr)
        return 2

    if not config.bot_enabled:
        print("Bot de gestao desativado por BOT_ENABLED=false.")
        return 0
    if not config.telegram_bot_token:
        print("TELEGRAM_BOT_TOKEN nao configurado.", file=sys.stderr)
        return 2

    try:
        from telegram.ext import Application, CallbackQueryHandler, CommandHandler, MessageHandler, filters
    except ImportError:
        print("python-telegram-bot nao instalado. Execute o setup do ambiente primeiro.", file=sys.stderr)
        return 2

    mt5_account_service = None
    if config.mt5_credential_key:
        mt5_account_service = MT5AccountService(
            config.database_path,
            credential_service=CredentialService(config.mt5_credential_key),
            terminal_manager=TerminalManager(
                config.mt5_base_dir,
                config.mt5_template_path,
                config.mt5_broker_template_paths,
            ),
            client_factory=MT5Client,
            allow_live_accounts=config.allow_live_accounts,
            max_accounts_per_vps=config.mt5_max_accounts_per_vps,
            daily_performance_timezone=config.daily_performance_timezone,
        )

    service = BotService(
        config.database_path,
        admin_ids=config.bot_admin_ids,
        mt5_account_service=mt5_account_service,
        mt5_onboarding_url=config.mt5_onboarding_url,
        brand_name=config.brand_name,
        market_news_available=config.market_news_enabled,
        market_news_minutes_before=config.market_news_minutes_before,
        market_news_minutes_after=config.market_news_minutes_after,
    )
    application = Application.builder().token(config.telegram_bot_token).build()
    application.add_handler(CommandHandler("start", functools.partial(start_handler, service=service)))
    application.add_handler(CommandHandler("menu", functools.partial(menu_handler, service=service)))
    application.add_handler(CommandHandler("status", functools.partial(status_handler, service=service)))
    application.add_handler(CallbackQueryHandler(functools.partial(callback_handler, service=service)))
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, functools.partial(text_handler, service=service))
    )
    application.add_error_handler(error_handler)

    print("Bot de gestao iniciado.")
    try:
        application.run_polling(drop_pending_updates=True)
    finally:
        service.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
