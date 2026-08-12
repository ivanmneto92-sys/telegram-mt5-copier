from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib import parse, request

from .config import AppConfig
from .channel_catalog import ChannelCatalogService
from .database import (
    SIGNAL_MONITOR_SERVICE_NAME,
    SignalDatabase,
    connect_database,
    update_service_heartbeat,
)
from .models import DecisionStatus, IncomingMessage, ProcessingDecision
from .market_news import MarketNewsService
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
SIGNAL_MONITOR_HEARTBEAT_SECONDS = 15
TELEGRAM_RETRY_DELAY_SECONDS = 5
MAX_TELEGRAM_SIGNAL_AGE_SECONDS = 300


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
        if not self.database.claim_signal(signal.content_signature, signal.source_chat_id):
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
    if not config.source_chat_ids:
        raise ValueError("SOURCE_CHAT_IDS nao configurado.")

    try:
        from telethon import TelegramClient, events
    except ImportError as exc:
        raise RuntimeError("Telethon nao instalado. Execute o setup do ambiente primeiro.") from exc

    database = SignalDatabase(config.database_path)
    database.initialize()
    channel_catalog = ChannelCatalogService(config.database_path)
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
            daily_performance_timezone=config.daily_performance_timezone,
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
            news_service=MarketNewsService(
                config.database_path,
                minutes_before=config.market_news_minutes_before,
                minutes_after=config.market_news_minutes_after,
                enabled=bool(
                    config.market_news_enabled and config.economic_calendar_api_key
                ),
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

    client = TelegramClient(
        str(config.telegram_session_name),
        api_id,
        api_hash,
        base_logger=logger,
        **telegram_client_options(),
    )
    await client.connect()
    try:
        if not await client.is_user_authorized():
            logger.error("Sessao Telegram nao autenticada. Execute telegram-login uma vez antes do monitoramento.")
            return 2

        source_entities = await resolve_source_chats(client, config.source_chat_ids, logger)
        for source_chat_id, entity in zip(config.source_chat_ids, source_entities):
            snapshot = await inspect_source_entity(client, entity)
            channel_catalog.register_configured_channel(
                telegram_chat_id=source_chat_id,
                title=telegram_entity_title(entity),
                username=getattr(entity, "username", None),
                content_protected=bool(getattr(entity, "noforwards", False)),
                history_accessible=bool(snapshot["history_accessible"]),
                last_message_id=snapshot["last_message_id"],
            )

        @client.on(events.NewMessage())
        async def handle_new_message(event: Any) -> None:
            incoming = incoming_from_telethon_event(event)
            if not channel_catalog.is_active_chat(incoming.source_chat_id):
                return
            log_incoming_message(logger, incoming, edited=False)
            if telegram_event_is_stale(event, edited=False):
                database.record_event(
                    DecisionStatus.IGNORED,
                    "stale_telegram_message",
                    incoming=incoming,
                )
                logger.info("ignored: stale_telegram_message")
                return
            await processor.process(incoming, client=client)

        @client.on(events.MessageEdited())
        async def handle_edited_message(event: Any) -> None:
            incoming = incoming_from_telethon_event(event)
            if not channel_catalog.is_active_chat(incoming.source_chat_id):
                return
            log_incoming_message(logger, incoming, edited=True)
            if telegram_event_is_stale(event, edited=True):
                database.record_event(
                    DecisionStatus.IGNORED,
                    "stale_telegram_message",
                    incoming=incoming,
                )
                logger.info("ignored: stale_telegram_message")
                return
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

        heartbeat_task = asyncio.create_task(
            run_signal_monitor_heartbeat(
                config.database_path,
                logger,
                is_connected=client.is_connected,
            )
        )
        validation_task = asyncio.create_task(
            run_channel_validation_loop(client, channel_catalog, logger)
        )
        logger.info("Monitoramento Telegram iniciado. DRY_RUN=%s", str(config.dry_run).lower())
        try:
            await client.run_until_disconnected()
            return 0
        finally:
            heartbeat_task.cancel()
            validation_task.cancel()
            try:
                await heartbeat_task
            except asyncio.CancelledError:
                pass
            try:
                await validation_task
            except asyncio.CancelledError:
                pass
    finally:
        processor.close()
        await client.disconnect()


async def resolve_source_chat(client: Any, source_chat_id: str, logger: logging.Logger) -> Any:
    try:
        numeric_chat_id = int(source_chat_id)
    except (TypeError, ValueError) as exc:
        raise ValueError("Cada canal de origem deve ter um ID numérico, como -1001234567890.") from exc

    try:
        entity = await client.get_entity(numeric_chat_id)
    except Exception as exc:
        raise RuntimeError(
            f"Não foi possível acessar o canal de origem {numeric_chat_id}. "
            "Confirme o ID e se a conta da sessão participa do canal."
        ) from exc

    snapshot = await inspect_source_entity(client, entity)
    latest_message_id = snapshot["last_message_id"]
    history_access = "sim" if snapshot["history_accessible"] else "não"

    logger.info(
        "Canal de origem validado. nome=%s id=%s tipo=%s conteudo_protegido=%s "
        "historico_acessivel=%s ultima_mensagem_id=%s",
        telegram_entity_title(entity),
        numeric_chat_id,
        telegram_entity_kind(entity),
        "sim" if bool(getattr(entity, "noforwards", False)) else "não",
        history_access,
        latest_message_id,
    )
    return entity


async def inspect_source_entity(client: Any, entity: Any) -> dict[str, object]:
    latest_message_id: int | str = "nenhuma"
    history_accessible = True
    try:
        messages = await client.get_messages(entity, limit=1)
        if messages:
            latest_message_id = getattr(messages[0], "id", "desconhecida")
    except Exception:
        history_accessible = False
    return {
        "history_accessible": history_accessible,
        "last_message_id": latest_message_id,
    }


async def resolve_source_chats(
    client: Any,
    source_chat_ids: tuple[str, ...],
    logger: logging.Logger,
) -> list[Any]:
    if not source_chat_ids:
        raise ValueError("SOURCE_CHAT_IDS nao configurado.")

    entities = [
        await resolve_source_chat(client, source_chat_id, logger)
        for source_chat_id in source_chat_ids
    ]
    logger.info("Canais de origem prontos. total=%s", len(entities))
    return entities


def telegram_entity_title(entity: Any) -> str:
    return str(
        getattr(entity, "title", None)
        or getattr(entity, "username", None)
        or getattr(entity, "id", "sem_nome")
    )


def telegram_entity_kind(entity: Any) -> str:
    if bool(getattr(entity, "megagroup", False)):
        return "supergrupo"
    if bool(getattr(entity, "broadcast", False)):
        return "canal"
    return "grupo_ou_chat"


def log_incoming_message(
    logger: logging.Logger,
    incoming: IncomingMessage,
    *,
    edited: bool,
) -> None:
    logger.info(
        "Mensagem Telegram recebida. origem=%s mensagem_id=%s tipo=%s editada=%s texto_presente=%s",
        incoming.source_chat_id,
        incoming.source_message_id,
        incoming.media_type or "text",
        "sim" if edited else "não",
        "sim" if bool((incoming.text or incoming.caption or "").strip()) else "não",
    )


async def run_signal_monitor_heartbeat(
    database_path: Path,
    logger: logging.Logger,
    *,
    interval_seconds: int = SIGNAL_MONITOR_HEARTBEAT_SECONDS,
    is_connected: Callable[[], bool] | None = None,
) -> None:
    while True:
        if is_connected is not None and not is_connected():
            await asyncio.sleep(interval_seconds)
            continue
        try:
            await asyncio.to_thread(
                update_service_heartbeat,
                database_path,
                SIGNAL_MONITOR_SERVICE_NAME,
                details="telegram_authorized",
            )
        except Exception as exc:
            logger.error("Falha ao registrar heartbeat do monitor de sinais: %s", exc)
        await asyncio.sleep(interval_seconds)


async def run_channel_validation_loop(
    client: Any,
    catalog: ChannelCatalogService,
    logger: logging.Logger,
    *,
    interval_seconds: int = 30,
) -> None:
    while True:
        try:
            await validate_pending_channels_once(client, catalog, logger)
        except Exception as exc:
            logger.error("Falha ao validar canais sugeridos: %s", exc)
        await asyncio.sleep(interval_seconds)


async def validate_pending_channels_once(
    client: Any,
    catalog: ChannelCatalogService,
    logger: logging.Logger,
) -> None:
    for request_item in await asyncio.to_thread(catalog.pending_validations):
        normalized_key = str(request_item["normalized_key"])
        username = str(request_item["username"])
        try:
            entity = await client.get_entity(username)
            if bool(getattr(entity, "left", False)):
                catalog.mark_awaiting_membership(normalized_key, "monitor_not_member")
                continue
            snapshot = await inspect_source_entity(client, entity)
            if not snapshot["history_accessible"]:
                catalog.mark_awaiting_membership(normalized_key, "history_not_accessible")
                continue
            chat_id = int(getattr(entity, "id"))
            if chat_id > 0 and (
                bool(getattr(entity, "broadcast", False))
                or bool(getattr(entity, "megagroup", False))
            ):
                chat_id = int(f"-100{chat_id}")
            catalog.mark_access_confirmed(
                normalized_key=normalized_key,
                telegram_chat_id=chat_id,
                title=telegram_entity_title(entity),
                username=getattr(entity, "username", None) or username,
                content_protected=bool(getattr(entity, "noforwards", False)),
                history_accessible=True,
                last_message_id=snapshot["last_message_id"],
            )
            logger.info(
                "Canal sugerido pronto para revisao. nome=%s id=%s",
                telegram_entity_title(entity),
                chat_id,
            )
        except Exception as exc:
            await asyncio.to_thread(
                catalog.mark_awaiting_membership,
                normalized_key,
                type(exc).__name__,
            )


def telegram_client_options() -> dict[str, object]:
    return {
        "auto_reconnect": True,
        "connection_retries": None,
        "retry_delay": TELEGRAM_RETRY_DELAY_SECONDS,
        "catch_up": True,
        "sequential_updates": True,
    }


def telegram_event_is_stale(
    event: Any,
    *,
    edited: bool,
    now: datetime | None = None,
    max_age_seconds: int = MAX_TELEGRAM_SIGNAL_AGE_SECONDS,
) -> bool:
    message = getattr(event, "message", None)
    timestamp = getattr(message, "edit_date", None) if edited else None
    timestamp = timestamp or getattr(message, "date", None)
    if not isinstance(timestamp, datetime):
        return False
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    current_time = now or datetime.now(tz=timezone.utc)
    return (current_time - timestamp).total_seconds() > max_age_seconds


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
