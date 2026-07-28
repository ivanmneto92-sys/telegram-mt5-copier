from __future__ import annotations

import json
import logging
from urllib import parse, request


class TelegramAdminNotifier:
    def __init__(
        self,
        bot_token: str | None,
        admin_ids: tuple[int, ...],
        *,
        logger: logging.Logger,
        timeout_seconds: int = 15,
    ) -> None:
        self.bot_token = bot_token
        self.admin_ids = admin_ids
        self.logger = logger
        self.timeout_seconds = timeout_seconds

    @property
    def configured(self) -> bool:
        return bool(self.bot_token and self.admin_ids)

    def send(self, message: str) -> bool:
        if not self.configured:
            self.logger.warning(
                "Alerta nao enviado: TELEGRAM_BOT_TOKEN ou BOT_ADMIN_IDS ausente."
            )
            return False

        delivered = False
        for admin_id in self.admin_ids:
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
