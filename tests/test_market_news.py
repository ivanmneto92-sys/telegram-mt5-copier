from datetime import datetime, timedelta, timezone

from telegram_mt5_copier.database import connect_database, initialize_database, utc_now
from telegram_mt5_copier.market_news import (
    EconomicEvent,
    MarketNewsService,
    currencies_for_symbol,
)
from telegram_mt5_copier.settings_service import SettingsService


def create_user(database_path) -> int:
    initialize_database(database_path)
    with connect_database(database_path) as connection:
        cursor = connection.execute(
            "INSERT INTO users(telegram_user_id,telegram_username,status,created_at,updated_at) VALUES(?,?,?,?,?)",
            (123, "cliente", "active", utc_now(), utc_now()),
        )
        return int(cursor.lastrowid)


def test_symbol_currency_mapping_supports_gold_and_forex():
    assert currencies_for_symbol("XAUUSDb") == {"USD"}
    assert currencies_for_symbol("EURUSDc") == {"EUR", "USD"}
    assert currencies_for_symbol("CADCHF") == {"CAD", "CHF"}
    assert currencies_for_symbol("NAS100.cash") == {"USD"}


def test_news_protection_is_opt_in_and_currency_specific(tmp_path):
    database_path = tmp_path / "news.sqlite3"
    user_id = create_user(database_path)
    now = datetime.now(timezone.utc)
    service = MarketNewsService(database_path, minutes_before=10, minutes_after=10)
    usd_event = EconomicEvent("1", now + timedelta(minutes=5), "United States", "USD", "Non Farm Payrolls")
    service.upsert_events((usd_event,))

    SettingsService(database_path).ensure_defaults(user_id)
    assert service.blocking_event(user_id, "XAUUSD", now) is None

    SettingsService(database_path).update_avoid_high_impact_news(user_id, True)
    assert service.blocking_event(user_id, "XAUUSD", now) == usd_event
    assert service.blocking_event(user_id, "EURGBP", now) is None


def test_news_outside_window_does_not_block(tmp_path):
    database_path = tmp_path / "news.sqlite3"
    user_id = create_user(database_path)
    now = datetime.now(timezone.utc)
    service = MarketNewsService(database_path)
    service.upsert_events((EconomicEvent("2", now + timedelta(minutes=11), "United States", "USD", "CPI"),))
    SettingsService(database_path).update_avoid_high_impact_news(user_id, True)
    assert service.blocking_event(user_id, "XAUUSD", now) is None


def test_disabled_calendar_never_uses_cached_event(tmp_path):
    database_path = tmp_path / "news.sqlite3"
    user_id = create_user(database_path)
    now = datetime.now(timezone.utc)
    writer = MarketNewsService(database_path)
    writer.upsert_events((EconomicEvent("3", now, "United States", "USD", "Fed decision"),))
    SettingsService(database_path).update_avoid_high_impact_news(user_id, True)
    disabled = MarketNewsService(database_path, enabled=False)
    assert disabled.blocking_event(user_id, "XAUUSD", now) is None
