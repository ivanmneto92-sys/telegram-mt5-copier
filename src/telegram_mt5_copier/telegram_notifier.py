from __future__ import annotations

import json
import logging
from pathlib import Path
from urllib import parse, request

from .database import connect_database


class TelegramAdminNotifier:
    def __init__(
        self,
        bot_token: str | None,
        admin_ids: tuple[int, ...],
        *,
        database_path: Path | None = None,
        logger: logging.Logger,
        timeout_seconds: int = 15,
    ) -> None:
        self.bot_token = bot_token
        self.admin_ids = admin_ids
        self.database_path = database_path
        self.logger = logger
        self.timeout_seconds = timeout_seconds

    @property
    def configured(self) -> bool:
        return bool(self.bot_token and self.admin_ids)

    def _recipient_ids(self) -> tuple[int, ...]:
        """BOT_ADMIN_IDS (.env) + admins ativos adicionados pelo painel web.

        Sem isso, um admin so-roster nunca receberia alertas operacionais
        (sincronizacao travada, heartbeat parado, etc.) por Telegram --
        so quem esta em BOT_ADMIN_IDS receberia, mesmo que o admin so-roster
        ja opere o painel normalmente.
        """
        ids = set(self.admin_ids)
        if self.database_path is not None:
            try:
                with connect_database(self.database_path) as connection:
                    rows = connection.execute(
                        "SELECT telegram_user_id FROM admin_roster WHERE revoked_at IS NULL"
                    ).fetchall()
                ids.update(int(row[0]) for row in rows)
            except Exception:
                self.logger.exception("Falha ao resolver admins do roster para alerta operacional.")
        return tuple(ids)

    def send(self, message: str) -> bool:
        if not self.configured:
            self.logger.warning(
                "Alerta nao enviado: TELEGRAM_BOT_TOKEN ou BOT_ADMIN_IDS ausente."
            )
            return False

        delivered = False
        for admin_id in self._recipient_ids():
            try:
                payload = parse.urlencode(
                    {"chat_id": str(admin_id), "text": message}
                ).encode("utf-8")
                api_request = request.Request(
                    f"https://api.telegram.org/bot{self.bot_token}/sendMessage",
                    data=payload,
                    method="POST",
                )
                with request.urlopen(
                    api_request,
                    timeout=self.timeout_seconds,
                ) as response:
                    body = json.loads(response.read().decode("utf-8"))
                if not body.get("ok"):
                    raise RuntimeError("telegram_notification_rejected")
                delivered = True
            except Exception as exc:
                self.logger.error(
                    "Falha ao enviar alerta operacional para admin_id=%s tipo=%s",
                    admin_id,
                    type(exc).__name__,
                )
        return delivered


class TelegramUserNotifier:
    """Small synchronous Bot API client used by the MT5 worker outbox."""

    def __init__(
        self,
        bot_token: str | None,
        *,
        logger: logging.Logger,
        timeout_seconds: int = 10,
    ) -> None:
        self.bot_token = bot_token
        self.logger = logger
        self.timeout_seconds = timeout_seconds

    def send(self, telegram_user_id: int, message: str) -> bool:
        if not self.bot_token:
            return False
        try:
            payload = parse.urlencode(
                {"chat_id": str(telegram_user_id), "text": message}
            ).encode("utf-8")
            api_request = request.Request(
                f"https://api.telegram.org/bot{self.bot_token}/sendMessage",
                data=payload,
                method="POST",
            )
            with request.urlopen(api_request, timeout=self.timeout_seconds) as response:
                body = json.loads(response.read().decode("utf-8"))
            if not body.get("ok"):
                raise RuntimeError("telegram_notification_rejected")
            return True
        except Exception as exc:
            self.logger.warning(
                "Falha ao notificar resultado. user_id=%s tipo=%s",
                telegram_user_id,
                type(exc).__name__,
            )
            return False
