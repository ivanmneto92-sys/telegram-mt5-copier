from decimal import Decimal

from telegram_mt5_copier.image_ocr import (
    recover_direction_from_ocr,
    validated_ocr_signal,
)


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


# Formato real do FOREX BULL TRADER: a imagem só traz um selo "GOLD SELL" (sem
# nenhum número), e a legenda tem todos os preços certos mas a palavra SELL
# vira um emoji customizado, sumindo do texto entregue pelo Telegram.
CAPTION_WITH_EMOJI_DIRECTION = """XAUUSD \U0001F7E3 NOW 4395_4399
TP¹ ⬇ 4390
TP² ⬇ 4386
TP³ ⬇ 4381
TP⁴ ⬇ 4376
TP⁵ ⬇ 4370
❌SL 4408"""


def test_direction_only_recovered_when_caption_has_no_bare_direction_word() -> None:
    assert recover_direction_from_ocr("GOLD SELL", CAPTION_WITH_EMOJI_DIRECTION.replace(
        "\U0001F7E3", "SELL"
    )) is None


def test_recovers_direction_from_ocr_when_emoji_hides_it_in_caption() -> None:
    result = recover_direction_from_ocr("GOLD SELL", CAPTION_WITH_EMOJI_DIRECTION)

    assert result is not None
    signal, spliced_caption = result
    assert signal.direction.value == "SELL"
    assert signal.symbol == "XAUUSD"
    assert signal.entry_low == Decimal("4395")
    assert signal.entry_high == Decimal("4399")
    assert signal.stop_loss == Decimal("4408")
    assert signal.take_profits == (
        Decimal("4390"),
        Decimal("4386"),
        Decimal("4381"),
        Decimal("4376"),
        Decimal("4370"),
    )
    assert "SELL" in spliced_caption
    assert "\U0001F7E3" not in spliced_caption


def test_direction_recovery_gives_up_without_a_readable_direction_word_in_ocr() -> None:
    assert recover_direction_from_ocr("GOLD", CAPTION_WITH_EMOJI_DIRECTION) is None


def test_direction_recovery_returns_none_without_a_caption() -> None:
    assert recover_direction_from_ocr("GOLD SELL", None) is None
    assert recover_direction_from_ocr("GOLD SELL", "   ") is None

