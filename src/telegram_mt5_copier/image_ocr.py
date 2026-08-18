from __future__ import annotations

from decimal import Decimal
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

from .models import DecisionStatus, TradeSignal
from .parser import DIRECTION_RE, all_decimals, parse_signal_text
from .signal_formatter import SUPPORTED_ASSET_PATTERN
from .validator import validate_signal

ASSET_TOKEN_GAP_RE = re.compile(rf"(\b{SUPPORTED_ASSET_PATTERN}\b)([^\w]{{1,12}})", re.IGNORECASE)


COMMON_TESSERACT_PATHS = (
    Path(r"C:\Program Files\Tesseract-OCR\tesseract.exe"),
    Path(r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe"),
)


def find_tesseract_command(configured_command: str | None = None) -> str | None:
    if configured_command:
        configured_path = Path(configured_command).expanduser()
        if configured_path.is_file():
            return str(configured_path)
        resolved = shutil.which(configured_command)
        if resolved:
            return resolved
        return None

    resolved = shutil.which("tesseract")
    if resolved:
        return resolved
    for candidate in COMMON_TESSERACT_PATHS:
        if candidate.is_file():
            return str(candidate)
    return None


def extract_image_text(
    image_bytes: bytes,
    *,
    tesseract_command: str,
    timeout_seconds: int = 20,
) -> str:
    if not image_bytes:
        return ""

    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as temporary:
            temporary.write(image_bytes)
            temporary_path = Path(temporary.name)

        completed = subprocess.run(
            [
                tesseract_command,
                str(temporary_path),
                "stdout",
                "--psm",
                "6",
                "-l",
                "eng",
            ],
            capture_output=True,
            check=False,
            timeout=timeout_seconds,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if completed.returncode != 0:
            return ""
        return completed.stdout.decode("utf-8", errors="replace").strip()
    except (OSError, subprocess.SubprocessError):
        return ""
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def validated_ocr_signal(ocr_text: str, telegram_caption: str | None) -> TradeSignal | None:
    """Aceita OCR somente quando estrutura e números críticos têm confirmação dupla."""
    parsed = parse_signal_text(ocr_text)
    if parsed.status != DecisionStatus.ACCEPTED or parsed.signal is None:
        return None
    validated = validate_signal(parsed.signal)
    if validated.status != DecisionStatus.ACCEPTED or validated.signal is None:
        return None
    if not caption_corroborates_signal(telegram_caption, validated.signal):
        return None
    return validated.signal


def caption_corroborates_signal(caption: str | None, signal: TradeSignal) -> bool:
    """Evita operar apenas por OCR: entrada, SL e pelo menos dois TPs devem coincidir."""
    if not caption or not caption.strip():
        return False
    caption_values = set(all_decimals(normalize_caption_numbers(caption)))
    required_prices = {signal.entry_low, signal.entry_high, signal.stop_loss}
    if not required_prices.issubset(caption_values):
        return False
    matching_targets = sum(target in caption_values for target in signal.take_profits)
    return matching_targets >= min(2, len(signal.take_profits))


def normalize_caption_numbers(value: str) -> str:
    return re.sub(r"\s*[_–—]\s*", "-", value)


def recover_direction_from_ocr(
    ocr_text: str,
    telegram_caption: str | None,
) -> tuple[TradeSignal, str] | None:
    """Recupera somente a direção (BUY/SELL/COMPRA/VENDA) via OCR quando um
    emoji customizado a substitui na legenda original, algo comum em salas
    que estilizam a palavra como selo colorido em vez de texto simples.

    Ao contrário de `validated_ocr_signal`, que confia no OCR para o sinal
    inteiro, aqui entrada, Stop Loss e Take Profits continuam vindo
    exclusivamente da legenda original, passando pelas mesmas validações de
    sempre. O OCR contribui apenas a palavra que o emoji apagou. Se a legenda
    já tiver uma direção reconhecível, nada é alterado — isto nunca sobrepõe
    uma direção já presente.
    """
    if not telegram_caption or not telegram_caption.strip():
        return None
    if DIRECTION_RE.search(telegram_caption):
        return None
    direction_match = DIRECTION_RE.search(ocr_text)
    if direction_match is None:
        return None
    direction_word = direction_match.group(1)

    spliced_caption = _splice_direction_after_asset(telegram_caption, direction_word)
    if spliced_caption is None:
        spliced_caption = f"{direction_word} {telegram_caption}"

    parsed = parse_signal_text(spliced_caption)
    if parsed.status != DecisionStatus.ACCEPTED or parsed.signal is None:
        return None
    validated = validate_signal(parsed.signal)
    if validated.status != DecisionStatus.ACCEPTED or validated.signal is None:
        return None
    return validated.signal, spliced_caption


def _splice_direction_after_asset(caption: str, direction_word: str) -> str | None:
    """Substitui o trecho entre o ativo e a próxima palavra pela direção.

    Cobre o formato mais comum ("XAUUSD [emoji] NOW 4395_4399"), em que o
    emoji ocupa exatamente o lugar onde a legenda diria SELL/BUY entre o
    ativo e o restante da linha — a mesma folga que `HEADER_RE` já tolera
    nesse formato. Sem isso, só colar a palavra no início da legenda deixa o
    emoji original no meio do texto, quebrando a extração da zona de entrada.
    """
    match = ASSET_TOKEN_GAP_RE.search(caption)
    if match is None:
        return None
    return f"{caption[:match.start(2)]} {direction_word} {caption[match.end(2):]}"

