from __future__ import annotations

from datetime import datetime, timedelta, timezone
import logging
from logging.handlers import RotatingFileHandler
import sys
import time

from .config import AppConfig
from .database import update_service_heartbeat
from .market_news import (
    MarketNewsService,
    ForexFactoryCalendarClient,
    TradingEconomicsCalendarClient,
    format_news_alert,
)
from .telegram_notifier import TelegramUserNotifier

SERVICE_NAME = "market_news_monitor"


def build_logger(config: AppConfig) -> logging.Logger:
    logger = logging.getLogger(f"market_news_{config.instance_id}")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    stream = logging.StreamHandler()
    stream.setFormatter(formatter)
    logger.addHandler(stream)
    rotating = RotatingFileHandler(
        config.log_dir / "market-news.log", maxBytes=5 * 1024 * 1024,
        backupCount=5, encoding="utf-8",
    )
    rotating.setFormatter(formatter)
    logger.addHandler(rotating)
    return logger


def main() -> int:
    try:
        config = AppConfig.load(create_dirs=True)
    except Exception as exc:
        print(f"Falha ao carregar calendário financeiro: {exc}", file=sys.stderr)
        return 2
    logger = build_logger(config)
    if not config.market_news_enabled:
        logger.info("Monitor de notícias desativado por MARKET_NEWS_ENABLED=false.")
        return 0
    service = MarketNewsService(
        config.database_path,
        minutes_before=config.market_news_minutes_before,
        minutes_after=config.market_news_minutes_after,
    )
    notifier = TelegramUserNotifier(config.telegram_bot_token, logger=logger)
    client = (
        TradingEconomicsCalendarClient(config.economic_calendar_api_key)
        if config.economic_calendar_api_key
        else ForexFactoryCalendarClient()
    )
    next_fetch = datetime.min.replace(tzinfo=timezone.utc)
    logger.info(
        "Monitor de notícias iniciado. antecedencia=%s tolerancia=%s fonte=%s",
        config.market_news_minutes_before, config.market_news_minutes_after,
        "trading_economics" if config.economic_calendar_api_key else "forex_factory_gratuito",
    )
    while True:
        now = datetime.now(timezone.utc)
        try:
            if client is not None and now >= next_fetch:
                events = client.fetch_high_impact(
                    (now - timedelta(days=1)).date(),
                    (now + timedelta(days=2)).date(),
                )
                service.upsert_events(events)
                next_fetch = now + timedelta(minutes=5)
                logger.info("Calendário atualizado. eventos_fortes=%s", len(events))
            users = service.active_connected_users()
            for event, phase in service.due_events(now):
                for user_id, telegram_user_id, protected in users:
                    if service.notification_sent(event, user_id, phase):
                        continue
                    if notifier.send(
                        telegram_user_id,
                        format_news_alert(event, phase, protected, brand_name=config.brand_name),
                    ):
                        service.mark_notification_sent(event, user_id, phase)
            update_service_heartbeat(
                config.database_path, SERVICE_NAME,
                details="provider=ok",
            )
        except Exception as exc:
            # Fail-open: uma indisponibilidade externa nunca bloqueia sinais sem calendário válido.
            logger.exception("Falha temporária no calendário financeiro: %s", type(exc).__name__)
        time.sleep(config.market_news_poll_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
