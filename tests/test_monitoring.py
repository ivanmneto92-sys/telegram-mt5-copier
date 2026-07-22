from __future__ import annotations

from decimal import Decimal
from pathlib import Path
import tempfile
import unittest

from telegram_mt5_copier.database import SignalDatabase
from telegram_mt5_copier.listener import SignalProcessor
from telegram_mt5_copier.models import DecisionStatus, IncomingMessage
from telegram_mt5_copier.parser import parse_signal_text
from telegram_mt5_copier.signal_formatter import clean_signal_text
from telegram_mt5_copier.validator import validate_signal


BUY_VALID = """XAUUSD BUY

ENTRY 4105-03

SL 4090
TP 4110
TP 4115
TP 4120
TP 4140
"""

SELL_VALID = """XAUUSD SELL
ENTRY 4103-4105
SL 4110
TP 4095
TP 4090
TP 4080
"""

PROMO_SIGNAL = """VIP SIGNAL

XAUUSD BUY

ENTRY 4061-59
SL 4044
TP 4066
TP 4071
TP 4076
TP 4096

Deposit $300 get FREE VIP:
Https://puvip.co/VvQz2e
"""

PROMO_SIGNAL_CLEAN = """XAUUSD BUY

ENTRY 4061-59

SL 4044

TP 4066
TP 4071
TP 4076
TP 4096"""


class FakePublisher:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def publish(self, signal, formatted_message: str, client=None) -> None:
        self.messages.append(formatted_message)


class MonitorPipelineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = SignalDatabase(Path(self.temp_dir.name) / "signals.sqlite3")
        self.database.initialize()
        self.publisher = FakePublisher()
        self.processor = SignalProcessor(self.database, self.publisher, logger=NullLogger())

    async def asyncTearDown(self) -> None:
        if hasattr(self.processor, "close"):
            self.processor.close()
        if hasattr(self.database, "close"):
            self.database.close()
        self.temp_dir.cleanup()

    async def process_text(self, text: str) -> DecisionStatus:
        decision = await self.processor.process(
            IncomingMessage(source_chat_id="source", source_message_id=1, text=text)
        )
        return decision.status

    async def test_buy_valido(self) -> None:
        decision = await self.processor.process(
            IncomingMessage(source_chat_id="source", source_message_id=1, text=BUY_VALID)
        )

        self.assertEqual(decision.status, DecisionStatus.ACCEPTED)
        self.assertEqual(decision.signal.direction.value, "BUY")
        self.assertEqual(decision.signal.entry_low, Decimal("4103"))
        self.assertEqual(decision.signal.entry_high, Decimal("4105"))
        self.assertEqual(decision.signal.stop_loss, Decimal("4090"))
        self.assertEqual(len(decision.signal.take_profits), 4)
        self.assertEqual(len(self.publisher.messages), 1)

    async def test_sell_valido(self) -> None:
        decision = await self.processor.process(
            IncomingMessage(source_chat_id="source", source_message_id=1, text=SELL_VALID)
        )

        self.assertEqual(decision.status, DecisionStatus.ACCEPTED)
        self.assertEqual(decision.signal.direction.value, "SELL")
        self.assertEqual(decision.signal.entry_low, Decimal("4103"))
        self.assertEqual(decision.signal.entry_high, Decimal("4105"))
        self.assertEqual(decision.signal.stop_loss, Decimal("4110"))
        self.assertEqual(decision.signal.take_profits, (Decimal("4095"), Decimal("4090"), Decimal("4080")))

    async def test_mensagem_comum_em_ingles(self) -> None:
        decision = await self.processor.process(
            IncomingMessage(
                source_chat_id="source",
                source_message_id=1,
                text="Good morning traders, stay patient and manage your risk today.",
            )
        )

        self.assertEqual(decision.status, DecisionStatus.IGNORED)
        self.assertEqual(decision.reason, "common_message")

    async def test_imagem_sem_legenda(self) -> None:
        decision = await self.processor.process(
            IncomingMessage(source_chat_id="source", source_message_id=1, media_type="photo")
        )

        self.assertEqual(decision.status, DecisionStatus.IGNORED)
        self.assertEqual(decision.reason, "image_without_caption")

    async def test_imagem_com_legenda_valida(self) -> None:
        decision = await self.processor.process(
            IncomingMessage(
                source_chat_id="source",
                source_message_id=1,
                caption=BUY_VALID,
                media_type="photo",
            )
        )

        self.assertEqual(decision.status, DecisionStatus.ACCEPTED)
        self.assertEqual(len(self.publisher.messages), 1)

    async def test_sinal_sem_sl(self) -> None:
        decision = await self.processor.process(
            IncomingMessage(
                source_chat_id="source",
                source_message_id=1,
                text="XAUUSD BUY\nENTRY 4103-4105\nTP 4110",
            )
        )

        self.assertEqual(decision.status, DecisionStatus.REJECTED)
        self.assertEqual(decision.reason, "missing_stop_loss")

    async def test_sinal_sem_tp(self) -> None:
        decision = await self.processor.process(
            IncomingMessage(
                source_chat_id="source",
                source_message_id=1,
                text="XAUUSD BUY\nENTRY 4103-4105\nSL 4090",
            )
        )

        self.assertEqual(decision.status, DecisionStatus.REJECTED)
        self.assertEqual(decision.reason, "missing_take_profit")

    async def test_buy_incoerente(self) -> None:
        decision = await self.processor.process(
            IncomingMessage(
                source_chat_id="source",
                source_message_id=1,
                text="XAUUSD BUY\nENTRY 4103-4105\nSL 4106\nTP 4110",
            )
        )

        self.assertEqual(decision.status, DecisionStatus.REJECTED)
        self.assertEqual(decision.reason, "buy_stop_loss_not_below_entry")

    async def test_sell_incoerente(self) -> None:
        decision = await self.processor.process(
            IncomingMessage(
                source_chat_id="source",
                source_message_id=1,
                text="XAUUSD SELL\nENTRY 4103-4105\nSL 4100\nTP 4090",
            )
        )

        self.assertEqual(decision.status, DecisionStatus.REJECTED)
        self.assertEqual(decision.reason, "sell_stop_loss_not_above_entry")

    async def test_duplicidade(self) -> None:
        first = await self.processor.process(
            IncomingMessage(source_chat_id="source", source_message_id=1, text=BUY_VALID)
        )
        second = await self.processor.process(
            IncomingMessage(source_chat_id="source", source_message_id=2, text=BUY_VALID)
        )

        self.assertEqual(first.status, DecisionStatus.ACCEPTED)
        self.assertEqual(second.status, DecisionStatus.IGNORED)
        self.assertEqual(second.reason, "duplicate_signal")
        self.assertEqual(len(self.publisher.messages), 1)

    async def test_ativo_diferente_de_xauusd(self) -> None:
        decision = await self.processor.process(
            IncomingMessage(
                source_chat_id="source",
                source_message_id=1,
                text="GOLD BUY\nENTRY 4103-4105\nSL 4090\nTP 4110",
            )
        )

        self.assertEqual(decision.status, DecisionStatus.REJECTED)
        self.assertEqual(decision.reason, "unsupported_asset")

    async def test_sinal_com_propaganda_removida(self) -> None:
        decision = await self.processor.process(
            IncomingMessage(source_chat_id="source", source_message_id=1, text=PROMO_SIGNAL)
        )

        self.assertEqual(decision.status, DecisionStatus.ACCEPTED)
        self.assertEqual(decision.formatted_message, PROMO_SIGNAL_CLEAN)
        self.assertNotIn("Deposit", decision.formatted_message)

    async def test_sinal_com_url_removida(self) -> None:
        decision = await self.processor.process(
            IncomingMessage(source_chat_id="source", source_message_id=1, text=PROMO_SIGNAL)
        )

        self.assertEqual(decision.status, DecisionStatus.ACCEPTED)
        self.assertNotIn("http", decision.formatted_message.lower())
        self.assertNotIn("puvip", decision.formatted_message.lower())


class ParserTests(unittest.TestCase):
    def test_faixa_abreviada_4105_03(self) -> None:
        decision = parse_signal_text(BUY_VALID)

        self.assertEqual(decision.status, DecisionStatus.ACCEPTED)
        self.assertEqual(decision.signal.entry_low, Decimal("4103"))
        self.assertEqual(decision.signal.entry_high, Decimal("4105"))

    def test_faixa_normal_4103_4105(self) -> None:
        decision = parse_signal_text("XAUUSD BUY\nENTRY 4103-4105\nSL 4090\nTP 4110")

        self.assertEqual(decision.status, DecisionStatus.ACCEPTED)
        self.assertEqual(decision.signal.entry_low, Decimal("4103"))
        self.assertEqual(decision.signal.entry_high, Decimal("4105"))

    def test_multiplos_tps(self) -> None:
        decision = parse_signal_text(BUY_VALID)

        self.assertEqual(
            decision.signal.take_profits,
            (Decimal("4110"), Decimal("4115"), Decimal("4120"), Decimal("4140")),
        )

    def test_multiplos_tps_na_mesma_linha(self) -> None:
        decision = parse_signal_text(
            "XAUUSD BUY\nENTRY 4103-4105\nSL 4090\nTP1 4110 TP2 4115 TP3 4120"
        )

        self.assertEqual(
            decision.signal.take_profits,
            (Decimal("4110"), Decimal("4115"), Decimal("4120")),
        )

    def test_mesmos_precos_em_mensagens_diferentes_nao_sao_duplicados(self) -> None:
        first = parse_signal_text(
            "XAUUSD BUY\nENTRY 4103-4105\nSL 4090\nTP 4110",
            source_chat_id=-1001,
            source_message_id=10,
        )
        second = parse_signal_text(
            "XAUUSD BUY\nENTRY 4103-4105\nSL 4090\nTP 4110",
            source_chat_id=-1001,
            source_message_id=11,
        )

        self.assertNotEqual(first.signal.signature, second.signal.signature)

    def test_validacao_buy_valido(self) -> None:
        parsed = parse_signal_text(BUY_VALID)
        validated = validate_signal(parsed.signal)

        self.assertEqual(validated.status, DecisionStatus.ACCEPTED)

    def test_saida_somente_com_bloco_limpo(self) -> None:
        self.assertEqual(clean_signal_text(PROMO_SIGNAL), PROMO_SIGNAL_CLEAN)


class NullLogger:
    def info(self, *_args, **_kwargs) -> None:
        return None

    def error(self, *_args, **_kwargs) -> None:
        return None


if __name__ == "__main__":
    unittest.main()
