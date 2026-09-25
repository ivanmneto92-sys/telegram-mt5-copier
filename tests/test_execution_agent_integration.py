"""Testes de integracao da Etapa 4 (agente de execucao, so simulacao) contra
o stack local real do `supabase start` -- Postgres E Auth/GoTrue, os dois.
Pulados automaticamente se qualquer um dos dois nao responder. Nunca tocam
em Supabase remoto nem na VPS.

As chaves abaixo (ANON_KEY/SERVICE_ROLE_KEY) sao os valores PADRAO de
desenvolvimento local do Supabase CLI -- os mesmos em qualquer instalacao
local, documentados publicamente pelo proprio Supabase, nao um segredo
real."""

from __future__ import annotations

from datetime import datetime
import json
import socket
import unittest
import uuid

import asyncpg
import httpx

from telegram_mt5_copier.execution_agent import (
    AgentApiClient,
    ExecutionAgent,
    SimulationExecutionBackend,
    SupabaseAuthClient,
)

LOCAL_DB_HOST = "127.0.0.1"
LOCAL_DB_PORT = 54322
LOCAL_API_HOST = "127.0.0.1"
LOCAL_API_PORT = 54321
LOCAL_DATABASE_URL = f"postgresql://postgres:postgres@{LOCAL_DB_HOST}:{LOCAL_DB_PORT}/postgres"
LOCAL_API_URL = f"http://{LOCAL_API_HOST}:{LOCAL_API_PORT}"
LOCAL_ANON_KEY = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZS1kZW1vIiwicm9sZSI6ImFub24iLCJl"
    "eHAiOjE5ODM4MTI5OTZ9.CRXP1A7WOeoJeXxjNni43kdQwgnWNReilDMblYTn_I0"
)
LOCAL_SERVICE_ROLE_KEY = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZS1kZW1vIiwicm9sZSI6InNlcnZpY2Vf"
    "cm9sZSIsImV4cCI6MTk4MzgxMjk5Nn0.EGIM96RAZx35lJzdJsyH-qQwv8Hdp7fsn3W0YpN81IU"
)
TEST_INSTANCE_ID = "main"


def _tcp_reachable(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=1.0):
            return True
    except OSError:
        return False


def _local_stack_reachable() -> bool:
    return _tcp_reachable(LOCAL_DB_HOST, LOCAL_DB_PORT) and _tcp_reachable(LOCAL_API_HOST, LOCAL_API_PORT)


class NullLogger:
    def info(self, *args, **kwargs) -> None:
        pass

    def warning(self, *args, **kwargs) -> None:
        pass

    def error(self, *args, **kwargs) -> None:
        pass

    def exception(self, *args, **kwargs) -> None:
        pass


@unittest.skipUnless(
    _local_stack_reachable(), "Postgres/Auth locais (supabase start) nao estao rodando"
)
class ExecutionAgentIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        suffix = uuid.uuid4().hex[:10]
        self.node_id = f"agent-test-{suffix}"
        self.email = f"agent-test-{suffix}@example.com"
        self.password = "senha-de-teste-123!"
        self.source_seed = 900000 + int(suffix[:5], 16) % 90000  # base numerica unica por execucao

        self.pool = await asyncpg.create_pool(LOCAL_DATABASE_URL, min_size=1, max_size=2)
        self.http_client = httpx.AsyncClient()

        self.other_node_id: str | None = None
        self.user_id = await self._create_test_auth_user()
        await self._seed_registry()

        self.auth = SupabaseAuthClient(LOCAL_API_URL, LOCAL_ANON_KEY, self.email, self.password)
        self.client = AgentApiClient(LOCAL_API_URL, LOCAL_ANON_KEY, self.auth)
        self.backend = SimulationExecutionBackend()
        self.agent = ExecutionAgent(
            self.client, self.backend, claim_limit=5, lease_seconds=60, poll_seconds=0.01, logger=NullLogger()
        )

    async def asyncTearDown(self) -> None:
        await self.auth.close()
        await self.client.close()
        await self._cleanup_registry()
        await self.pool.close()
        await self._delete_test_auth_user()
        await self.http_client.aclose()

    async def _create_test_auth_user(self) -> str:
        response = await self.http_client.post(
            f"{LOCAL_API_URL}/auth/v1/admin/users",
            headers={
                "apikey": LOCAL_SERVICE_ROLE_KEY,
                "Authorization": f"Bearer {LOCAL_SERVICE_ROLE_KEY}",
                "Content-Type": "application/json",
            },
            json={"email": self.email, "password": self.password, "email_confirm": True},
        )
        response.raise_for_status()
        return response.json()["id"]

    async def _delete_test_auth_user(self) -> None:
        await self.http_client.delete(
            f"{LOCAL_API_URL}/auth/v1/admin/users/{self.user_id}",
            headers={
                "apikey": LOCAL_SERVICE_ROLE_KEY,
                "Authorization": f"Bearer {LOCAL_SERVICE_ROLE_KEY}",
            },
        )

    async def _seed_registry(self) -> None:
        await self.pool.execute(
            """
            insert into portal.nodes (id, label, auth_user_id, status, kill_switch_enabled)
            values ($1, $1, $2, 'active', false)
            """,
            self.node_id,
            self.user_id,
        )
        self.customer_id = await self.pool.fetchval(
            """
            insert into portal.customers (instance_id, source_user_id, status, created_at)
            values ($1, $2, 'active', now())
            returning id
            """,
            TEST_INSTANCE_ID,
            self.source_seed,
        )
        self.account_id = await self.pool.fetchval(
            """
            insert into portal.mt5_accounts (
                customer_id, instance_id, source_account_id, node_id, broker_name, server_name,
                account_type, connection_status, created_at
            ) values ($1, $2, $3, $4, 'XM', 'XM-Demo', 'demo', 'connected', now())
            returning id
            """,
            self.customer_id,
            TEST_INSTANCE_ID,
            self.source_seed,
            self.node_id,
        )
        self.channel_id = await self.pool.fetchval(
            """
            insert into portal.channels (instance_id, source_channel_id, title, status, access_status, created_at)
            values ($1, $2, 'Canal Agente Teste', 'active', 'confirmed', now())
            returning id
            """,
            TEST_INSTANCE_ID,
            self.source_seed,
        )

    async def _cleanup_registry(self) -> None:
        await self.pool.execute(
            "delete from portal.execution_attempts where execution_job_id in "
            "(select id from portal.execution_jobs where mt5_account_id = $1)",
            self.account_id,
        )
        await self.pool.execute(
            "delete from portal.execution_job_orders where execution_job_id in "
            "(select id from portal.execution_jobs where mt5_account_id = $1)",
            self.account_id,
        )
        await self.pool.execute("delete from portal.execution_jobs where mt5_account_id = $1", self.account_id)
        await self.pool.execute(
            "delete from portal.signal_revisions where signal_id in "
            "(select id from portal.signals where channel_id = $1)",
            self.channel_id,
        )
        await self.pool.execute("delete from portal.signals where channel_id = $1", self.channel_id)
        await self.pool.execute("delete from portal.mt5_accounts where id = $1", self.account_id)
        await self.pool.execute("delete from portal.channels where id = $1", self.channel_id)
        await self.pool.execute("delete from portal.customers where id = $1", self.customer_id)
        await self.pool.execute("delete from portal.nodes where id = $1", self.node_id)
        if self.other_node_id is not None:
            await self.pool.execute("delete from portal.nodes where id = $1", self.other_node_id)

    async def _seed_other_node(self) -> str:
        self.other_node_id = f"{self.node_id}-outro"
        await self.pool.execute(
            "insert into portal.nodes (id, label, status, kill_switch_enabled) values ($1, $1, 'active', false)",
            self.other_node_id,
        )
        return self.other_node_id

    async def _seed_signal(self, source_message_id: int) -> str:
        return await self.pool.fetchval(
            """
            insert into portal.signals (
                instance_id, channel_id, source_message_id, content_signature,
                symbol, direction, entry_low, entry_high, stop_loss, take_profits
            ) values ($1, $2, $3, $4, 'XAUUSD', 'BUY', 4103, 4105, 4090, '[4110]'::jsonb)
            returning id
            """,
            TEST_INSTANCE_ID,
            self.channel_id,
            source_message_id,
            f"sig-agent-{source_message_id}",
        )

    async def _seed_pending_job(
        self,
        source_message_id: int,
        *,
        payload: dict | None = None,
        node_id: str | None = None,
        status: str = "pending",
        reserved_by: str | None = None,
        reserved_until: str | None = None,
        reservation_token: str | None = None,
    ) -> str:
        signal_id = await self._seed_signal(source_message_id)
        payload = payload if payload is not None else {
            "symbol": "XAUUSD",
            "direction": "BUY",
            "orders": [{"tp_index": 1, "requested_volume": "0.01"}],
        }
        return await self.pool.fetchval(
            """
            insert into portal.execution_jobs (
                instance_id, mt5_account_id, node_id, signal_id, content_signature, status,
                payload, reserved_by, reserved_until, reservation_token
            ) values ($1, $2, $3, $4, $5, $6, $7::jsonb, $8, $9::timestamptz, $10::uuid)
            returning id
            """,
            TEST_INSTANCE_ID,
            self.account_id,
            node_id or self.node_id,
            signal_id,
            f"sig-agent-{source_message_id}",
            status,
            json.dumps(payload),
            reserved_by,
            datetime.fromisoformat(reserved_until) if reserved_until is not None else None,
            reservation_token,
        )

    async def _job_row(self, job_id: str) -> asyncpg.Record:
        return await self.pool.fetchrow("select * from portal.execution_jobs where id = $1", job_id)

    async def test_fluxo_completo_claim_start_simula_completa(self) -> None:
        job_id = await self._seed_pending_job(701)

        processed = await self.agent.run_once()

        self.assertEqual(processed, 1)
        row = await self._job_row(job_id)
        self.assertEqual(row["status"], "succeeded")
        result = json.loads(row["result"]) if isinstance(row["result"], str) else row["result"]
        self.assertEqual(result["mode"], "simulation")
        self.assertEqual(result["symbol"], "XAUUSD")

    async def test_lease_expirado_de_outro_no_e_reivindicado(self) -> None:
        past_token = str(uuid.uuid4())
        job_id = await self._seed_pending_job(
            702,
            status="reserved",
            reserved_by=self.node_id,
            reserved_until="2020-01-01T00:00:00+00:00",
            reservation_token=past_token,
        )

        processed = await self.agent.run_once()

        self.assertEqual(processed, 1)
        row = await self._job_row(job_id)
        self.assertEqual(row["status"], "succeeded")
        self.assertNotEqual(str(row["reservation_token"]), past_token)  # reivindicado com token novo

    async def test_agente_nunca_reivindica_job_de_outro_no(self) -> None:
        other_node_id = await self._seed_other_node()
        job_id = await self._seed_pending_job(703, node_id=other_node_id)

        processed = await self.agent.run_once()

        self.assertEqual(processed, 0)
        row = await self._job_row(job_id)
        self.assertEqual(row["status"], "pending")  # intocado

    async def test_payload_invalido_aciona_fail_execution_job(self) -> None:
        job_id = await self._seed_pending_job(704, payload={"symbol": "XAUUSD"})  # sem "orders"

        await self.agent.run_once()

        row = await self._job_row(job_id)
        self.assertEqual(row["status"], "retry_wait")
        self.assertEqual(row["last_error_code"], "ExecutionBackendError")

    async def test_processa_varios_jobs_pendentes_na_mesma_leva(self) -> None:
        job_id_1 = await self._seed_pending_job(705)
        job_id_2 = await self._seed_pending_job(706)

        processed = await self.agent.run_once()

        self.assertEqual(processed, 2)
        row_1 = await self._job_row(job_id_1)
        row_2 = await self._job_row(job_id_2)
        self.assertEqual(row_1["status"], "succeeded")
        self.assertEqual(row_2["status"], "succeeded")


if __name__ == "__main__":
    unittest.main()
