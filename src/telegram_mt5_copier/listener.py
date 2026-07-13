from __future__ import annotations

import asyncio
import logging
from typing import Any

from .config import AppConfig
from .database import SignalDatabase
from .models import DecisionStatus, IncomingMessage, ProcessingDecision
from .parser import parse_signal_text
from .publisher import TelegramPublisher, format_signal
from .telegram_login import validate_telegram_credentials
from .validator import validate_signal

SUPPORTED_TEXT_MEDIA = {"text", "photo"}
IGNORED_MEDIA = {"video", "audio", "voice", "sticker", "document", "media"}


class SignalProcessor:
    def __init__(
        self,
        database: SignalDatabase,
        publisher: TelegramPublisher,
        logger: logging.Logger,
    ) -> None:
        self.database = database
        self.publisher = publisher
        self.logger = logger
        self._closed = False

    def close(self) -> None:
        if self._closed:
            return
        self.database.close()
        self._closed = True

    def __enter__(self) -> "SignalProcessor":
        return self

    def __exit__(self, _exc_type: object, _exc: object, _traceback: object) -> None:
        self.close()

    async def process(self, incoming: IncomingMessage, client: Any | None = None) -> ProcessingDecision:
        text_decision = text_for_analysis(incoming)
        if text_decision.status != DecisionStatus.ACCEPTED:
            self.database.record_event(text_decision.status, text_decision.reason, incoming=incoming)
            self.logger.info("%s: %s", text_decision.status.value, text_decision.reason)
            return text_decision

        raw_text = text_decision.formatted_message or ""
        parse_decision = parse_signal_text(
            raw_text,
            source_chat_id=incoming.source_chat_id,
            source_message_id=incoming.source_message_id,
        )
        if parse_decision.status != DecisionStatus.ACCEPTED or parse_decision.signal is None:
            self.database.record_event(
                parse_decision.status,
                parse_decision.reason,
                incoming=incoming,
                raw_text=raw_text,
            )
            self.logger.info("%s: %s", parse_decision.status.value, parse_decision.reason)
            return parse_decision

        validation_decision = validate_signal(parse_decision.signal)
        if validation_decision.status != DecisionStatus.ACCEPTED or validation_decision.signal is None:
            self.database.record_event(
                validation_decision.status,
                validation_decision.reason,
                signal=parse_decision.signal,
            )
            self.logger.info("%s: %s", validation_decision.status.value, validation_decision.reason)
            return validation_decision

        signal = validation_decision.signal
        if self.database.has_duplicate(signal.signature):
            duplicate_decision = ProcessingDecision(DecisionStatus.IGNORED, "duplicate_signal", signal=signal)
            self.database.record_event(duplicate_decision.status, duplicate_decision.reason, signal=signal)
            self.logger.info("%s: %s", duplicate_decision.status.value, duplicate_decision.reason)
            return duplicate_decision

        formatted_message = format_signal(signal)
        await self.publisher.publish(signal, formatted_message, client=client)
        self.database.record_accepted(signal, formatted_message)
        accepted_decision = ProcessingDecision(
            DecisionStatus.ACCEPTED,
            "accepted",
            signal=signal,
            formatted_message=formatted_message,
        )
        self.logger.info("accepted: sinal %s %s", signal.symbol, signal.direction.value)
        return accepted_decision


def text_for_analysis(incoming: IncomingMessage) -> ProcessingDecision:
    media_type = (incoming.media_type or "text").lower()
    if media_type == "photo":
        if not incoming.caption or not incoming.caption.strip():
            return ProcessingDecision(DecisionStatus.IGNORED, "image_without_caption")
        return ProcessingDecision(
            DecisionStatus.ACCEPTED,
            "caption",
            formatted_message=incoming.caption.strip(),
        )

    if media_type in IGNORED_MEDIA or media_type not in SUPPORTED_TEXT_MEDIA:
        return ProcessingDecision(DecisionStatus.IGNORED, f"unsupported_media:{media_type}")

    if not incoming.text or not incoming.text.strip():
        return ProcessingDecision(DecisionStatus.IGNORED, "empty_message")

    return ProcessingDecision(DecisionStatus.ACCEPTED, "text", formatted_message=incoming.text.strip())


async def run_telegram_listener(config: AppConfig, logger: logging.Logger) -> int:
    api_id, api_hash = validate_telegram_credentials(config)
    if not config.source_chat_id:
        raise ValueError("SOURCE_CHAT_ID nao configurado.")

    try:
        from telethon import TelegramClient, events
    except ImportError as exc:
        raise RuntimeError("Telethon nao instalado. Execute o setup do ambiente primeiro.") from exc

    database = SignalDatabase(config.database_path)
    database.initialize()
    publisher = TelegramPublisher(config, logger)
    processor = SignalProcessor(database, publisher, logger)

    client = TelegramClient(str(config.telegram_session_name), api_id, api_hash)
    await client.connect()
    try:
        if not await client.is_user_authorized():
            logger.error("Sessao Telegram nao autenticada. Execute telegram-login uma vez antes do monitoramento.")
            return 2

        @client.on(events.NewMessage(chats=int(config.source_chat_id)))
        async def handle_new_message(event: Any) -> None:
            incoming = incoming_from_telethon_event(event)
            await processor.process(incoming, client=client)

        logger.info("Monitoramento Telegram iniciado. DRY_RUN=%s", str(config.dry_run).lower())
        await client.run_until_disconnected()
        return 0
    finally:
        processor.close()
        await client.disconnect()


def incoming_from_telethon_event(event: Any) -> IncomingMessage:
    message = event.message
    media_type = classify_telethon_message(message)
    message_text = getattr(message, "message", None) or None

    return IncomingMessage(
        source_chat_id=getattr(event, "chat_id", None),
        source_message_id=getattr(message, "id", None),
        text=message_text if media_type == "text" else None,
        caption=message_text if media_type == "photo" else None,
        media_type=media_type,
    )


def classify_telethon_message(message: Any) -> str:
    if getattr(message, "photo", None):
        return "photo"
    if getattr(message, "video", None):
        return "video"
    if getattr(message, "audio", None):
        return "audio"
    if getattr(message, "voice", None):
        return "voice"
    if getattr(message, "sticker", None):
        return "sticker"
    if getattr(message, "document", None):
        return "document"
    if getattr(message, "media", None):
        return "media"
    return "text"


def run_listener(config: AppConfig, logger: logging.Logger) -> int:
    try:
        return asyncio.run(run_telegram_listener(config, logger))
    except KeyboardInterrupt:
        logger.info("Monitoramento interrompido pelo usuario.")
        return 0
