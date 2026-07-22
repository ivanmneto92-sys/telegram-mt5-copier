from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib import parse, request

from .config import AppConfig
from .database import SignalDatabase, connect_database
from .models import DecisionStatus, IncomingMessage, ProcessingDecision
from .parser import parse_signal_text
from .publisher import TelegramPublisher, format_signal
from .telegram_login import validate_telegram_credentials
from .validator import validate_signal
from .credential_service import CredentialService
from .mt5.account_service import MT5AccountService
from .mt5.client import MT5Client, SimulatedMT5Client
from .mt5.pending_order_executor import PendingOrderExecutor
from .mt5.pending_order_executor import PendingExecutionResult

SUPPORTED_TEXT_MEDIA = {"text", "photo"}
IGNORED_MEDIA = {"video", "audio", "voice", "sticker", "document", "media"}


class SignalProcessor:
    def __init__(
        self,
        database: SignalDatabase,
        publisher: TelegramPublisher,
        logger: logging.Logger,
        pending_order_executor: PendingOrderExecutor | None = None,
        execution_notifier: Callable[[PendingExecutionResult], Awaitable[None]] | None = None,
    ) -> None:
        self.database = database
        self.publisher = publisher
        self.logger = logger
        self.pending_order_executor = pending_order_executor
        self.execution_notifier = execution_notifier
        self._closed = False

    def close(self) -> None:
        if self._closed:
            return
        self.database.close()
        if self.pending_order_executor is not None:
            self.pending_order_executor.close()
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
        if self.database.has_duplicate(signal.content_signature):
            duplicate_decision = ProcessingDecision(DecisionStatus.IGNORED, "duplicate_signal", signal=signal)
            self.database.record_event(duplicate_decision.status, duplicate_decision.reason, signal=signal)
            self.logger.info("%s: %s", duplicate_decision.status.value, duplicate_decision.reason)
            return duplicate_decision

        formatted_message = format_signal(signal)
        await self.publisher.publish(signal, formatted_message, client=client)
        self.database.record_accepted(signal, formatted_message)
        if self.pending_order_executor is not None:
            execution_results = await asyncio.to_thread(
                self.pending_order_executor.execute_for_signal,
                signal,
            )
            for result in execution_results:
                if result.group_result.duplicate:
                    self.logger.info(
                        "pending_order_execution: duplicada para user_id=%s account_id=%s",
                        result.account.user_id,
                        result.account.id,
                    )
                    continue
                self.logger.info("pending_order_execution:\n%s", result.message)
                if self.execution_notifier is not None and result.message:
                    await self.execution_notifier(result)
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
    pending_order_executor = None
    if config.mt5_execution_mode in {"simulation", "demo_execution", "live_execution"}:
        credential_service = CredentialService(config.mt5_credential_key) if config.mt5_credential_key else None
        if config.mt5_execution_mode in {"demo_execution", "live_execution"} and credential_service is None:
            raise ValueError("MT5_CREDENTIAL_KEY obrigatoria para execucao MT5.")
        mt5_accounts = MT5AccountService(
            config.database_path,
            credential_service=credential_service,
            allow_live_accounts=config.allow_live_accounts,
            max_accounts_per_vps=config.mt5_max_accounts_per_vps,
        )
        pending_order_executor = PendingOrderExecutor(
            config.database_path,
            mt5_accounts,
            execution_mode=config.mt5_execution_mode,
            global_kill_switch=config.global_execution_kill_switch,
            allow_live_accounts=config.allow_live_accounts,
            client_factory=(
                MT5Client
                if config.mt5_execution_mode in {"demo_execution", "live_execution"}
                else SimulatedMT5Client
            ),
        )
    execution_notifier = None
    if pending_order_executor is not None and config.telegram_bot_token:
        async def execution_notifier(result: PendingExecutionResult) -> None:
            await notify_management_user(
                config.telegram_bot_token,
                config.database_path,
                result.account.user_id,
                result.message,
                logger,
            )
    processor = SignalProcessor(
        database,
        publisher,
        logger,
        pending_order_executor=pending_order_executor,
        execution_notifier=execution_notifier,
    )

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

        @client.on(events.MessageEdited(chats=int(config.source_chat_id)))
        async def handle_edited_message(event: Any) -> None:
            incoming = incoming_from_telethon_event(event)
            if database.has_accepted_source_message(
                incoming.source_chat_id,
                incoming.source_message_id,
            ):
                database.record_event(
                    DecisionStatus.IGNORED,
                    "edited_signal_already_processed",
                    incoming=incoming,
                )
                return
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


async def notify_management_user(
    bot_token: str,
    database_path: Path,
    user_id: int,
    message: str,
    logger: logging.Logger,
) -> None:
    with connect_database(database_path) as connection:
        cursor = connection.execute("SELECT telegram_user_id FROM users WHERE id = ?", (user_id,))
        try:
            row = cursor.fetchone()
        finally:
            cursor.close()
    if row is None:
        return

    def send() -> None:
        payload = parse.urlencode({"chat_id": str(row[0]), "text": message}).encode("utf-8")
        api_request = request.Request(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            data=payload,
            method="POST",
        )
        with request.urlopen(api_request, timeout=15) as response:
            body = json.loads(response.read().decode("utf-8"))
            if not body.get("ok"):
                raise RuntimeError("telegram_notification_rejected")

    try:
        await asyncio.to_thread(send)
    except Exception as exc:
        logger.error("Falha ao notificar resultado MT5 para user_id=%s: %s", user_id, exc)
