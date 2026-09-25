"""Testes de integracao da Etapa 2 (shadow-write) contra Postgres local real
(supabase start). Pulados automaticamente se o Postgres local nao responder --
nunca tocam em Supabase remoto nem na VPS."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
import socket
import tempfile
import unittest

from tests.central_sync_execution_helpers import (
    make_execution_group,
    make_execution_order,
    make_mt5_account,
    make_pending_order_plan,
    seed_customer_and_account,
)
from telegram_mt5_copier.central_sync import CentralSyncClient, CentralSyncOutbox, _drain_one, _run_audit_cycle
from telegram_mt5_copier.channel_catalog import ChannelCatalogService
from telegram_mt5_copier.config import AppConfig
from telegram_mt5_copier.database import SignalDatabase, connect_database
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
        central_sync_audit_sample_size=5,
        central_sync_audit_interval_seconds=1800,
        execution_agent_enabled=False,
        execution_agent_mode="simulation",
        supabase_url=None,
        supabase_anon_key=None,
        execution_agent_email=None,
        execution_agent_password=None,
        execution_agent_poll_seconds=5,
        execution_agent_claim_limit=5,
        execution_agent_lease_seconds=60,
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


@unittest.skipUnless(_local_postgres_reachable(), "Postgres local (supabase start) nao esta rodando")
class ExecutionJobShadowWriteIntegrationTests(unittest.IsolatedAsyncioTestCase):
    """Finalizacao da Etapa 2: mirror de execution_jobs/execution_job_orders
    contra o Postgres local real -- inclui a regressao do bug de schema
    encontrado nesta revisao (execution_key nao pode ter unique GLOBAL, so
    contas diferentes executando o mesmo sinal no mesmo TP geram o mesmo
    execution_key)."""

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
        self.user_id, self.account_id = seed_customer_and_account(self.database_path, telegram_user_id=777001)
        self.account = make_mt5_account(self.account_id, self.user_id)
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
            "delete from portal.execution_job_orders where execution_job_id in "
            "(select id from portal.execution_jobs where instance_id = $1)",
            TEST_INSTANCE_ID,
        )
        await pool.execute("delete from portal.execution_jobs where instance_id = $1", TEST_INSTANCE_ID)
        await pool.execute("delete from portal.mt5_accounts where instance_id = $1", TEST_INSTANCE_ID)
        await pool.execute("delete from portal.customers where instance_id = $1", TEST_INSTANCE_ID)
        await pool.execute(
            "delete from portal.signal_revisions where signal_id in (select id from portal.signals where instance_id = $1)",
            TEST_INSTANCE_ID,
        )
        await pool.execute("delete from portal.signals where instance_id = $1", TEST_INSTANCE_ID)
        await pool.execute("delete from portal.channels where instance_id = $1", TEST_INSTANCE_ID)
        await pool.execute("delete from portal.instances where id = $1", TEST_INSTANCE_ID)
        await pool.execute("delete from portal.nodes where id = $1", self.config.node_id)

    async def _upsert_registry(self) -> None:
        await self.client.upsert_node(self.config.node_id, self.config.node_label)
        await self.client.upsert_instance(self.config.instance_id, self.config.brand_name, node_id=self.config.node_id)

    def _make_signal(self, source_message_id: int) -> TradeSignal:
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

    async def test_mirror_completo_popula_customers_accounts_jobs_orders(self) -> None:
        await self._upsert_registry()
        signal = self._make_signal(710)
        self.outbox.enqueue_signal_shadow_write(signal, "mensagem formatada", 8710)
        group = make_execution_group(1, self.account, signal)
        orders = (make_execution_order(group.id, 1), make_execution_order(group.id, 2))
        self.outbox.enqueue_execution_job_shadow_write(
            signal, self.account, group, orders, rejected_reason=None, local_group_id=group.id
        )

        rows = self.outbox.claim_batch(10)
        self.assertEqual(len(rows), 2)
        for row in rows:
            await _drain_one(self.client, self.config, row)
            self.outbox.mark_done(row.id)

        pool = self.client._pool
        customer_row = await pool.fetchrow(
            "select id, source_user_id, billing_status from portal.customers "
            "where instance_id = $1 and source_user_id = $2",
            TEST_INSTANCE_ID,
            self.user_id,
        )
        self.assertIsNotNone(customer_row)
        self.assertEqual(customer_row["billing_status"], "paid")

        account_row = await pool.fetchrow(
            "select id, customer_id, login_last4 from portal.mt5_accounts "
            "where instance_id = $1 and source_account_id = $2",
            TEST_INSTANCE_ID,
            self.account_id,
        )
        self.assertIsNotNone(account_row)
        self.assertEqual(account_row["customer_id"], customer_row["id"])
        self.assertEqual(account_row["login_last4"], "6655")

        job_row = await pool.fetchrow(
            "select id, status, mt5_account_id from portal.execution_jobs where mt5_account_id = $1",
            account_row["id"],
        )
        self.assertIsNotNone(job_row)
        self.assertEqual(job_row["status"], "succeeded")

        order_count = await pool.fetchval(
            "select count(*) from portal.execution_job_orders where execution_job_id = $1", job_row["id"]
        )
        self.assertEqual(order_count, 2)

    async def test_mirror_de_execucao_antes_do_sinal_drenar_falha_e_e_retentavel(self) -> None:
        await self._upsert_registry()
        signal = self._make_signal(711)
        # NAO drena o sinal ainda -- so o enqueue local, simula o item de
        # execucao chegando antes do de sinal ser drenado (ambos sao itens
        # separados do mesmo outbox, drenados um de cada vez).
        self.outbox.enqueue_signal_shadow_write(signal, "mensagem formatada", 8711)
        group = make_execution_group(2, self.account, signal)
        orders = (make_execution_order(group.id, 1),)
        self.outbox.enqueue_execution_job_shadow_write(
            signal, self.account, group, orders, rejected_reason=None, local_group_id=group.id
        )

        rows = self.outbox.claim_batch(10)
        execution_row = next(r for r in rows if r.kind == "execution_job_shadow_write")
        signal_row = next(r for r in rows if r.kind == "signal_shadow_write")

        with self.assertRaises(ValueError):
            await _drain_one(self.client, self.config, execution_row)

        # Agora drena o sinal, depois retenta a execucao -- deve funcionar.
        await _drain_one(self.client, self.config, signal_row)
        self.outbox.mark_done(signal_row.id)
        await _drain_one(self.client, self.config, execution_row)
        self.outbox.mark_done(execution_row.id)

        pool = self.client._pool
        job_count = await pool.fetchval(
            "select count(*) from portal.execution_jobs j "
            "join portal.mt5_accounts a on a.id = j.mt5_account_id where a.instance_id = $1",
            TEST_INSTANCE_ID,
        )
        self.assertEqual(job_count, 1)

    async def test_duas_contas_diferentes_mesmo_sinal_mesmo_tp_nao_colidem_em_execution_key(self) -> None:
        # Regressao do bug real de schema encontrado nesta revisao:
        # execution_key e derivado SO do sinal (group.signal_id), entao e
        # IGUAL pra qualquer conta que copie o mesmo sinal no mesmo TP -- a
        # antiga unique GLOBAL em execution_key quebraria aqui na segunda
        # gravacao. A migration 20260924020000 remove essa unique global,
        # mantendo so unique(execution_job_id, tp_index) (suficiente).
        await self._upsert_registry()
        signal = self._make_signal(712)
        self.outbox.enqueue_signal_shadow_write(signal, "mensagem formatada", 8712)
        rows = self.outbox.claim_batch(10)
        await _drain_one(self.client, self.config, rows[0])
        self.outbox.mark_done(rows[0].id)

        user_b, account_b_id = seed_customer_and_account(
            self.database_path, telegram_user_id=777002, login="1122334455"
        )
        account_b = make_mt5_account(account_b_id, user_b, login="1122334455")

        group_a = make_execution_group(3, self.account, signal)
        group_b = make_execution_group(4, account_b, signal)
        self.assertEqual(group_a.signal_id, group_b.signal_id)  # mesmo sinal -> mesmo execution_key

        self.outbox.enqueue_execution_job_shadow_write(
            signal, self.account, group_a, (make_execution_order(group_a.id, 1),),
            rejected_reason=None, local_group_id=group_a.id,
        )
        self.outbox.enqueue_execution_job_shadow_write(
            signal, account_b, group_b, (make_execution_order(group_b.id, 1),),
            rejected_reason=None, local_group_id=group_b.id,
        )

        execution_rows = [r for r in self.outbox.claim_batch(10) if r.kind == "execution_job_shadow_write"]
        self.assertEqual(len(execution_rows), 2)
        for row in execution_rows:
            await _drain_one(self.client, self.config, row)
            self.outbox.mark_done(row.id)

        pool = self.client._pool
        order_count = await pool.fetchval(
            "select count(*) from portal.execution_job_orders o "
            "join portal.execution_jobs j on j.id = o.execution_job_id "
            "join portal.mt5_accounts a on a.id = j.mt5_account_id "
            "where a.instance_id = $1",
            TEST_INSTANCE_ID,
        )
        self.assertEqual(order_count, 2)

    async def test_rejeicao_grava_status_rejected_com_error_code(self) -> None:
        await self._upsert_registry()
        signal = self._make_signal(713)
        self.outbox.enqueue_signal_shadow_write(signal, "mensagem formatada", 8713)
        rows = self.outbox.claim_batch(10)
        await _drain_one(self.client, self.config, rows[0])
        self.outbox.mark_done(rows[0].id)

        group = make_execution_group(5, self.account, signal)
        orders = (
            make_execution_order(group.id, 1, status="failed", mt5_order_ticket=None, broker_retcode="10004"),
        )
        self.outbox.enqueue_execution_job_shadow_write(
            signal, self.account, group, orders,
            rejected_reason="order_send_failed:requote", local_group_id=group.id,
        )
        execution_row = next(r for r in self.outbox.claim_batch(10) if r.kind == "execution_job_shadow_write")
        await _drain_one(self.client, self.config, execution_row)

        pool = self.client._pool
        job_row = await pool.fetchrow(
            "select status, last_error_code from portal.execution_jobs j "
            "join portal.mt5_accounts a on a.id = j.mt5_account_id where a.instance_id = $1",
            TEST_INSTANCE_ID,
        )
        self.assertEqual(job_row["status"], "rejected")
        self.assertEqual(job_row["last_error_code"], "order_send_failed:requote")

        order_row = await pool.fetchrow(
            "select o.status, o.mt5_order_ticket from portal.execution_job_orders o "
            "join portal.execution_jobs j on j.id = o.execution_job_id "
            "join portal.mt5_accounts a on a.id = j.mt5_account_id where a.instance_id = $1",
            TEST_INSTANCE_ID,
        )
        self.assertEqual(order_row["status"], "failed")
        self.assertIsNone(order_row["mt5_order_ticket"])


@unittest.skipUnless(_local_postgres_reachable(), "Postgres local (supabase start) nao esta rodando")
class ExecutionJobPendingLifecycleIntegrationTests(unittest.IsolatedAsyncioTestCase):
    """Etapa 5a: prova que o job 'pending' (produtor, enfileirado ANTES do
    order_send) e o job de resultado (Etapa 2, enfileirado DEPOIS) convergem
    pra MESMA linha central em portal.execution_jobs/execution_job_orders,
    transicionando pending -> succeeded de verdade -- inclui a regressao do
    bug de finished_at encontrado nesta revisao (upsert_execution_job gravava
    finished_at=now() incondicionalmente, mesmo pra um job ainda pending)."""

    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp_dir.name) / "signals.sqlite3"
        SignalDatabase(self.database_path).initialize()
        self.catalog = ChannelCatalogService(self.database_path)
        self.catalog.register_configured_channel(
            telegram_chat_id="987654321",
            title="Canal Integracao Pending",
            username=None,
            content_protected=False,
            history_accessible=True,
            last_message_id=None,
        )
        self.user_id, self.account_id = seed_customer_and_account(self.database_path, telegram_user_id=778001)
        self.account = make_mt5_account(self.account_id, self.user_id)
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
            "delete from portal.execution_job_orders where execution_job_id in "
            "(select id from portal.execution_jobs where instance_id = $1)",
            TEST_INSTANCE_ID,
        )
        await pool.execute("delete from portal.execution_jobs where instance_id = $1", TEST_INSTANCE_ID)
        await pool.execute("delete from portal.mt5_accounts where instance_id = $1", TEST_INSTANCE_ID)
        await pool.execute("delete from portal.customers where instance_id = $1", TEST_INSTANCE_ID)
        await pool.execute(
            "delete from portal.signal_revisions where signal_id in (select id from portal.signals where instance_id = $1)",
            TEST_INSTANCE_ID,
        )
        await pool.execute("delete from portal.signals where instance_id = $1", TEST_INSTANCE_ID)
        await pool.execute("delete from portal.channels where instance_id = $1", TEST_INSTANCE_ID)
        await pool.execute("delete from portal.instances where id = $1", TEST_INSTANCE_ID)
        await pool.execute("delete from portal.nodes where id = $1", self.config.node_id)

    async def _upsert_registry(self) -> None:
        await self.client.upsert_node(self.config.node_id, self.config.node_label)
        await self.client.upsert_instance(self.config.instance_id, self.config.brand_name, node_id=self.config.node_id)

    def _make_signal(self, source_message_id: int) -> TradeSignal:
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

    async def test_pending_depois_resultado_transiciona_a_mesma_linha_central(self) -> None:
        await self._upsert_registry()
        signal = self._make_signal(720)
        self.outbox.enqueue_signal_shadow_write(signal, "mensagem formatada", 8720)
        group = make_execution_group(1, self.account, signal)
        plan = make_pending_order_plan(self.account, signal)

        signal_row = next(r for r in self.outbox.claim_batch(10) if r.kind == "signal_shadow_write")
        await _drain_one(self.client, self.config, signal_row)
        self.outbox.mark_done(signal_row.id)

        # 1) Produtor: job nasce pending, ANTES de qualquer order_send.
        self.outbox.enqueue_execution_job_pending(signal, self.account, group, plan)
        pending_row = next(r for r in self.outbox.claim_batch(10) if r.kind == "execution_job_pending")
        await _drain_one(self.client, self.config, pending_row)
        self.outbox.mark_done(pending_row.id)

        pool = self.client._pool
        job_row = await pool.fetchrow(
            "select j.id, j.status, j.finished_at from portal.execution_jobs j "
            "join portal.mt5_accounts a on a.id = j.mt5_account_id where a.instance_id = $1",
            TEST_INSTANCE_ID,
        )
        self.assertEqual(job_row["status"], "pending")
        self.assertIsNone(job_row["finished_at"])  # regressao do bug do finished_at
        job_id = job_row["id"]

        order_rows = await pool.fetch(
            "select status, mt5_order_ticket from portal.execution_job_orders where execution_job_id = $1",
            job_id,
        )
        self.assertEqual(len(order_rows), 2)
        self.assertTrue(all(row["status"] == "pending" for row in order_rows))
        self.assertTrue(all(row["mt5_order_ticket"] is None for row in order_rows))

        # 2) Etapa 2 (ja existente): job de resultado, DEPOIS do order_send real.
        orders = (make_execution_order(group.id, 1), make_execution_order(group.id, 2))
        self.outbox.enqueue_execution_job_shadow_write(
            signal, self.account, group, orders, rejected_reason=None, local_group_id=group.id
        )
        outcome_row = next(r for r in self.outbox.claim_batch(10) if r.kind == "execution_job_shadow_write")
        await _drain_one(self.client, self.config, outcome_row)
        self.outbox.mark_done(outcome_row.id)

        job_row_after = await pool.fetchrow(
            "select id, status, finished_at from portal.execution_jobs where id = $1", job_id
        )
        self.assertEqual(job_row_after["id"], job_id)  # mesma linha, transicionou de verdade
        self.assertEqual(job_row_after["status"], "succeeded")
        self.assertIsNotNone(job_row_after["finished_at"])

        order_rows_after = await pool.fetch(
            "select status, mt5_order_ticket from portal.execution_job_orders where execution_job_id = $1",
            job_id,
        )
        self.assertEqual(len(order_rows_after), 2)
        self.assertTrue(all(row["status"] == "sent" for row in order_rows_after))
        self.assertTrue(all(row["mt5_order_ticket"] == 123456 for row in order_rows_after))

    async def test_execution_key_do_pending_bate_com_o_do_resultado(self) -> None:
        await self._upsert_registry()
        signal = self._make_signal(721)
        self.outbox.enqueue_signal_shadow_write(signal, "mensagem formatada", 8721)
        signal_row = next(r for r in self.outbox.claim_batch(10) if r.kind == "signal_shadow_write")
        await _drain_one(self.client, self.config, signal_row)
        self.outbox.mark_done(signal_row.id)

        group = make_execution_group(2, self.account, signal)
        plan = make_pending_order_plan(self.account, signal, tp_indices=(1,))
        self.outbox.enqueue_execution_job_pending(signal, self.account, group, plan)
        pending_row = next(r for r in self.outbox.claim_batch(10) if r.kind == "execution_job_pending")
        await _drain_one(self.client, self.config, pending_row)
        self.outbox.mark_done(pending_row.id)

        pool = self.client._pool
        execution_key_before = await pool.fetchval(
            "select o.execution_key from portal.execution_job_orders o "
            "join portal.execution_jobs j on j.id = o.execution_job_id "
            "join portal.mt5_accounts a on a.id = j.mt5_account_id where a.instance_id = $1",
            TEST_INSTANCE_ID,
        )

        orders = (make_execution_order(group.id, 1),)
        self.outbox.enqueue_execution_job_shadow_write(
            signal, self.account, group, orders, rejected_reason=None, local_group_id=group.id
        )
        outcome_row = next(r for r in self.outbox.claim_batch(10) if r.kind == "execution_job_shadow_write")
        await _drain_one(self.client, self.config, outcome_row)
        self.outbox.mark_done(outcome_row.id)

        execution_key_after = await pool.fetchval(
            "select o.execution_key from portal.execution_job_orders o "
            "join portal.execution_jobs j on j.id = o.execution_job_id "
            "join portal.mt5_accounts a on a.id = j.mt5_account_id where a.instance_id = $1",
            TEST_INSTANCE_ID,
        )
        self.assertEqual(execution_key_before, execution_key_after)


@unittest.skipUnless(_local_postgres_reachable(), "Postgres local (supabase start) nao esta rodando")
class ContentAuditIntegrationTests(unittest.IsolatedAsyncioTestCase):
    """Etapa 3B: auditoria por amostragem contra o Postgres local real -- prova
    que um drift REAL no Supabase (nao so no SQLite local) e detectado, e que
    uma correcao subsequente limpa o achado."""

    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp_dir.name) / "signals.sqlite3"
        self.database = SignalDatabase(self.database_path)
        self.database.initialize()
        self.catalog = ChannelCatalogService(self.database_path)
        self.catalog.register_configured_channel(
            telegram_chat_id="987654321",
            title="Canal Auditoria",
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
        self.database.close()
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
        await self.client.upsert_node(self.config.node_id, self.config.node_label)
        await self.client.upsert_instance(self.config.instance_id, self.config.brand_name, node_id=self.config.node_id)

    def _make_signal(self, source_message_id: int) -> TradeSignal:
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

    def _findings(self) -> dict[int, str | None]:
        with connect_database(self.database_path) as connection:
            return {
                row[0]: row[1]
                for row in connection.execute(
                    "SELECT signal_id, remote_content_signature FROM central_sync_audit_findings"
                )
            }

    async def _drain_signal(self, source_message_id: int) -> tuple[int, str]:
        signal = self._make_signal(source_message_id)
        local_signal_id = self.database.record_accepted(signal, "mensagem formatada")
        self.outbox.enqueue_signal_shadow_write(signal, "mensagem formatada", local_signal_id)
        rows = self.outbox.claim_batch(10)
        row = next(r for r in rows if r.payload.get("source_message_id") == str(source_message_id))
        await _drain_one(self.client, self.config, row)
        self.outbox.mark_done(row.id)
        return local_signal_id, signal.content_signature

    async def test_auditoria_nao_encontra_nada_quando_conteudo_bate(self) -> None:
        await self._upsert_registry()
        await self._drain_signal(801)

        await _run_audit_cycle(self.client, self.outbox, self.config)

        self.assertEqual(self._findings(), {})

    async def test_auditoria_detecta_drift_real_no_supabase_e_limpa_apos_correcao(self) -> None:
        await self._upsert_registry()
        local_id, original_signature = await self._drain_signal(802)

        # Simula um drift REAL no Supabase -- direto via SQL, fora do caminho
        # normal de escrita (append_signal_revision), pra provar que a
        # auditoria pega mesmo o que o proprio codigo de replicacao nunca
        # produziria sozinho.
        pool = self.client._pool
        await pool.execute(
            """
            update portal.signals set content_signature = 'drift-simulado'
            where instance_id = $1 and source_message_id = 802
            """,
            TEST_INSTANCE_ID,
        )

        await _run_audit_cycle(self.client, self.outbox, self.config)

        self.assertEqual(self._findings(), {local_id: "drift-simulado"})

        # Reverte a corrupcao -- a proxima auditoria deve fechar o achado.
        await pool.execute(
            """
            update portal.signals set content_signature = $1
            where instance_id = $2 and source_message_id = 802
            """,
            original_signature,
            TEST_INSTANCE_ID,
        )
        await _run_audit_cycle(self.client, self.outbox, self.config)

        self.assertEqual(self._findings(), {})

    async def test_auditoria_detecta_sinal_marcado_done_mas_nunca_replicado(self) -> None:
        # Situacao hipotetica de bug (done normalmente so acontece apos um
        # upsert real ter sucesso) -- a auditoria precisa pegar mesmo essa
        # divergencia extrema (sinal 'done' localmente, mas ausente no
        # Supabase de verdade).
        await self._upsert_registry()
        signal = self._make_signal(803)
        local_signal_id = self.database.record_accepted(signal, "mensagem formatada")
        self.outbox.enqueue_signal_shadow_write(signal, "mensagem formatada", local_signal_id)
        rows = self.outbox.claim_batch(10)
        row = next(r for r in rows if r.payload.get("source_message_id") == "803")
        self.outbox.mark_done(row.id)  # marca done sem de fato drenar

        await _run_audit_cycle(self.client, self.outbox, self.config)

        self.assertEqual(self._findings(), {local_signal_id: None})


if __name__ == "__main__":
    unittest.main()
