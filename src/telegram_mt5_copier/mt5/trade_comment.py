from __future__ import annotations

from dataclasses import dataclass
import re
import unicodedata


# MT5 documenta 31 caracteres, mas alguns terminais/corretoras (ex.: white-label)
# rejeitam com "Invalid comment argument" perto desse limite; 26 é o valor seguro
# observado na prática.
MT5_COMMENT_MAX_LENGTH = 26
LEGACY_COMMENT_RE = re.compile(
    r"^tgcp (?P<signal>[0-9a-f]{8}) TP(?P<tp>\d+)$",
    re.IGNORECASE,
)
ROOM_COMMENT_RE = re.compile(
    r"^.+ (?P<signal>[0-9a-f]{8}) T(?P<tp>\d+)$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ParsedTradeComment:
    signal_prefix: str
    tp_index: int


def build_trade_comment(
    room_name: str | None,
    signal_id: str,
    tp_index: int,
) -> str:
    signal_prefix = signal_id[:8].lower()
    if not room_name:
        return f"tgcp {signal_prefix} TP{tp_index}"
    suffix = f" {signal_prefix} T{tp_index}"
    available = MT5_COMMENT_MAX_LENGTH - len(suffix)
    compact_name = sanitize_room_name(room_name)[:available].rstrip(" .-_")
    if not compact_name:
        compact_name = "Sala"[:available]
    return f"{compact_name}{suffix}"


def parse_trade_comment(comment: str) -> ParsedTradeComment | None:
    for pattern in (LEGACY_COMMENT_RE, ROOM_COMMENT_RE):
        match = pattern.match(comment.strip())
        if match is not None:
            return ParsedTradeComment(
                signal_prefix=match.group("signal").lower(),
                tp_index=int(match.group("tp")),
            )
    return None


def sanitize_room_name(name: str) -> str:
    normalized = unicodedata.normalize("NFKD", name)
    ascii_name = normalized.encode("ascii", "ignore").decode("ascii")
    cleaned = re.sub(r"[^A-Za-z0-9 _.-]+", " ", ascii_name)
    return " ".join(cleaned.split())
