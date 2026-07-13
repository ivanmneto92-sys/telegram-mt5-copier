from __future__ import annotations

import asyncio

from .config import AppConfig


def interactive_login(config: AppConfig) -> None:
    asyncio.run(_interactive_login(config))


async def _interactive_login(config: AppConfig) -> None:
    if not config.telegram_api_id:
        raise ValueError("TELEGRAM_API_ID nao configurado.")
    if not config.telegram_api_hash:
        raise ValueError("TELEGRAM_API_HASH nao configurado.")

    try:
        api_id = int(config.telegram_api_id)
    except ValueError as exc:
        raise ValueError("TELEGRAM_API_ID deve ser numerico.") from exc

    try:
        from telethon import TelegramClient
    except ImportError as exc:
        raise RuntimeError("Telethon nao instalado. Execute o setup do ambiente primeiro.") from exc

    client = TelegramClient(str(config.telegram_session_name), api_id, config.telegram_api_hash)
    await client.start()

    me = await client.get_me()
    user_label = getattr(me, "username", None) or getattr(me, "id", "desconhecido")
    print(f"Login Telegram concluido para: {user_label}")
    print(f"Sessao salva em: {config.session_dir}")

    await client.disconnect()
