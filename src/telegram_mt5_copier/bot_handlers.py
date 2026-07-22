from __future__ import annotations

import logging
from typing import Any

from .bot_keyboards import build_inline_keyboard
from .bot_service import BotResponse, BotService

LOGGER = logging.getLogger(__name__)


def telegram_user_from_update(update: Any) -> tuple[int, str | None, str | None]:
    user = update.effective_user
    return int(user.id), getattr(user, "username", None), getattr(user, "first_name", None)


async def send_response(target: Any, response: BotResponse) -> None:
    reply_markup = build_inline_keyboard(response.keyboard) if response.keyboard else None
    await target.reply_text(response.text, reply_markup=reply_markup)


async def start_handler(update: Any, _context: Any, service: BotService) -> None:
    telegram_user_id, username, first_name = telegram_user_from_update(update)
    response = service.start(telegram_user_id, username, first_name)
    await send_response(update.message, response)


async def menu_handler(update: Any, _context: Any, service: BotService) -> None:
    telegram_user_id, username, first_name = telegram_user_from_update(update)
    response = service.menu(telegram_user_id, username, first_name)
    await send_response(update.message, response)


async def status_handler(update: Any, _context: Any, service: BotService) -> None:
    telegram_user_id, username, first_name = telegram_user_from_update(update)
    response = service.status(telegram_user_id, username, first_name)
    await send_response(update.message, response)


async def text_handler(update: Any, _context: Any, service: BotService) -> None:
    telegram_user_id, username, first_name = telegram_user_from_update(update)
    response = service.handle_text(
        telegram_user_id,
        username,
        update.message.text or "",
        first_name,
    )
    await send_response(update.message, response)


async def callback_handler(update: Any, _context: Any, service: BotService) -> None:
    query = update.callback_query
    try:
        await query.answer()
    except Exception as exc:
        if is_expired_callback_error(exc):
            LOGGER.info("Callback antigo do Telegram descartado.")
            return
        raise
    telegram_user_id, username, first_name = telegram_user_from_update(update)
    response = service.handle_callback(telegram_user_id, username, query.data, first_name)
    reply_markup = build_inline_keyboard(response.keyboard) if response.keyboard else None
    await query.edit_message_text(response.text, reply_markup=reply_markup)


def is_expired_callback_error(error: Exception) -> bool:
    message = str(error).lower()
    return "query is too old" in message or "response timeout expired" in message


async def error_handler(_update: object, context: Any) -> None:
    error = getattr(context, "error", None)
    if error is None:
        LOGGER.error("Erro não tratado no bot de gestão.")
        return
    LOGGER.error(
        "Erro não tratado no bot de gestão: %s",
        error,
        exc_info=(type(error), error, error.__traceback__),
    )
