from __future__ import annotations

import asyncio
import sys
from typing import Any

from .config import AppConfig


def main() -> int:
    try:
        config = AppConfig.load(create_dirs=True)
        interactive_login(config)
    except Exception as exc:
        print(f"Falha no login do Telegram: {exc}", file=sys.stderr)
        return 2

    return 0


def interactive_login(config: AppConfig) -> None:
    asyncio.run(_interactive_login(config))


async def _interactive_login(config: AppConfig) -> None:
    api_id, api_hash = validate_telegram_credentials(config)

    try:
        from telethon import TelegramClient
    except ImportError as exc:
        raise RuntimeError("Telethon nao instalado. Execute o setup do ambiente primeiro.") from exc

    client = TelegramClient(str(config.telegram_session_name), api_id, api_hash)
    try:
        await client.start()

        me = await client.get_me()
        user_label = getattr(me, "username", None) or getattr(me, "id", "desconhecido")
        print(f"Login Telegram concluido para: {user_label}")
        print(f"Sessao salva em: {config.session_dir}")
        print("Chats disponiveis:")
        await list_available_chats(client)
    finally:
        await client.disconnect()


def validate_telegram_credentials(config: AppConfig) -> tuple[int, str]:
    if not config.telegram_api_id:
        raise ValueError("TELEGRAM_API_ID nao configurado.")
    if not config.telegram_api_hash:
        raise ValueError("TELEGRAM_API_HASH nao configurado.")

    try:
        api_id = int(config.telegram_api_id)
    except ValueError as exc:
        raise ValueError("TELEGRAM_API_ID deve ser numerico.") from exc

    return api_id, config.telegram_api_hash


async def list_available_chats(client: Any) -> None:
    async for dialog in client.iter_dialogs():
        chat_type = telegram_chat_type(dialog.entity)
        if chat_type is None:
            continue

        name = dialog.name or getattr(dialog.entity, "title", "sem nome")
        print(f"Nome: {name} | ID: {dialog.id} | Tipo: {chat_type}")


def telegram_chat_type(entity: object) -> str | None:
    try:
        from telethon.tl.types import Channel, Chat
    except ImportError as exc:
        raise RuntimeError("Telethon nao instalado. Execute o setup do ambiente primeiro.") from exc

    if isinstance(entity, Chat):
        return "grupo"

    if isinstance(entity, Channel):
        if getattr(entity, "megagroup", False):
            return "supergrupo"
        if getattr(entity, "broadcast", False):
            return "canal"
        return "canal"

    return None


if __name__ == "__main__":
    raise SystemExit(main())
