from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from hashlib import sha256
import json


class DecisionStatus(StrEnum):
    ACCEPTED = "accepted"
    IGNORED = "ignored"
    REJECTED = "rejected"


class Direction(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


@dataclass(frozen=True)
class IncomingMessage:
    source_chat_id: int | str | None
    source_message_id: int | str | None
    text: str | None = None
    caption: str | None = None
    media_type: str = "text"


@dataclass(frozen=True)
class TradeSignal:
    symbol: str
    direction: Direction
    entry_low: Decimal
    entry_high: Decimal
    stop_loss: Decimal
    take_profits: tuple[Decimal, ...]
    raw_text: str
    source_chat_id: int | str | None = None
    source_message_id: int | str | None = None
    clean_message: str | None = None

    @property
    def content_signature(self) -> str:
        payload = {
            "symbol": self.symbol,
            "direction": self.direction.value,
            "entry_low": decimal_to_text(self.entry_low),
            "entry_high": decimal_to_text(self.entry_high),
            "stop_loss": decimal_to_text(self.stop_loss),
            "take_profits": [decimal_to_text(value) for value in self.take_profits],
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return sha256(encoded).hexdigest()

    @property
    def signature(self) -> str:
        if self.source_chat_id is None or self.source_message_id is None:
            return self.content_signature
        encoded = (
            f"{self.content_signature}:{self.source_chat_id}:{self.source_message_id}"
        ).encode("utf-8")
        return sha256(encoded).hexdigest()


@dataclass(frozen=True)
class ProcessingDecision:
    status: DecisionStatus
    reason: str
    signal: TradeSignal | None = None
    formatted_message: str | None = None


def decimal_to_text(value: Decimal) -> str:
    normalized = value.normalize()
    if normalized == normalized.to_integral():
        return str(normalized.quantize(Decimal("1")))
    return format(normalized, "f")
