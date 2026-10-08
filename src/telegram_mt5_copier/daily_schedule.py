from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


try:
    SAO_PAULO_TIMEZONE = ZoneInfo("America/Sao_Paulo")
except ZoneInfoNotFoundError:
    SAO_PAULO_TIMEZONE = timezone(timedelta(hours=-3), name="America/Sao_Paulo")

DAILY_SIGNAL_RESUME_HOUR = 23

# "Parar ate eu religar": mesma coluna da pausa diaria (users.
# daily_signal_pause_until), com uma data que nunca chega -- toda checagem
# existente (SQL `<= now`, daily_signal_pause_is_active) ja trata como pausado.
INDEFINITE_SIGNAL_PAUSE_UNTIL = "9999-12-31T00:00:00+00:00"


def is_indefinite_signal_pause(value: object) -> bool:
    return isinstance(value, str) and value.startswith("9999-")


def next_daily_signal_resume_at(now: datetime | None = None) -> datetime:
    local_now = normalized_utc(now).astimezone(SAO_PAULO_TIMEZONE)
    resume_at = local_now.replace(
        hour=DAILY_SIGNAL_RESUME_HOUR,
        minute=0,
        second=0,
        microsecond=0,
    )
    if resume_at <= local_now:
        resume_at += timedelta(days=1)
    while resume_at.weekday() in {4, 5}:
        resume_at += timedelta(days=1)
    return resume_at.astimezone(timezone.utc)


def current_daily_signal_session_start_at(now: datetime | None = None) -> datetime:
    local_now = normalized_utc(now).astimezone(SAO_PAULO_TIMEZONE)
    session_start = local_now.replace(
        hour=DAILY_SIGNAL_RESUME_HOUR,
        minute=0,
        second=0,
        microsecond=0,
    )
    if session_start > local_now:
        session_start -= timedelta(days=1)
    return session_start.astimezone(timezone.utc)


def normalized_utc(value: datetime | None = None) -> datetime:
    current = value or datetime.now(tz=timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc)
