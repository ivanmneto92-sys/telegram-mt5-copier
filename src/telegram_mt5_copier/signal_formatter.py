from __future__ import annotations

import re

from .models import TradeSignal, decimal_to_text

HEADER_RE = re.compile(r"\bXAUUSD\s+(BUY|SELL)\b", re.IGNORECASE)
ENTRY_LINE_RE = re.compile(r"\bENTRY\b\s*[:\-]?\s*(?P<value>\d+(?:[\.,]\d+)?(?:\s*[-–—]\s*\d+(?:[\.,]\d+)?)?)", re.IGNORECASE)
SL_LINE_RE = re.compile(r"\bSL\b\s*[:\-]?\s*(?P<value>\d+(?:[\.,]\d+)?)", re.IGNORECASE)
TP_LINE_RE = re.compile(r"\bTP\d*\b\s*[:\-]?\s*(?P<value>\d+(?:[\.,]\d+)?)", re.IGNORECASE)


def clean_signal_text(text: str) -> str | None:
    header_match = HEADER_RE.search(text)
    if not header_match:
        return None

    direction = header_match.group(1).upper()
    body = text[header_match.end() :]
    lines = [strip_noise(line) for line in body.splitlines()]

    entry_value: str | None = None
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

        tp_match = TP_LINE_RE.search(line)
        if tp_match:
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
