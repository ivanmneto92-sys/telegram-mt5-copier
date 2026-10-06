from datetime import datetime, timedelta, timezone

from telegram_mt5_copier.database import connect_database, initialize_database, utc_now
from telegram_mt5_copier.market_news import (
    EconomicEvent,
    ForexFactoryCalendarClient,
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


def test_forex_factory_parser_keeps_only_high_impact(monkeypatch):
    payload = b'''[
      {"title":"Core CPI m/m","country":"USD","date":"2026-08-12T08:30:00-04:00","impact":"High","forecast":"0.2%","previous":"0.0%"},
      {"title":"Minor report","country":"USD","date":"2026-08-12T10:30:00-04:00","impact":"Low","forecast":"","previous":""}
    ]'''

    class Response:
        def __enter__(self): return self
        def __exit__(self, *_args): return None
        def read(self): return payload

    monkeypatch.setattr("telegram_mt5_copier.market_news.request.urlopen", lambda *_args, **_kwargs: Response())
    events = ForexFactoryCalendarClient().fetch_high_impact(
        datetime(2026, 8, 12, tzinfo=timezone.utc).date(),
        datetime(2026, 8, 13, tzinfo=timezone.utc).date(),
    )
    assert len(events) == 1
    assert events[0].currency == "USD"
    assert events[0].provider == "forex_factory"
    assert events[0].event_at == datetime(2026, 8, 12, 12, 30, tzinfo=timezone.utc)


def _add_account(database_path, user_id: int, product_kind: str) -> None:
    with connect_database(database_path) as connection:
        connection.execute(
            """INSERT INTO mt5_accounts (user_id, account_alias, broker_name, terminal_path,
               server_name, login, encrypted_password, account_type, account_mode,
               connection_status, product_kind, created_at, updated_at)
               VALUES (?, 'Conta', 'HFM', 't.exe', 'HFM-Live', '111', 'x', 'real', 'hedging',
                       'connected', ?, ?, ?)""",
            (user_id, product_kind, utc_now(), utc_now()),
        )
        connection.execute(
            """INSERT INTO push_subscriptions (user_id, endpoint, p256dh_key, auth_key,
               created_at, updated_at) VALUES (?, ?, 'k', 'a', ?, ?)""",
            (user_id, f"https://push.example/{user_id}", utc_now(), utc_now()),
        )


def test_news_push_reaches_copy_trader_and_automatic_once(tmp_path):
    from telegram_mt5_copier.market_news_monitor import send_news_push
    from telegram_mt5_copier.web_push import ExpiredPushSubscription

    database_path = tmp_path / "news.sqlite3"
    automatic = create_user(database_path)
    with connect_database(database_path) as connection:
        copy_user = int(connection.execute(
            "INSERT INTO users(telegram_user_id,telegram_username,status,created_at,updated_at) VALUES(?,?,?,?,?)",
            (456, "copy", "active", utc_now(), utc_now()),
        ).lastrowid)
    _add_account(database_path, automatic, "signal_copier")
    _add_account(database_path, copy_user, "broker_copy")
    SettingsService(database_path).update_avoid_high_impact_news(automatic, True)
    service = MarketNewsService(database_path)
    event = EconomicEvent("9", datetime.now(timezone.utc) + timedelta(minutes=5), "United States", "USD", "CPI")

    class Sender:
        configured = True

        def __init__(self) -> None:
            self.sent: list[tuple[str, str, str]] = []

        def send(self, subscription, *, title, body, tag=None, data=None):
            self.sent.append((subscription.endpoint, title, body))
            return True

    sender = Sender()
    send_news_push(service, sender, event, "before")
    send_news_push(service, sender, event, "before")  # nao repete

    by_endpoint = {endpoint: body for endpoint, _title, body in sender.sent}
    assert len(sender.sent) == 2
    assert "Proteção ativa" in by_endpoint[f"https://push.example/{automatic}"]
    assert "Mercado pode ficar volátil" in by_endpoint[f"https://push.example/{copy_user}"]
    assert all(title == "🔴 Notícia forte em 10 min" for _e, title, _b in sender.sent)

    class ExpiredSender(Sender):
        def send(self, subscription, **kwargs):
            raise ExpiredPushSubscription(subscription.endpoint)

    send_news_push(service, ExpiredSender(), event, "now")
    assert service.push_subscriptions(automatic) == ()
