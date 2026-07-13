from __future__ import annotations

from typing import Any

from .bot_keyboards import build_inline_keyboard
from .bot_service import BotResponse, BotService


def telegram_user_from_update(update: Any) -> tuple[int, str | None]:
    user = update.effective_user
    return int(user.id), getattr(user, "username", None)


async def send_response(target: Any, response: BotResponse) -> None:
    reply_markup = build_inline_keyboard(response.keyboard) if response.keyboard else None
    await target.reply_text(response.text, reply_markup=reply_markup)


async def start_handler(update: Any, _context: Any, service: BotService) -> None:
    telegram_user_id, username = telegram_user_from_update(update)
    response = service.start(telegram_user_id, username)
    await send_response(update.message, response)


async def menu_handler(update: Any, _context: Any, service: BotService) -> None:
    telegram_user_id, username = telegram_user_from_update(update)
    response = service.menu(telegram_user_id, username)
    await send_response(update.message, response)


async def status_handler(update: Any, _context: Any, service: BotService) -> None:
    telegram_user_id, username = telegram_user_from_update(update)
    response = service.status(telegram_user_id, username)
    await send_response(update.message, response)


async def callback_handler(update: Any, _context: Any, service: BotService) -> None:
    query = update.callback_query
    await query.answer()
    telegram_user_id, username = telegram_user_from_update(update)
    response = service.handle_callback(telegram_user_id, username, query.data)
    reply_markup = build_inline_keyboard(response.keyboard) if response.keyboard else None
    await query.edit_message_text(response.text, reply_markup=reply_markup)
