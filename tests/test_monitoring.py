from __future__ import annotations

import asyncio
from decimal import Decimal
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest

from telegram_mt5_copier.database import SignalDatabase, connect_database
from telegram_mt5_copier.listener import (
    SignalProcessor,
    log_incoming_message,
    resolve_source_chat,
    resolve_source_chats,
    telegram_client_options,
    telegram_event_is_stale,
)
from telegram_mt5_copier.models import DecisionStatus, Direction, IncomingMessage
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

GOLD_BUY_NOW = """Gold Buy Now:4120 - 4115

TAKE PROFIT:4125
TAKE PROFIT:4130

STOP LOSS:4110"""

GOLD_BUY_NOW_CLEAN = """XAUUSD BUY

ENTRY 4120-4115

SL 4110

TP 4125
TP 4130"""

GOLD_BUY_NOW_PARENTHESIZED = """GOLD BUY NOW (4035-4029)

TP1: 4045
TP2: 4055
TP3: 4065

STOP LOSS: 4025"""

SELL_XAUUSD_AT = """📍SELL📍 XAUUSD @4090 - 4095

✅TP1 : 4085
✅TP2 : 4080
✅TP3 : 4075

🔴SL : 4105"""


class FakePublisher:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def publish(self, signal, formatted_message: str, client=None) -> None:
        self.messages.append(formatted_message)


class SlowFakePublisher(FakePublisher):
    async def publish(self, signal, formatted_message: str, client=None) -> None:
        await asyncio.sleep(0.02)
        await super().publish(signal, formatted_message, client=client)


class FakeSourceEntity:
    id = 1234567890
    title = "Canal VIP"
    broadcast = True
    megagroup = False
    noforwards = True


class FakeTelegramClient:
    async def get_entity(self, chat_id: int):
        if chat_id != -1001234567890:
            raise ValueError("unknown channel")
        return FakeSourceEntity()

    async def get_messages(self, _entity, *, limit: int):
        self.requested_limit = limit
        return [type("Message", (), {"id": 987})()]


class FakeMultipleSourceClient:
    def __init__(self) -> None:
        self.requested_ids: list[int] = []

    async def get_entity(self, chat_id: int):
        self.requested_ids.append(chat_id)
        entity = FakeSourceEntity()
        entity.id = abs(chat_id)
        entity.title = f"Canal {abs(chat_id)}"
        return entity

    async def get_messages(self, _entity, *, limit: int):
        return [type("Message", (), {"id": limit})()]


class CaptureLogger:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def info(self, message: str, *args: object, **_kwargs: object) -> None:
        self.messages.append(message % args if args else message)

    def error(self, message: str, *args: object, **_kwargs: object) -> None:
        self.messages.append(message % args if args else message)


class SourceChannelValidationTests(unittest.IsolatedAsyncioTestCase):
    def test_cliente_telegram_reconecta_e_recupera_atualizacoes(self) -> None:
        options = telegram_client_options()

        self.assertTrue(options["auto_reconnect"])
        self.assertIsNone(options["connection_retries"])
        self.assertTrue(options["catch_up"])
        self.assertTrue(options["sequential_updates"])

    def test_recuperacao_rejeita_sinal_antigo(self) -> None:
        now = datetime(2026, 7, 23, 12, 0, tzinfo=timezone.utc)
        old_event = type(
            "Event",
            (),
            {"message": type("Message", (), {"date": now - timedelta(minutes=6)})()},
        )()
        recent_event = type(
            "Event",
            (),
            {"message": type("Message", (), {"date": now - timedelta(minutes=2)})()},
        )()

        self.assertTrue(telegram_event_is_stale(old_event, edited=False, now=now))
        self.assertFalse(telegram_event_is_stale(recent_event, edited=False, now=now))

    async def test_resolve_canal_protegido_e_confirma_historico(self) -> None:
        client = FakeTelegramClient()
        logger = CaptureLogger()

        entity = await resolve_source_chat(client, "-1001234567890", logger)

        self.assertIsInstance(entity, FakeSourceEntity)
        self.assertEqual(client.requested_limit, 1)
        self.assertIn("nome=Canal VIP", logger.messages[0])
        self.assertIn("conteudo_protegido=sim", logger.messages[0])
        self.assertIn("ultima_mensagem_id=987", logger.messages[0])

    async def test_source_chat_id_precisa_ser_numerico(self) -> None:
        with self.assertRaisesRegex(ValueError, "ID numérico"):
            await resolve_source_chat(FakeTelegramClient(), "canal-vip", CaptureLogger())

    async def test_resolve_todos_os_canais_configurados(self) -> None:
        client = FakeMultipleSourceClient()
        logger = CaptureLogger()

        entities = await resolve_source_chats(
            client,
            ("-1001111111111", "-1002222222222"),
            logger,
        )

        self.assertEqual(len(entities), 2)
        self.assertEqual(client.requested_ids, [-1001111111111, -1002222222222])
        self.assertIn("total=2", logger.messages[-1])

    async def test_lista_de_canais_vazia_e_rejeitada(self) -> None:
        with self.assertRaisesRegex(ValueError, "SOURCE_CHAT_IDS"):
            await resolve_source_chats(FakeMultipleSourceClient(), (), CaptureLogger())

    def test_log_de_mensagem_nao_expoe_conteudo(self) -> None:
        logger = CaptureLogger()
        incoming = IncomingMessage(
            source_chat_id=-1001234567890,
            source_message_id=55,
            text="XAUUSD BUY segredo",
        )

        log_incoming_message(logger, incoming, edited=False)

        self.assertIn("mensagem_id=55", logger.messages[0])
        self.assertIn("texto_presente=sim", logger.messages[0])
        self.assertNotIn("segredo", logger.messages[0])


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

    async def test_duplicidade_simultanea_e_reservada_antes_da_publicacao(self) -> None:
        publisher = SlowFakePublisher()
        first_processor = SignalProcessor(self.database, publisher, logger=NullLogger())
        second_processor = SignalProcessor(self.database, publisher, logger=NullLogger())

        first, second = await asyncio.gather(
            first_processor.process(
                IncomingMessage(source_chat_id="source", source_message_id=101, text=BUY_VALID)
            ),
            second_processor.process(
                IncomingMessage(source_chat_id="source", source_message_id=102, text=BUY_VALID)
            ),
        )

        self.assertEqual(
            sorted((first.status, second.status)),
            [DecisionStatus.ACCEPTED, DecisionStatus.IGNORED],
        )
        self.assertEqual(len(publisher.messages), 1)

    async def test_mesmo_sinal_de_duas_salas_publica_uma_vez_no_destino(self) -> None:
        publisher = SlowFakePublisher()
        first_processor = SignalProcessor(
            self.database,
            publisher,
            logger=NullLogger(),
            publication_scope="-100destino",
        )
        second_processor = SignalProcessor(
            self.database,
            publisher,
            logger=NullLogger(),
            publication_scope="-100destino",
        )

        first, second = await asyncio.gather(
            first_processor.process(
                IncomingMessage(source_chat_id="sala-1", source_message_id=1, text=BUY_VALID)
            ),
            second_processor.process(
                IncomingMessage(source_chat_id="sala-2", source_message_id=2, text=BUY_VALID)
            ),
        )

        self.assertEqual(first.status, DecisionStatus.ACCEPTED)
        self.assertEqual(second.status, DecisionStatus.ACCEPTED)
        self.assertEqual(len(publisher.messages), 1)
        self.assertEqual(count_signals(self.database.database_path), 2)

    async def test_par_forex_e_aceito(self) -> None:
        decision = await self.processor.process(
            IncomingMessage(
                source_chat_id="source",
                source_message_id=1,
                text="EURUSD BUY\nENTRY 1.1500-1.1510\nSL 1.1450\nTP 1.1600",
            )
        )

        self.assertEqual(decision.status, DecisionStatus.ACCEPTED)
        self.assertEqual(decision.signal.symbol, "EURUSD")

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
    def test_cadchf_sell_com_entry_arroba(self) -> None:
        decision = parse_signal_text(
            """🔵🔵11 AUGUST FREE signal forecast:
CADCHF

CADCHF SELL

ENTRY @ 0.58217
SL: 0.58304

TP1: 0.58136
TP2: 0.58041
TP3: 0.57956

Become a VIP member"""
        )

        self.assertEqual(decision.status, DecisionStatus.ACCEPTED)
        self.assertEqual(decision.signal.symbol, "CADCHF")
        self.assertEqual(decision.signal.direction, Direction.SELL)
        self.assertEqual(decision.signal.entry_low, Decimal("0.58217"))
        self.assertEqual(decision.signal.stop_loss, Decimal("0.58304"))
        self.assertEqual(
            decision.signal.take_profits,
            (Decimal("0.58136"), Decimal("0.58041"), Decimal("0.57956")),
        )
        self.assertEqual(validate_signal(decision.signal).status, DecisionStatus.ACCEPTED)
        self.assertTrue(decision.signal.clean_message.startswith("CADCHF SELL"))

    def test_relatorio_de_pips_nao_vira_sinal(self) -> None:
        decision = parse_signal_text(
            """Monday 03/08/2026 closed trades:
GBPUSD BUY TP +50 pips✅
EURUSD BUY TP +50 pips✅
EURJPY SELL SL -60 pips"""
        )

        self.assertEqual(decision.status, DecisionStatus.IGNORED)
        self.assertEqual(decision.reason, "performance_report")

    def test_relatorio_fechado_com_entry_continua_ignorado(self) -> None:
        decision = parse_signal_text(
            "CLOSED TRADES\nEURUSD BUY\nENTRY 1.10\nSL 1.09\nTP 1.11"
        )

        self.assertEqual(decision.status, DecisionStatus.IGNORED)
        self.assertEqual(decision.reason, "performance_report")

    def test_cripto_continua_bloqueada(self) -> None:
        decision = parse_signal_text(
            "BTCUSD BUY\nENTRY 60000\nSL 59000\nTP 62000"
        )

        self.assertEqual(decision.status, DecisionStatus.REJECTED)
        self.assertEqual(decision.reason, "unsupported_asset")

    def test_scalping_em_portugues_com_xau_hifen(self) -> None:
        decision = parse_signal_text(
            """📊 VAMOS FAZER UM SCALPING

💵 Moeda: XAU-USD
✋ Análise: Venda ( Sell ) 4075
🎯 Entrada: 4075
⛔ Stop Loss (SL): 4090

Take Profit (TP):
✅ TP 4070
✅ TP 4065"""
        )

        self.assertEqual(decision.status, DecisionStatus.ACCEPTED)
        self.assertEqual(decision.signal.symbol, "XAUUSD")
        self.assertEqual(decision.signal.direction, Direction.SELL)
        self.assertEqual(decision.signal.entry_low, Decimal("4075"))
        self.assertEqual(decision.signal.entry_high, Decimal("4075"))
        self.assertEqual(decision.signal.stop_loss, Decimal("4090"))
        self.assertEqual(
            decision.signal.take_profits,
            (Decimal("4070"), Decimal("4065")),
        )
        self.assertEqual(validate_signal(decision.signal).status, DecisionStatus.ACCEPTED)

    def test_gold_buy_now_com_entrada_no_cabecalho(self) -> None:
        decision = parse_signal_text(GOLD_BUY_NOW)

        self.assertEqual(decision.status, DecisionStatus.ACCEPTED)
        self.assertEqual(decision.signal.symbol, "XAUUSD")
        self.assertEqual(decision.signal.direction, Direction.BUY)
        self.assertEqual(decision.signal.entry_low, Decimal("4115"))
        self.assertEqual(decision.signal.entry_high, Decimal("4120"))
        self.assertEqual(decision.signal.stop_loss, Decimal("4110"))
        self.assertEqual(
            decision.signal.take_profits,
            (Decimal("4125"), Decimal("4130")),
        )
        self.assertEqual(decision.signal.clean_message, GOLD_BUY_NOW_CLEAN)

    def test_gold_buy_now_com_entrada_entre_parenteses(self) -> None:
        decision = parse_signal_text(GOLD_BUY_NOW_PARENTHESIZED)

        self.assertEqual(decision.status, DecisionStatus.ACCEPTED)
        self.assertEqual(decision.signal.direction, Direction.BUY)
        self.assertEqual(decision.signal.entry_low, Decimal("4029"))
        self.assertEqual(decision.signal.entry_high, Decimal("4035"))
        self.assertEqual(decision.signal.stop_loss, Decimal("4025"))
        self.assertEqual(
            decision.signal.take_profits,
            (Decimal("4045"), Decimal("4055"), Decimal("4065")),
        )
        self.assertEqual(validate_signal(decision.signal).status, DecisionStatus.ACCEPTED)

    def test_gold_buy_now_in_zone_com_tp_open(self) -> None:
        decision = parse_signal_text(
            """GOLD BUY NOW IN ZONE 4027.00-4020.00
(SCALPING)

TP 1 : 4029.00
TP 2 : 4031.00
TP 3 : OPEN

SL : 4017.00"""
        )

        self.assertEqual(decision.status, DecisionStatus.ACCEPTED)
        self.assertEqual(decision.signal.direction, Direction.BUY)
        self.assertEqual(decision.signal.entry_low, Decimal("4020.00"))
        self.assertEqual(decision.signal.entry_high, Decimal("4027.00"))
        self.assertEqual(decision.signal.stop_loss, Decimal("4017.00"))
        self.assertEqual(
            decision.signal.take_profits,
            (Decimal("4029.00"), Decimal("4031.00")),
        )
        self.assertNotIn("OPEN", decision.signal.clean_message)
        self.assertEqual(validate_signal(decision.signal).status, DecisionStatus.ACCEPTED)

    def test_sell_antes_de_xauusd_com_entrada_arroba(self) -> None:
        decision = parse_signal_text(SELL_XAUUSD_AT)

        self.assertEqual(decision.status, DecisionStatus.ACCEPTED)
        self.assertEqual(decision.signal.direction, Direction.SELL)
        self.assertEqual(decision.signal.entry_low, Decimal("4090"))
        self.assertEqual(decision.signal.entry_high, Decimal("4095"))
        self.assertEqual(decision.signal.stop_loss, Decimal("4105"))
        self.assertEqual(
            decision.signal.take_profits,
            (Decimal("4085"), Decimal("4080"), Decimal("4075")),
        )
        self.assertEqual(validate_signal(decision.signal).status, DecisionStatus.ACCEPTED)

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


def count_signals(database_path: Path) -> int:
    with connect_database(database_path) as connection:
        return int(connection.execute("SELECT COUNT(*) FROM signals").fetchone()[0])


if __name__ == "__main__":
    unittest.main()
