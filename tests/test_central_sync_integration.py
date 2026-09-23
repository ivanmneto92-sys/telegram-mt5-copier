"""Testes de integracao da Etapa 2 (shadow-write) contra Postgres local real
(supabase start). Pulados automaticamente se o Postgres local nao responder --
nunca tocam em Supabase remoto nem na VPS."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
import socket
import tempfile
import unittest

from telegram_mt5_copier.central_sync import CentralSyncClient, CentralSyncOutbox, _drain_one
from telegram_mt5_copier.channel_catalog import ChannelCatalogService
from telegram_mt5_copier.config import AppConfig
from telegram_mt5_copier.database import SignalDatabase
from telegram_mt5_copier.models import Direction, TradeSignal

LOCAL_DB_HOST = "127.0.0.1"
LOCAL_DB_PORT = 54322
LOCAL_DATABASE_URL = f"postgresql://postgres:postgres@{LOCAL_DB_HOST}:{LOCAL_DB_PORT}/postgres"
TEST_INSTANCE_ID = "etapa2_test"


def _local_postgres_reachable() -> bool:
    try:
        with socket.create_connection((LOCAL_DB_HOST, LOCAL_DB_PORT), timeout=1.0):
            return True
    except OSError:
        return False


def _fake_config(node_id: str = "dev-local") -> AppConfig:
    return AppConfig(
        project_root=Path("."),
        instance_id=TEST_INSTANCE_ID,
        brand_name="Etapa 2 Test",
        telegram_api_id=None,
        telegram_api_hash=None,
        telegram_bot_token=None,
        source_chat_id=None,
        source_chat_ids=(),
        destination_chat_id=None,
        bot_admin_ids=(),
        bot_enabled=False,
        dry_run=True,
        data_dir=Path("."),
        session_dir=Path("."),
        log_dir=Path("."),
        mt5_credential_key=None,
        mt5_template_path=None,
        mt5_broker_template_paths={},
        mt5_broker_servers={},
        mt5_base_dir=Path("."),
        mt5_execution_mode="simulation",
        mt5_max_accounts_per_vps=10,
        mt5_onboarding_url=None,
        client_app_url=None,
        onboarding_host="127.0.0.1",
        onboarding_port=8080,
        allow_live_accounts=False,
        default_pending_expiration_minutes=120,
        global_execution_kill_switch=True,
        operational_alerts_enabled=False,
        health_check_interval_seconds=30,
        health_stale_after_seconds=90,
        operational_alert_repeat_minutes=360,
        daily_performance_timezone="Europe/Athens",
        market_news_enabled=False,
        economic_calendar_api_key=None,
        market_news_minutes_before=10,
        market_news_minutes_after=10,
        market_news_poll_seconds=30,
        telegram_image_ocr_enabled=False,
        telegram_image_ocr_chat_ids=(),
        tesseract_command=None,
        peer_channel_sync_database_paths=(),
        resend_api_key=None,
        resend_from_email=None,
        node_id=node_id,
        node_label=node_id,
        central_sync_enabled=True,
        central_sync_database_url=LOCAL_DATABASE_URL,
        central_sync_poll_seconds=5,
        central_sync_max_batch=20,
        central_sync_delivery_lag_seconds=600,
        backup_encryption_key=None,
        backup_retention_days=14,
        b2_key_id=None,
        b2_application_key=None,
        b2_bucket_name=None,
    )


@unittest.skipUnless(_local_postgres_reachable(), "Postgres local (supabase start) nao esta rodando")
class CentralSyncIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp_dir.name) / "signals.sqlite3"
        SignalDatabase(self.database_path).initialize()
        self.catalog = ChannelCatalogService(self.database_path)
        self.catalog.register_configured_channel(
            telegram_chat_id="987654321",
            title="Canal Integracao",
            username=None,
            content_protected=False,
            history_accessible=True,
            last_message_id=None,
        )
        self.outbox = CentralSyncOutbox(self.database_path)
        self.config = _fake_config()
        self.client = CentralSyncClient(LOCAL_DATABASE_URL)
        await self.client.connect()
        await self._cleanup_rows()

    async def asyncTearDown(self) -> None:
        await self._cleanup_rows()
        await self.client.close()
        self.temp_dir.cleanup()

    async def _cleanup_rows(self) -> None:
        pool = self.client._pool
        await pool.execute(
            "delete from portal.signal_revisions where signal_id in (select id from portal.signals where instance_id = $1)",
            TEST_INSTANCE_ID,
        )
        await pool.execute("delete from portal.signals where instance_id = $1", TEST_INSTANCE_ID)
        await pool.execute("delete from portal.channels where instance_id = $1", TEST_INSTANCE_ID)
        await pool.execute("delete from portal.instances where id = $1", TEST_INSTANCE_ID)
        await pool.execute("delete from portal.nodes where id = $1", self.config.node_id)

    async def _upsert_registry(self) -> None:
        # Mesma sequencia que run_central_sync_drain_loop faz a cada iteracao,
        # antes de drenar qualquer linha do outbox (portal.channels/signals
        # tem FK obrigatoria para portal.instances).
        await self.client.upsert_node(self.config.node_id, self.config.node_label)
        await self.client.upsert_instance(self.config.instance_id, self.config.brand_name, node_id=self.config.node_id)

    def _make_signal(self, source_message_id: int = 501) -> TradeSignal:
        return TradeSignal(
            symbol="XAUUSD",
            direction=Direction.BUY,
            entry_low=Decimal("4103"),
            entry_high=Decimal("4105"),
            stop_loss=Decimal("4090"),
            take_profits=(Decimal("4110"), Decimal("4115")),
            raw_text="XAUUSD BUY\nENTRY 4103-4105\nSL 4090\nTP 4110\nTP 4115",
            source_chat_id="987654321",
            source_message_id=source_message_id,
        )

    async def test_shadow_write_cria_registro_central_e_uma_revisao(self) -> None:
        signal = self._make_signal()
        self.outbox.enqueue_signal_shadow_write(signal, "mensagem formatada", 9501)
        rows = self.outbox.claim_batch(10)
        self.assertEqual(len(rows), 1)

        await self._upsert_registry()
        await _drain_one(self.client, self.config, rows[0])
        self.outbox.mark_done(rows[0].id)

        pool = self.client._pool
        node_row = await pool.fetchrow("select id from portal.nodes where id = $1", self.config.node_id)
        self.assertIsNotNone(node_row)

        instance_row = await pool.fetchrow(
            "select id, node_id from portal.instances where id = $1", TEST_INSTANCE_ID
        )
        self.assertIsNotNone(instance_row)
        self.assertEqual(instance_row["node_id"], self.config.node_id)

        channel_row = await pool.fetchrow(
            "select id, source_channel_id, telegram_chat_id from portal.channels where instance_id = $1",
            TEST_INSTANCE_ID,
        )
        self.assertIsNotNone(channel_row)
        self.assertEqual(channel_row["telegram_chat_id"], "987654321")

        signal_row = await pool.fetchrow(
            "select id, content_signature from portal.signals where instance_id = $1 and source_message_id = $2",
            TEST_INSTANCE_ID,
            501,
        )
        self.assertIsNotNone(signal_row)
        self.assertEqual(signal_row["content_signature"], signal.content_signature)

        revision_count = await pool.fetchval(
            "select count(*) from portal.signal_revisions where signal_id = $1", signal_row["id"]
        )
        self.assertEqual(revision_count, 1)

    async def test_drenar_o_mesmo_sinal_duas_vezes_e_idempotente(self) -> None:
        await self._upsert_registry()
        signal = self._make_signal(source_message_id=502)
        self.outbox.enqueue_signal_shadow_write(signal, "mensagem formatada", 9502)
        rows = self.outbox.claim_batch(10)
        await _drain_one(self.client, self.config, rows[0])
        # Redrena a MESMA linha (simula retry apos falha parcial).
        await _drain_one(self.client, self.config, rows[0])

        pool = self.client._pool
        signal_row = await pool.fetchrow(
            "select id from portal.signals where instance_id = $1 and source_message_id = $2",
            TEST_INSTANCE_ID,
            502,
        )
        revision_count = await pool.fetchval(
            "select count(*) from portal.signal_revisions where signal_id = $1", signal_row["id"]
        )
        self.assertEqual(revision_count, 1)

    async def test_revisao_com_conteudo_diferente_atualiza_campos_estruturados(self) -> None:
        """Regressao da correcao Etapa 3 em portal.append_signal_revision():
        uma segunda revisao com conteudo diferente precisa atualizar
        symbol/direction/entry_low/etc em portal.signals, nao so content_signature."""
        await self._upsert_registry()
        signal_v1 = self._make_signal(source_message_id=504)
        self.outbox.enqueue_signal_shadow_write(signal_v1, "mensagem v1", 9504)
        rows = self.outbox.claim_batch(10)
        await _drain_one(self.client, self.config, rows[0])
        self.outbox.mark_done(rows[0].id)

        pool = self.client._pool
        signal_row = await pool.fetchrow(
            "select id from portal.signals where instance_id = $1 and source_message_id = $2",
            TEST_INSTANCE_ID,
            504,
        )
        signal_id = signal_row["id"]

        # Segunda revisao, conteudo bem diferente, aplicada diretamente via a
        # funcao (simula o que _drain_one faria se o mesmo sinal fosse
        # reprocessado com um payload novo).
        from telegram_mt5_copier.central_sync import build_outbox_payload, resolve_local_channel

        channel = resolve_local_channel(self.database_path, "987654321")
        signal_v2 = TradeSignal(
            symbol="EURUSD",
            direction=Direction.SELL,
            entry_low=Decimal("1.1000"),
            entry_high=Decimal("1.1010"),
            stop_loss=Decimal("1.1050"),
            take_profits=(Decimal("1.0950"),),
            raw_text="EURUSD SELL",
            source_chat_id="987654321",
            source_message_id=504,
        )
        payload_v2 = build_outbox_payload(signal_v2, "mensagem v2", channel)
        await self.client.append_signal_revision(signal_id, signal_v2.content_signature, payload_v2)

        updated = await pool.fetchrow(
            "select symbol, direction, entry_low, stop_loss from portal.signals where id = $1",
            signal_id,
        )
        self.assertEqual(updated["symbol"], "EURUSD")
        self.assertEqual(updated["direction"], "SELL")
        self.assertEqual(float(updated["entry_low"]), 1.1000)
        self.assertEqual(float(updated["stop_loss"]), 1.1050)

        revision_count = await pool.fetchval(
            "select count(*) from portal.signal_revisions where signal_id = $1", signal_id
        )
        self.assertEqual(revision_count, 2)

    async def test_nao_toca_tabelas_fora_do_escopo_da_etapa_2(self) -> None:
        pool = self.client._pool

        async def counts() -> dict[str, int]:
            return {
                "execution_jobs": await pool.fetchval("select count(*) from portal.execution_jobs"),
                "execution_job_orders": await pool.fetchval("select count(*) from portal.execution_job_orders"),
                "account_signal_claims": await pool.fetchval("select count(*) from portal.account_signal_claims"),
                "mt5_accounts": await pool.fetchval("select count(*) from portal.mt5_accounts"),
                "customers": await pool.fetchval("select count(*) from portal.customers"),
                "payments": await pool.fetchval("select count(*) from portal.payments"),
            }

        before = await counts()
        await self._upsert_registry()
        signal = self._make_signal(source_message_id=503)
        self.outbox.enqueue_signal_shadow_write(signal, "mensagem formatada", 9503)
        rows = self.outbox.claim_batch(10)
        await _drain_one(self.client, self.config, rows[0])
        after = await counts()

        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
