from __future__ import annotations

from decimal import Decimal
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

from .models import DecisionStatus, TradeSignal
from .parser import all_decimals, parse_signal_text
from .validator import validate_signal


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

