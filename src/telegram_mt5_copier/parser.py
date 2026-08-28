from __future__ import annotations

from decimal import Decimal, InvalidOperation
import re

from .models import DecisionStatus, Direction, ProcessingDecision, TradeSignal
from .signal_formatter import (
    HEADER_RE,
    clean_signal_text,
    extract_signal_direction,
    extract_signal_symbol,
)

NUMBER_TEXT = r"\d+(?:[\.,]\d+)?"
NUMBER_RE = re.compile(NUMBER_TEXT)
RANGE_RE = re.compile(rf"(?P<first>{NUMBER_TEXT})\s*[-–—_]\s*(?P<second>{NUMBER_TEXT})")

OTHER_ASSET_RE = re.compile(
    r"\b(?:EUR\s*/?\s*USD|GBP\s*/?\s*USD|USD\s*/?\s*JPY|BTC\s*/?\s*USD|"
    r"ETH\s*/?\s*USD|US30|NAS100|SPX500|US500|WTI|OIL)\b",
    re.IGNORECASE,
)
DIRECTION_RE = re.compile(r"\b(BUY|SELL|COMPRA|VENDA)\b", re.IGNORECASE)
ENTRY_LABEL_RE = re.compile(
    r"\b(?:ENTRY(?:\s+ZONE)?|ENTER|ENTRADA|(?:IN\s+)?ZONE)\b",
    re.IGNORECASE,
)
CLOSED_TRADES_REPORT_RE = re.compile(r"\bCLOSED\s+TRADES?\b", re.IGNORECASE)
RESULT_REPORT_RE = re.compile(r"\b(?:RESULTS?|PIPS?)\b", re.IGNORECASE)
SL_LABEL_RE = re.compile(r"\b(?:SL|STOP(?:\s+LOSS)?)\b", re.IGNORECASE)
TP_LABEL_RE = re.compile(r"\b(?:TP\d*|TARGET\d*|TAKE\s+PROFIT\d*)\b", re.IGNORECASE)
INDEXED_TP_LABEL_RE = re.compile(
    r"\b(?:TP|TARGET|TAKE\s+PROFIT)\s+\d+\s*:",
    re.IGNORECASE,
)


def parse_signal_text(
    text: str,
    *,
    source_chat_id: int | str | None = None,
    source_message_id: int | str | None = None,
) -> ProcessingDecision:
    clean_text = text.strip()
    if not clean_text:
        return ProcessingDecision(DecisionStatus.IGNORED, "empty_message")

    if CLOSED_TRADES_REPORT_RE.search(clean_text) or (
        RESULT_REPORT_RE.search(clean_text) and not ENTRY_LABEL_RE.search(clean_text)
    ):
        return ProcessingDecision(DecisionStatus.IGNORED, "performance_report")

    symbol = extract_signal_symbol(clean_text)
    if symbol is None:
        if looks_like_trade_message(clean_text) or OTHER_ASSET_RE.search(clean_text):
            return ProcessingDecision(DecisionStatus.REJECTED, "unsupported_asset")
        return ProcessingDecision(DecisionStatus.IGNORED, "common_message")

    if not HEADER_RE.search(clean_text):
        return ProcessingDecision(DecisionStatus.REJECTED, "missing_symbol_direction")

    direction_match = DIRECTION_RE.search(clean_text)
    if direction_match:
        direction = direction_from_text(direction_match.group(1))
    else:
        # Some channels never spell BUY/SELL as text at all -- direction is
        # a custom emoji instead, with no literal word for DIRECTION_RE to
        # find anywhere in the message. HEADER_RE already knows how to pull
        # a direction out of those (e.g. the arrow next to NOW), so fall
        # back to that before giving up.
        header_direction = extract_signal_direction(clean_text)
        if header_direction is None:
            return ProcessingDecision(DecisionStatus.REJECTED, "missing_direction")
        direction = direction_from_text(header_direction)

    cleaned_signal = clean_signal_text(clean_text)
    lines = [line.strip() for line in (cleaned_signal or clean_text).splitlines() if line.strip()]

    try:
        entry_range = extract_entry_range(lines)
        stop_loss = extract_stop_loss(lines)
        take_profits = extract_take_profits(lines)
    except ValueError as exc:
        return ProcessingDecision(DecisionStatus.REJECTED, str(exc))

    if entry_range is None:
        return ProcessingDecision(DecisionStatus.REJECTED, "missing_entry")
    if stop_loss is None:
        return ProcessingDecision(DecisionStatus.REJECTED, "missing_stop_loss")
    if not take_profits:
        return ProcessingDecision(DecisionStatus.REJECTED, "missing_take_profit")

    signal = TradeSignal(
        symbol=symbol,
        direction=direction,
        entry_low=entry_range[0],
        entry_high=entry_range[1],
        stop_loss=stop_loss,
        take_profits=tuple(take_profits),
            raw_text=clean_text,
            source_chat_id=source_chat_id,
            source_message_id=source_message_id,
            clean_message=cleaned_signal,
        )
    return ProcessingDecision(DecisionStatus.ACCEPTED, "parsed", signal=signal)


def looks_like_trade_message(text: str) -> bool:
    has_direction = bool(DIRECTION_RE.search(text))
    has_order_labels = any(
        pattern.search(text) for pattern in (ENTRY_LABEL_RE, SL_LABEL_RE, TP_LABEL_RE)
    )
    return has_direction and has_order_labels


def extract_entry_range(lines: list[str]) -> tuple[Decimal, Decimal] | None:
    for index, line in enumerate(lines):
        match = ENTRY_LABEL_RE.search(line)
        if not match:
            continue

        segment = line[match.end() :]
        if not NUMBER_RE.search(segment) and index + 1 < len(lines):
            segment = f"{segment} {lines[index + 1]}"
        return parse_price_range(segment)

    return None


def extract_stop_loss(lines: list[str]) -> Decimal | None:
    for line in lines:
        match = SL_LABEL_RE.search(line)
        if not match:
            continue

        segment = line[match.end() :]
        return first_decimal(segment)

    return None


def extract_take_profits(lines: list[str]) -> list[Decimal]:
    take_profits: list[Decimal] = []
    for line in lines:
        match = INDEXED_TP_LABEL_RE.search(line) or TP_LABEL_RE.search(line)
        if not match:
            continue

        segment = line[match.end() :]
        take_profits.extend(all_decimals(segment))

    return take_profits


def parse_price_range(text: str) -> tuple[Decimal, Decimal] | None:
    range_match = RANGE_RE.search(text)
    if range_match:
        first_text = range_match.group("first")
        second_text = range_match.group("second")
        first = decimal_from_text(first_text)
        second = expand_abbreviated_price(first_text, second_text)
        return (min(first, second), max(first, second))

    value = first_decimal(text)
    if value is None:
        return None
    return (value, value)


def expand_abbreviated_price(first_text: str, second_text: str) -> Decimal:
    normalized_first = normalize_number_text(first_text)
    normalized_second = normalize_number_text(second_text)

    first_integer = normalized_first.split(".", 1)[0]
    second_integer, separator, second_fraction = normalized_second.partition(".")

    if len(second_integer) < len(first_integer):
        prefix_length = len(first_integer) - len(second_integer)
        expanded = first_integer[:prefix_length] + second_integer
        if separator:
            expanded = f"{expanded}.{second_fraction}"
        return decimal_from_text(expanded)

    return decimal_from_text(normalized_second)


def first_decimal(text: str) -> Decimal | None:
    match = NUMBER_RE.search(text)
    if not match:
        return None
    return decimal_from_text(match.group(0))


def all_decimals(text: str) -> list[Decimal]:
    return [decimal_from_text(match.group(0)) for match in NUMBER_RE.finditer(text)]


def decimal_from_text(value: str) -> Decimal:
    normalized = normalize_number_text(value)
    try:
        return Decimal(normalized)
    except InvalidOperation as exc:
        raise ValueError("invalid_numeric_value") from exc


def normalize_number_text(value: str) -> str:
    return value.strip().replace(",", ".")


def direction_from_text(value: str) -> Direction:
    normalized = value.upper()
    if normalized in {"BUY", "COMPRA"}:
        return Direction.BUY
    return Direction.SELL
