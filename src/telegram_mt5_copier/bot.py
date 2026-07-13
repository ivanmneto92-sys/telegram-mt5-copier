from __future__ import annotations

import functools
import sys

from .bot_handlers import callback_handler, menu_handler, start_handler, status_handler
from .bot_service import BotService
from .config import AppConfig


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
        from telegram.ext import Application, CallbackQueryHandler, CommandHandler
    except ImportError:
        print("python-telegram-bot nao instalado. Execute o setup do ambiente primeiro.", file=sys.stderr)
        return 2

    service = BotService(config.database_path, admin_ids=config.bot_admin_ids)
    application = Application.builder().token(config.telegram_bot_token).build()
    application.add_handler(CommandHandler("start", functools.partial(start_handler, service=service)))
    application.add_handler(CommandHandler("menu", functools.partial(menu_handler, service=service)))
    application.add_handler(CommandHandler("status", functools.partial(status_handler, service=service)))
    application.add_handler(CallbackQueryHandler(functools.partial(callback_handler, service=service)))

    print("Bot de gestao iniciado.")
    try:
        application.run_polling()
    finally:
        service.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
