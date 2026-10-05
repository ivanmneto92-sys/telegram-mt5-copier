"""Web Push (RFC 8030/8291/8292) para o site/PWA -- canal separado do bot do
Telegram. Usa chaves VAPID proprias (sem servico de terceiro) e manda a
notificacao direto para o endpoint do navegador/celular que fez a inscricao.
"""

from __future__ import annotations

from dataclasses import dataclass
import base64
import binascii
import json
import logging

from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from pywebpush import WebPushException, webpush


@dataclass(frozen=True)
class PushSubscription:
    endpoint: str
    p256dh_key: str
    auth_key: str
    trade_alerts_enabled: bool = True
    daily_summary_enabled: bool = True


def generate_vapid_keypair() -> tuple[str, str]:
    """Gera um par de chaves VAPID novo. Retorna (public_key_b64url, private_key_b64url).

    Roda uma vez na VPS (ver README, secao Web Push) e guarda o resultado em
    VAPID_PUBLIC_KEY/VAPID_PRIVATE_KEY no `.env`. A chave publica tambem e
    enviada ao navegador como `applicationServerKey` na inscricao.
    """
    private_key = ec.generate_private_key(ec.SECP256R1(), default_backend())
    public_key = private_key.public_key()
    private_value = private_key.private_numbers().private_value
    private_raw = private_value.to_bytes(32, byteorder="big")
    public_raw = public_key.public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)
    return _b64url_encode(public_raw), _b64url_encode(private_raw)


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


class WebPushSender:
    def __init__(
        self,
        *,
        vapid_public_key: str | None,
        vapid_private_key: str | None,
        vapid_contact: str,
        logger: logging.Logger | None = None,
    ) -> None:
        self.vapid_public_key = vapid_public_key
        self.vapid_private_key = vapid_private_key
        self.vapid_contact = (
            vapid_contact
            if vapid_contact.startswith(("mailto:", "http://", "https://"))
            else f"mailto:{vapid_contact}"
        )
        self.logger = logger or logging.getLogger(__name__)

    @property
    def configured(self) -> bool:
        return bool(self.vapid_public_key and self.vapid_private_key)

    def send(
        self,
        subscription: PushSubscription,
        *,
        title: str,
        body: str,
        tag: str | None = None,
        data: dict[str, object] | None = None,
    ) -> bool:
        """Envia uma notificacao push. Retorna False (sem lancar excecao) em
        qualquer falha, incluindo inscricao expirada/invalida -- quem chama
        decide se remove a inscricao com base no codigo de erro."""
        if not self.configured:
            return False
        payload = json.dumps(
            {"title": title, "body": body, "tag": tag, "data": data or {}},
            ensure_ascii=False,
        )
        try:
            webpush(
                subscription_info={
                    "endpoint": subscription.endpoint,
                    "keys": {
                        "p256dh": subscription.p256dh_key,
                        "auth": subscription.auth_key,
                    },
                },
                data=payload,
                vapid_private_key=self.vapid_private_key,
                vapid_claims={"sub": self.vapid_contact},
            )
            return True
        except WebPushException as exc:
            status_code = getattr(exc.response, "status_code", None)
            is_expired = status_code in (404, 410)
            self.logger.warning(
                "Falha ao enviar push. endpoint=%s status=%s expirado=%s",
                subscription.endpoint[-24:],
                status_code,
                is_expired,
            )
            if is_expired:
                raise ExpiredPushSubscription(subscription.endpoint) from exc
            return False
        except (binascii.Error, ValueError):
            self.logger.warning("Push com chaves de inscricao invalidas. endpoint=%s", subscription.endpoint[-24:])
            raise ExpiredPushSubscription(subscription.endpoint) from None
        except Exception:
            self.logger.exception("Falha inesperada ao enviar push. endpoint=%s", subscription.endpoint[-24:])
            return False


class ExpiredPushSubscription(Exception):
    """A inscricao nao existe mais do lado do navegador -- deve ser removida."""

    def __init__(self, endpoint: str) -> None:
        super().__init__(endpoint)
        self.endpoint = endpoint
