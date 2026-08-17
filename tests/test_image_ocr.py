from decimal import Decimal

from telegram_mt5_copier.image_ocr import validated_ocr_signal


OCR_TEXT = """GOLD SELL
XAUUSD SELL NOW 4398_4402
TP1 4394
TP2 4390
TP3 4386
TP4 4380
TP5 4375
SL 4411"""

CORRUPTED_TELEGRAM_CAPTION = """XAUUSD ��🔤🔤 OW🔽🔽4398_4402
TP¹ 4394
T² 4390
TP³ 438
TP⁴ 480
TP⁵ 4375
SL 4411"""


def test_ocr_signal_is_accepted_when_caption_confirms_critical_prices() -> None:
    signal = validated_ocr_signal(OCR_TEXT, CORRUPTED_TELEGRAM_CAPTION)

    assert signal is not None
    assert signal.direction.value == "SELL"
    assert signal.entry_low == Decimal("4398")
    assert signal.entry_high == Decimal("4402")
    assert signal.stop_loss == Decimal("4411")
    assert signal.take_profits == (
        Decimal("4394"),
        Decimal("4390"),
        Decimal("4386"),
        Decimal("4380"),
        Decimal("4375"),
    )


def test_ocr_signal_is_rejected_without_caption_corroboration() -> None:
    assert validated_ocr_signal(OCR_TEXT, None) is None
    assert validated_ocr_signal(OCR_TEXT, "XAUUSD signal") is None


def test_ocr_signal_is_rejected_when_critical_price_differs() -> None:
    caption = CORRUPTED_TELEGRAM_CAPTION.replace("4411", "4412")

    assert validated_ocr_signal(OCR_TEXT, caption) is None

