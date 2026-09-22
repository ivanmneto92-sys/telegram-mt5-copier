from __future__ import annotations

from collections import deque
import logging
from typing import Any

from .config import AppConfig
from .models import TradeSignal, decimal_to_text
from .signal_formatter import format_signal as format_clean_signal


class TelegramPublisher:
    # Memoria das mensagens que o proprio publisher enviou, so pra este
    # processo reconhecer e ignorar o eco delas quando DESTINATION_CHAT_ID e
    # tambem um SOURCE_CHAT_IDS (canal que serve de origem de sinais manuais e
    # de destino das republicacoes). Nao sobrevive a reinicio -- aceitavel: o
    # pior caso de perder essa memoria num restart e o mesmo comportamento de
    # antes desta protecao existir, e a execucao de ordem real ja tem sua
    # propria trava independente por conta (ver claim_account_signal em
    # mt5/execution_repository.py), entao um eco escapando daqui nunca chega a
    # abrir uma ordem duplicada -- so evita ficar registrado como sinal novo.
    _ECHO_MEMORY_PER_CHAT = 500

    def __init__(self, config: AppConfig, logger: logging.Logger) -> None:
        self.config = config
        self.logger = logger
        self._sent_message_ids: dict[str, deque[int]] = {}

    async def publish(self, signal: TradeSignal, formatted_message: str, client: Any | None = None) -> None:
        if self.config.dry_run:
            self.logger.info("DRY_RUN: publicacao simulada:\n%s", formatted_message)
            return

        if client is None:
            raise RuntimeError("Cliente Telegram indisponivel para publicacao.")
        if not self.config.destination_chat_id:
            raise RuntimeError("DESTINATION_CHAT_ID nao configurado.")

        sent_message = await client.send_message(int(self.config.destination_chat_id), formatted_message)
        self._remember_sent(self.config.destination_chat_id, getattr(sent_message, "id", None))

    def _remember_sent(self, chat_id: int | str, message_id: object) -> None:
        if message_id is None:
            return
        try:
            message_id = int(message_id)
        except (TypeError, ValueError):
            return
        self._sent_message_ids.setdefault(
            str(chat_id), deque(maxlen=self._ECHO_MEMORY_PER_CHAT)
        ).append(message_id)

    def is_own_echo(self, chat_id: object, message_id: object) -> bool:
        """True se esta mensagem e o eco de algo que este publisher mandou.

        Reconhece pelo id exato da mensagem enviada, nao pelo conteudo -- um
        sinal digitado manualmente de novo, com o mesmo texto, tem um id
        diferente e continua sendo processado normalmente.
        """
        if chat_id is None or message_id is None:
            return False
        try:
            message_id = int(message_id)
        except (TypeError, ValueError):
            return False
        return message_id in self._sent_message_ids.get(str(chat_id), ())


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
