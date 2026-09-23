from __future__ import annotations

import asyncio
from decimal import Decimal
from pathlib import Path
import tempfile
import unittest

from telegram_mt5_copier.central_sync import (
    CentralSyncOutbox,
    build_outbox_payload,
    resolve_local_channel,
    run_central_sync_drain_loop,
)
from telegram_mt5_copier.channel_catalog import ChannelCatalogService
from telegram_mt5_copier.database import SignalDatabase, connect_database
from telegram_mt5_copier.listener import SignalProcessor
from telegram_mt5_copier.models import DecisionStatus, Direction, IncomingMessage, TradeSignal


class NullLogger:
    def info(self, *args, **kwargs) -> None:
        pass

    def warning(self, *args, **kwargs) -> None:
        pass

    def error(self, *args, **kwargs) -> None:
        pass

    def exception(self, *args, **kwargs) -> None:
        pass


class FakePublisher:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def publish(self, signal, formatted_message: str, client=None) -> None:
        self.messages.append(formatted_message)


class RaisingOutbox:
    def enqueue_signal_shadow_write(self, signal, formatted_message: str) -> None:
        raise RuntimeError("supabase indisponivel (simulado)")


BUY_VALID = """XAUUSD BUY

ENTRY 4105-03

SL 4090
TP 4110
TP 4115
"""


def make_signal(source_chat_id: str = "123456") -> TradeSignal:
    return TradeSignal(
        symbol="XAUUSD",
        direction=Direction.BUY,
        entry_low=Decimal("4103"),
        entry_high=Decimal("4105"),
        stop_loss=Decimal("4090"),
        take_profits=(Decimal("4110"), Decimal("4115")),
        raw_text=BUY_VALID,
        source_chat_id=source_chat_id,
        source_message_id=1,
    )


class ResolveLocalChannelTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp_dir.name) / "signals.sqlite3"
        SignalDatabase(self.database_path).initialize()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_canal_nao_registrado_retorna_none(self) -> None:
        self.assertIsNone(resolve_local_channel(self.database_path, "999"))

    def test_canal_registrado_e_encontrado(self) -> None:
        catalog = ChannelCatalogService(self.database_path)
        catalog.register_configured_channel(
            telegram_chat_id="123456",
            title="Canal VIP",
            username=None,
            content_protected=False,
            history_accessible=True,
            last_message_id=None,
        )

        channel = resolve_local_channel(self.database_path, "123456")

        self.assertIsNotNone(channel)
        self.assertEqual(channel.telegram_chat_id, "123456")
        self.assertEqual(channel.title, "Canal VIP")
        self.assertEqual(channel.status, "active")


class BuildOutboxPayloadTests(unittest.TestCase):
    def test_payload_contem_campos_esperados(self) -> None:
        from telegram_mt5_copier.central_sync import ChannelRow

        channel = ChannelRow(id=7, telegram_chat_id="123456", title="Canal VIP", status="active", access_status="confirmed")
        signal = make_signal()

        payload = build_outbox_payload(signal, "mensagem formatada", channel)

        self.assertEqual(payload["source_channel_id"], 7)
        self.assertEqual(payload["telegram_chat_id"], "123456")
        self.assertEqual(payload["symbol"], "XAUUSD")
        self.assertEqual(payload["direction"], "BUY")
        self.assertEqual(payload["entry_low"], "4103")
        self.assertEqual(payload["take_profits"], ["4110", "4115"])
        self.assertEqual(payload["source_message_id"], "1")
        self.assertEqual(payload["formatted_message"], "mensagem formatada")
        self.assertIn("received_at", payload)


class CentralSyncOutboxTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp_dir.name) / "signals.sqlite3"
        SignalDatabase(self.database_path).initialize()
        self.catalog = ChannelCatalogService(self.database_path)
        self.catalog.register_configured_channel(
            telegram_chat_id="123456",
            title="Canal VIP",
            username=None,
            content_protected=False,
            history_accessible=True,
            last_message_id=None,
        )
        self.outbox = CentralSyncOutbox(self.database_path)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _row_count(self) -> int:
        with connect_database(self.database_path) as connection:
            cursor = connection.execute("SELECT COUNT(*) FROM central_sync_outbox")
            try:
                return cursor.fetchone()[0]
            finally:
                cursor.close()

    def test_enqueue_grava_linha_para_canal_registrado(self) -> None:
        self.outbox.enqueue_signal_shadow_write(make_signal(), "mensagem formatada")

        self.assertEqual(self._row_count(), 1)
        rows = self.outbox.claim_batch(10)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].kind, "signal_shadow_write")
        self.assertEqual(rows[0].payload["symbol"], "XAUUSD")

    def test_enqueue_nao_grava_nada_para_canal_nao_registrado(self) -> None:
        self.outbox.enqueue_signal_shadow_write(make_signal(source_chat_id="999999"), "mensagem")

        self.assertEqual(self._row_count(), 0)

    def test_claim_mark_done_remove_da_proxima_leva(self) -> None:
        self.outbox.enqueue_signal_shadow_write(make_signal(), "mensagem")
        rows = self.outbox.claim_batch(10)
        self.outbox.mark_done(rows[0].id)

        self.assertEqual(self.outbox.claim_batch(10), [])

    def test_mark_failed_adia_next_attempt_at_e_nao_aparece_na_proxima_leva_imediata(self) -> None:
        self.outbox.enqueue_signal_shadow_write(make_signal(), "mensagem")
        rows = self.outbox.claim_batch(10)
        self.outbox.mark_failed(rows[0].id, "erro de conexao")

        # Falhou agora, next_attempt_at fica no futuro -> nao reaparece imediatamente.
        self.assertEqual(self.outbox.claim_batch(10), [])
        with connect_database(self.database_path) as connection:
            cursor = connection.execute(
                "SELECT status, attempts, last_error FROM central_sync_outbox WHERE id = ?",
                (rows[0].id,),
            )
            try:
                status, attempts, last_error = cursor.fetchone()
            finally:
                cursor.close()
        self.assertEqual(status, "failed")
        self.assertEqual(attempts, 1)
        self.assertEqual(last_error, "erro de conexao")


class SignalProcessorShadowWriteIsolationTests(unittest.IsolatedAsyncioTestCase):
    """Prova que o shadow-write nunca afeta o caminho real de processamento."""

    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = SignalDatabase(Path(self.temp_dir.name) / "signals.sqlite3")
        self.database.initialize()
        self.publisher = FakePublisher()

    async def asyncTearDown(self) -> None:
        self.database.close()
        self.temp_dir.cleanup()

    async def test_central_sync_outbox_none_e_um_no_op(self) -> None:
        processor = SignalProcessor(self.database, self.publisher, logger=NullLogger(), central_sync_outbox=None)

        decision = await processor.process(
            IncomingMessage(source_chat_id="source", source_message_id=1, text=BUY_VALID)
        )

        self.assertEqual(decision.status, DecisionStatus.ACCEPTED)
        self.assertEqual(len(self.publisher.messages), 1)

    async def test_falha_no_outbox_nao_impede_sinal_de_ser_aceito(self) -> None:
        processor = SignalProcessor(
            self.database, self.publisher, logger=NullLogger(), central_sync_outbox=RaisingOutbox()
        )

        decision = await processor.process(
            IncomingMessage(source_chat_id="source", source_message_id=1, text=BUY_VALID)
        )

        self.assertEqual(decision.status, DecisionStatus.ACCEPTED)
        self.assertEqual(len(self.publisher.messages), 1)


class FakeConfig:
    def __init__(self) -> None:
        self.node_id = "dev-local"
        self.node_label = "dev-local"
        self.instance_id = "test_instance"
        self.brand_name = "Test Brand"
        self.central_sync_max_batch = 20
        self.central_sync_poll_seconds = 0.01


class FlakyThenHealthyClient:
    """Simula uma conexao que falha algumas vezes e depois se recupera --
    prova que o loop de drenagem nunca trava/crasha numa indisponibilidade
    transitoria e se autocura sem reiniciar o processo."""

    def __init__(self, fail_times: int) -> None:
        self._fail_remaining = fail_times
        self._connected = False
        self.connect_attempts = 0
        self.upsert_calls = 0

    @property
    def connected(self) -> bool:
        return self._connected

    async def connect(self) -> None:
        self.connect_attempts += 1
        if self._fail_remaining > 0:
            self._fail_remaining -= 1
            raise ConnectionError("supabase indisponivel (simulado)")
        self._connected = True

    async def reset(self) -> None:
        self._connected = False

    async def upsert_node(self, node_id: str, node_label: str) -> None:
        self.upsert_calls += 1

    async def upsert_instance(self, instance_id: str, brand_name: str, *, node_id: str) -> None:
        self.upsert_calls += 1


class DrainLoopResilienceTests(unittest.IsolatedAsyncioTestCase):
    async def test_loop_sobrevive_a_falhas_de_conexao_e_se_autocura(self) -> None:
        temp_dir = tempfile.TemporaryDirectory()
        try:
            database_path = Path(temp_dir.name) / "signals.sqlite3"
            SignalDatabase(database_path).initialize()
            outbox = CentralSyncOutbox(database_path)
            client = FlakyThenHealthyClient(fail_times=2)
            config = FakeConfig()

            task = asyncio.create_task(run_central_sync_drain_loop(outbox, client, config, NullLogger()))
            try:
                await asyncio.wait_for(
                    self._wait_until(lambda: client.connected and client.upsert_calls >= 2),
                    timeout=2.0,
                )
            finally:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

            self.assertGreaterEqual(client.connect_attempts, 3)  # 2 falhas + 1 sucesso
            self.assertTrue(client.connected)
            self.assertGreaterEqual(client.upsert_calls, 2)  # node + instance, apos reconectar
        finally:
            temp_dir.cleanup()

    @staticmethod
    async def _wait_until(condition) -> None:
        while not condition():
            await asyncio.sleep(0.01)


if __name__ == "__main__":
    unittest.main()
