from __future__ import annotations

import logging
from typing import Any

from .config import AppConfig
from .models import TradeSignal, decimal_to_text
from .signal_formatter import format_signal as format_clean_signal


class TelegramPublisher:
    def __init__(self, config: AppConfig, logger: logging.Logger) -> None:
        self.config = config
        self.logger = logger

    async def publish(self, signal: TradeSignal, formatted_message: str, client: Any | None = None) -> None:
        if self.config.dry_run:
            self.logger.info("DRY_RUN: publicacao simulada:\n%s", formatted_message)
            return

        if client is None:
            raise RuntimeError("Cliente Telegram indisponivel para publicacao.")
        if not self.config.destination_chat_id:
            raise RuntimeError("DESTINATION_CHAT_ID nao configurado.")

        await client.send_message(int(self.config.destination_chat_id), formatted_message)


def format_signal(signal: TradeSignal) -> str:
    if signal.clean_message:
        return format_clean_signal(signal)

    lines = [
        f"{signal.symbol} {signal.direction.value}",
        "",
        f"ENTRY {decimal_to_text(signal.entry_low)}-{decimal_to_text(signal.entry_high)}",
        "",
        f"SL {decimal_to_text(signal.stop_loss)}",
        "",
    ]
    lines.extend(f"TP {decimal_to_text(take_profit)}" for take_profit in signal.take_profits)
    return "\n".join(lines)
