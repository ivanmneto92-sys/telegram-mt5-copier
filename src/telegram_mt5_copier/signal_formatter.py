from __future__ import annotations

import re

from .models import TradeSignal, decimal_to_text

PRICE_VALUE_PATTERN = r"\d+(?:[\.,]\d+)?(?:\s*[-–—]\s*\d+(?:[\.,]\d+)?)?"
GOLD_ASSET_PATTERN = r"(?:XAU\s*[-/]?\s*USD|GOLD)"
DIRECTION_PATTERN = r"(?:BUY|SELL|COMPRA|VENDA)"
HEADER_RE = re.compile(
    r"(?:"
    rf"\b{GOLD_ASSET_PATTERN}\b[^\w]{{1,8}}\b(?P<asset_first_direction>{DIRECTION_PATTERN})\b"
    r"(?:[ \t]+NOW\b)?"
    r"|"
    rf"\b(?P<direction_first_direction>{DIRECTION_PATTERN})\b[^\w]{{1,8}}"
    rf"\b{GOLD_ASSET_PATTERN}\b"
    r"|"
    rf"\b{GOLD_ASSET_PATTERN}\b[^\r\n]*\r?\n"
    rf"[^\r\n]*\b(?:AN[ÁA]LISE)\b\s*:\s*"
    rf"(?P<localized_direction>{DIRECTION_PATTERN})\b"
    r")",
    re.IGNORECASE,
)
HEADER_TAIL_ENTRY_RE = re.compile(
    rf"^[ \t:@\-\(\)\[\]]*(?:(?:IN[ \t]+)?ZONE\b\s*[:\-]?\s*)?"
    rf"(?P<value>{PRICE_VALUE_PATTERN})",
    re.IGNORECASE,
)
ENTRY_LINE_RE = re.compile(r"\b(?:ENTRY|ENTRADA)\b\s*[:\-]?\s*(?P<value>\d+(?:[\.,]\d+)?(?:\s*[-–—]\s*\d+(?:[\.,]\d+)?)?)", re.IGNORECASE)
SL_LINE_RE = re.compile(
    r"\b(?:SL|STOP[ \t]+LOSS)\b(?:\s*\(\s*SL\s*\))?\s*[:\-]?\s*"
    r"(?P<value>\d+(?:[\.,]\d+)?)",
    re.IGNORECASE,
)
TP_LINE_RE = re.compile(
    r"\b(?:"
    r"TP[ \t]+\d+[ \t]*:|TP\d*+(?![ \t]+\d+[ \t]*:)[ \t]*:?"
    r"|TAKE[ \t]+PROFIT[ \t]+\d+[ \t]*:"
    r"|TAKE[ \t]+PROFIT\d*+(?![ \t]+\d+[ \t]*:)[ \t]*:?"
    r"|TARGET[ \t]+\d+[ \t]*:|TARGET\d*+(?![ \t]+\d+[ \t]*:)[ \t]*:?"
    r")[ \t]*(?P<value>\d+(?:[\.,]\d+)?)",
    re.IGNORECASE,
)


def clean_signal_text(text: str) -> str | None:
    header_match = HEADER_RE.search(text)
    if not header_match:
        return None

    direction = (
        header_match.group("asset_first_direction")
        or header_match.group("direction_first_direction")
        or header_match.group("localized_direction")
    ).upper()
    direction = {"COMPRA": "BUY", "VENDA": "SELL"}.get(direction, direction)
    body = text[header_match.end() :]
    lines = [strip_noise(line) for line in body.splitlines()]

    header_line_tail = body.splitlines()[0] if body else ""
    header_entry_match = HEADER_TAIL_ENTRY_RE.match(header_line_tail)
    entry_value = (
        normalize_range_spacing(header_entry_match.group("value"))
        if header_entry_match
        else None
    )
    stop_loss_value: str | None = None
    take_profit_values: list[str] = []

    for line in lines:
        if not line:
            continue
        if entry_value is None:
            entry_match = ENTRY_LINE_RE.search(line)
            if entry_match:
                entry_value = normalize_range_spacing(entry_match.group("value"))
                continue

        if stop_loss_value is None:
            sl_match = SL_LINE_RE.search(line)
            if sl_match:
                stop_loss_value = sl_match.group("value")
                continue

        for tp_match in TP_LINE_RE.finditer(line):
            take_profit_values.append(tp_match.group("value"))

    if entry_value is None or stop_loss_value is None or not take_profit_values:
        return None

    output = [
        f"XAUUSD {direction}",
        "",
        f"ENTRY {entry_value}",
        "",
        f"SL {stop_loss_value}",
        "",
    ]
    output.extend(f"TP {value}" for value in take_profit_values)
    return "\n".join(output)


def format_signal(signal: TradeSignal) -> str:
    if signal.clean_message:
        return signal.clean_message

    lines = [
        f"{signal.symbol} {signal.direction.value}",
        "",
        f"ENTRY {decimal_to_text(signal.entry_low)}-{decimal_to_text(signal.entry_high)}",
        "",
        f"SL {decimal_to_text(signal.stop_loss)}",
        "",
    ]
    lines.extend(f"TP {decimal_to_text(take_profit)}" for take_profit in signal.take_profits)
    return "\n".join(lines)


def strip_noise(value: str) -> str:
    return "".join(character for character in value.strip() if character.isascii())


def normalize_range_spacing(value: str) -> str:
    return re.sub(r"\s*[-–—]\s*", "-", value.strip())
