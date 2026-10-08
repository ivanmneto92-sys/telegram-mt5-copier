"""Aviso de "sinais retomados" quando a pausa do cliente termina sozinha
(Parar sinais hoje, ou a pausa automatica de meta/limite diario), no
Telegram e no app (push). A pausa "ate eu religar" nunca termina sozinha,
entao nunca gera este aviso."""

from __future__ import annotations

import logging
from pathlib import Path

from .database import connect_database, utc_now
from .web_push import ExpiredPushSubscription, PushSubscription, WebPushSender

RESUME_MESSAGE = (
    "▶️ SINAIS RETOMADOS\n\n"
    "A pausa terminou e novos sinais voltam a abrir operações na sua conta.\n\n"
    "Se não quiser operar agora, pause de novo pelo app ou pelo bot."
)


def notify_resumed_signal_pauses(
    database_path: Path,
    notifier: object,
    push_sender: WebPushSender | None,
    *,
    logger: logging.Logger | None = None,
) -> int:
    log = logger or logging.getLogger(__name__)
    with connect_database(database_path) as connection:
        rows = connection.execute(
            """
            SELECT id, telegram_user_id, daily_signal_pause_until FROM users
            WHERE status = 'active'
              AND daily_signal_pause_until IS NOT NULL
              AND daily_signal_pause_until NOT LIKE '9999-%'
              AND datetime(daily_signal_pause_until) <= datetime('now')
              AND datetime(daily_signal_pause_until) >= datetime('now', '-30 minutes')
              AND (signal_pause_resume_notified_for IS NULL
                   OR signal_pause_resume_notified_for != daily_signal_pause_until)
            """
        ).fetchall()
    sent = 0
    for user_id, telegram_user_id, pause_until in rows:
        # Reserva atomica: com mais de um processo de worker rodando, so um
        # envia o aviso desta pausa.
        with connect_database(database_path) as connection:
            cursor = connection.execute(
                """
                UPDATE users SET signal_pause_resume_notified_for = ?, updated_at = ?
                WHERE id = ? AND daily_signal_pause_until = ?
                  AND (signal_pause_resume_notified_for IS NULL
                       OR signal_pause_resume_notified_for != ?)
                """,
                (pause_until, utc_now(), user_id, pause_until, pause_until),
            )
            claimed = cursor.rowcount == 1
            cursor.close()
        if not claimed:
            continue
        sent += 1
        if telegram_user_id:
            try:
                notifier.send(int(telegram_user_id), RESUME_MESSAGE)
            except Exception:
                log.exception("Falha ao avisar retomada no Telegram. user=%s", user_id)
        if push_sender is not None and push_sender.configured:
            _send_push(database_path, push_sender, int(user_id), log)
    return sent


def _send_push(
    database_path: Path, sender: WebPushSender, user_id: int, log: logging.Logger
) -> None:
    with connect_database(database_path) as connection:
        subscriptions = connection.execute(
            "SELECT endpoint, p256dh_key, auth_key FROM push_subscriptions WHERE user_id = ?",
            (user_id,),
        ).fetchall()
    for endpoint, p256dh, auth in subscriptions:
        try:
            sender.send(
                PushSubscription(str(endpoint), str(p256dh), str(auth)),
                title="▶️ Sinais retomados",
                body="A pausa terminou: novos sinais voltam a abrir operações.",
                tag=f"signals-resumed-{user_id}",
                data={"type": "signals_resumed"},
            )
        except ExpiredPushSubscription:
            with connect_database(database_path) as connection:
                connection.execute(
                    "DELETE FROM push_subscriptions WHERE endpoint = ?", (str(endpoint),)
                ).close()
        except Exception:
            log.exception("Falha ao enviar push de retomada. user=%s", user_id)
