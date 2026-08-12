from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import json
import logging
from pathlib import Path
from urllib import parse, request
from zoneinfo import ZoneInfo

from .database import connect_database, initialize_database, utc_now


COUNTRY_CURRENCIES = {
    "australia": "AUD", "canada": "CAD", "china": "CNY",
    "euro area": "EUR", "eurozone": "EUR", "france": "EUR",
    "germany": "EUR", "italy": "EUR", "japan": "JPY",
    "new zealand": "NZD", "switzerland": "CHF",
    "united kingdom": "GBP", "united states": "USD",
}
KNOWN_CURRENCIES = {"AUD", "CAD", "CHF", "CNY", "EUR", "GBP", "JPY", "NZD", "USD"}


@dataclass(frozen=True)
class EconomicEvent:
    provider_event_id: str
    event_at: datetime
    country: str
    currency: str
    event_name: str
    importance: int = 3
    date_span: int = 0
    category: str = ""
    previous_value: str = ""
    forecast_value: str = ""
    actual_value: str = ""
    source_url: str = ""
    provider: str = "trading_economics"


def parse_event_datetime(value: str) -> datetime:
    normalized = value.strip().replace("Z", "+00:00")
    result = datetime.fromisoformat(normalized)
    return result.replace(tzinfo=timezone.utc) if result.tzinfo is None else result.astimezone(timezone.utc)


def currency_for_country(country: str, raw_currency: str = "") -> str:
    explicit = raw_currency.strip().upper()
    if explicit in KNOWN_CURRENCIES:
        return explicit
    return COUNTRY_CURRENCIES.get(country.strip().casefold(), "")


def currencies_for_symbol(symbol: str) -> set[str]:
    normalized = symbol.upper()
    index_currencies = {
        "US30": "USD", "DJ30": "USD", "NAS100": "USD", "USTEC": "USD",
        "SPX500": "USD", "US500": "USD", "GER40": "EUR", "DE40": "EUR",
        "UK100": "GBP", "JP225": "JPY",
    }
    for prefix, currency in index_currencies.items():
        if normalized.startswith(prefix):
            return {currency}
    letters = "".join(character for character in normalized if character.isalpha())
    if letters.startswith(("XAUUSD", "XAGUSD")):
        return {"USD"}
    base, quote = letters[:3], letters[3:6]
    return {value for value in (base, quote) if value in KNOWN_CURRENCIES}


class TradingEconomicsCalendarClient:
    def __init__(self, api_key: str, *, timeout_seconds: int = 20) -> None:
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds

    def fetch_high_impact(self, start: date, end: date) -> tuple[EconomicEvent, ...]:
        encoded_key = parse.quote(self.api_key, safe="")
        url = (
            "https://api.tradingeconomics.com/calendar/country/All/"
            f"{start.isoformat()}/{end.isoformat()}?c={encoded_key}&importance=3&f=json"
        )
        with request.urlopen(url, timeout=self.timeout_seconds) as response:
            payload = json.loads(response.read().decode("utf-8"))
        if not isinstance(payload, list):
            raise RuntimeError("economic_calendar_invalid_response")
        events: list[EconomicEvent] = []
        for item in payload:
            importance = int(item.get("Importance") or 0)
            date_span = int(item.get("DateSpan") or 0)
            currency = currency_for_country(str(item.get("Country") or ""), str(item.get("Currency") or ""))
            if importance != 3 or date_span != 0 or not currency:
                continue
            event_id = item.get("CalendarId", item.get("CalendarID", item.get("Id")))
            if event_id in {None, ""}:
                event_id = "|".join((str(item.get("Date") or ""), str(item.get("Country") or ""), str(item.get("Event") or "")))
            events.append(EconomicEvent(
                provider_event_id=str(event_id),
                event_at=parse_event_datetime(str(item["Date"])),
                country=str(item.get("Country") or ""),
                currency=currency,
                event_name=str(item.get("Event") or item.get("Category") or "Notícia econômica"),
                category=str(item.get("Category") or ""),
                importance=importance,
                date_span=date_span,
                previous_value=str(item.get("Previous") or ""),
                forecast_value=str(item.get("Forecast") or ""),
                actual_value=str(item.get("Actual") or ""),
                source_url=str(item.get("URL") or ""),
            ))
        return tuple(events)


class MarketNewsService:
    def __init__(self, database_path: Path, *, minutes_before: int = 10, minutes_after: int = 10, enabled: bool = True) -> None:
        self.database_path = database_path
        self.minutes_before = minutes_before
        self.minutes_after = minutes_after
        self.enabled = enabled
        initialize_database(database_path)

    def upsert_events(self, events: tuple[EconomicEvent, ...]) -> None:
        now = utc_now()
        with connect_database(self.database_path) as connection:
            connection.executemany(
                """
                INSERT INTO economic_calendar_events (
                    provider,provider_event_id,event_at,country,currency,event_name,category,
                    importance,date_span,previous_value,forecast_value,actual_value,source_url,fetched_at,updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(provider,provider_event_id) DO UPDATE SET
                    event_at=excluded.event_at,country=excluded.country,currency=excluded.currency,
                    event_name=excluded.event_name,category=excluded.category,importance=excluded.importance,
                    date_span=excluded.date_span,previous_value=excluded.previous_value,
                    forecast_value=excluded.forecast_value,actual_value=excluded.actual_value,
                    source_url=excluded.source_url,fetched_at=excluded.fetched_at,updated_at=excluded.updated_at
                """,
                [(e.provider,e.provider_event_id,e.event_at.isoformat(),e.country,e.currency,e.event_name,e.category,
                  e.importance,e.date_span,e.previous_value,e.forecast_value,e.actual_value,e.source_url,now,now) for e in events],
            )

    def blocking_event(self, user_id: int, symbol: str, now: datetime | None = None) -> EconomicEvent | None:
        if not self.enabled:
            return None
        currencies = currencies_for_symbol(symbol)
        if not currencies:
            return None
        with connect_database(self.database_path) as connection:
            setting = connection.execute(
                "SELECT COALESCE(avoid_high_impact_news,0) FROM user_settings WHERE user_id=?", (user_id,)
            ).fetchone()
            if setting is None or not bool(setting[0]):
                return None
            rows = connection.execute(
                """SELECT provider,provider_event_id,event_at,country,currency,event_name,category,importance,date_span,
                          previous_value,forecast_value,actual_value,source_url
                   FROM economic_calendar_events WHERE importance=3 AND date_span=0"""
            ).fetchall()
        instant = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        for row in rows:
            event_at = parse_event_datetime(str(row[2]))
            if str(row[4]) in currencies and event_at - timedelta(minutes=self.minutes_before) <= instant <= event_at + timedelta(minutes=self.minutes_after):
                return EconomicEvent(str(row[1]), event_at, str(row[3]), str(row[4]), str(row[5]), int(row[7]), int(row[8]),
                                     str(row[6] or ""), str(row[9] or ""), str(row[10] or ""), str(row[11] or ""), str(row[12] or ""), str(row[0]))
        return None

    def active_connected_users(self) -> tuple[tuple[int, int, bool], ...]:
        with connect_database(self.database_path) as connection:
            rows = connection.execute(
                """SELECT DISTINCT u.id,u.telegram_user_id,COALESCE(s.avoid_high_impact_news,0)
                   FROM users u JOIN mt5_accounts a ON a.user_id=u.id
                   LEFT JOIN user_settings s ON s.user_id=u.id
                   WHERE u.status='active' AND a.connection_status='connected'"""
            ).fetchall()
        return tuple((int(r[0]), int(r[1]), bool(r[2])) for r in rows)

    def due_events(self, now: datetime | None = None) -> tuple[tuple[EconomicEvent, str], ...]:
        instant = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        lower = instant - timedelta(minutes=2)
        upper = instant + timedelta(minutes=self.minutes_before)
        with connect_database(self.database_path) as connection:
            rows = connection.execute(
                """SELECT provider,provider_event_id,event_at,country,currency,event_name,category,importance,date_span,
                          previous_value,forecast_value,actual_value,source_url
                   FROM economic_calendar_events WHERE importance=3 AND date_span=0"""
            ).fetchall()
        due: list[tuple[EconomicEvent, str]] = []
        for row in rows:
            event_at = parse_event_datetime(str(row[2]))
            phase = "before" if instant < event_at and event_at - timedelta(minutes=self.minutes_before) <= instant else "now" if lower <= event_at <= instant else ""
            if not phase or event_at > upper:
                continue
            due.append((EconomicEvent(str(row[1]),event_at,str(row[3]),str(row[4]),str(row[5]),int(row[7]),int(row[8]),str(row[6] or ""),str(row[9] or ""),str(row[10] or ""),str(row[11] or ""),str(row[12] or ""),str(row[0])), phase))
        return tuple(due)

    def notification_sent(self, event: EconomicEvent, user_id: int, phase: str) -> bool:
        with connect_database(self.database_path) as connection:
            return connection.execute(
                "SELECT 1 FROM economic_calendar_notifications WHERE provider=? AND provider_event_id=? AND user_id=? AND phase=?",
                (event.provider,event.provider_event_id,user_id,phase),
            ).fetchone() is not None

    def mark_notification_sent(self, event: EconomicEvent, user_id: int, phase: str) -> None:
        with connect_database(self.database_path) as connection:
            connection.execute(
                "INSERT OR IGNORE INTO economic_calendar_notifications(provider,provider_event_id,user_id,phase,sent_at) VALUES(?,?,?,?,?)",
                (event.provider,event.provider_event_id,user_id,phase,utc_now()),
            )


def format_news_alert(event: EconomicEvent, phase: str, protected: bool, *, brand_name: str) -> str:
    local_time = event.event_at.astimezone(ZoneInfo("America/Sao_Paulo")).strftime("%H:%M")
    title = "🔴 NOTÍCIA FORTE EM 10 MINUTOS" if phase == "before" else "🔴 NOTÍCIA FORTE AGORA"
    protection = "🛡️ Proteção ativa: novas entradas relacionadas serão bloqueadas." if protected else "▶️ Operações em notícias estão liberadas na sua configuração."
    details = [title, "", f"{event.currency} — {event.event_name}", f"Horário de Brasília: {local_time}"]
    if event.forecast_value:
        details.append(f"Previsão: {event.forecast_value}")
    if event.previous_value:
        details.append(f"Anterior: {event.previous_value}")
    if phase == "now" and event.actual_value:
        details.append(f"Atual: {event.actual_value}")
    return "\n".join(details + ["", protection, "Ordens já abertas não são alteradas.", "", brand_name])


def format_news_rejection(event: EconomicEvent, account_alias: str) -> str:
    return "\n".join([
        "🛡️ ENTRADA BLOQUEADA POR NOTÍCIA", "", f"Conta: {account_alias}",
        f"Evento: {event.currency} — {event.event_name}",
        "Motivo: proteção contra notícia de alto impacto ativada.", "",
        "Nenhuma ordem nova foi enviada. Ordens já abertas não foram alteradas.",
    ])
