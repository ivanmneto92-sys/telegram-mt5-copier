from __future__ import annotations

from decimal import Decimal
import json
from pathlib import Path
import tempfile
import unittest

import httpx

from telegram_mt5_copier.config import AppConfig
from telegram_mt5_copier.credential_service import CredentialService
from telegram_mt5_copier.execution_agent import (
    AgentApiClient,
    AgentApiError,
    ExecutionAgent,
    ExecutionBackendError,
    ExecutionOutcome,
    RealExecutionBackend,
    SimulationExecutionBackend,
    SupabaseAuthClient,
    SupabaseAuthError,
)
from telegram_mt5_copier.mt5.account_service import MT5AccountForm, MT5AccountService
from telegram_mt5_copier.mt5.client import SimulatedMT5Client
from telegram_mt5_copier.mt5.models import SymbolInfo, TickInfo
from telegram_mt5_copier.mt5.terminal_manager import TerminalManager
from telegram_mt5_copier.users import UserRepository
from tests.access_helpers import grant_paid_access


class NullLogger:
    def info(self, *args, **kwargs) -> None:
        pass

    def warning(self, *args, **kwargs) -> None:
        pass

    def error(self, *args, **kwargs) -> None:
        pass

    def exception(self, *args, **kwargs) -> None:
        pass


def token_response(access_token: str, refresh_token: str, expires_in: int = 3600) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "access_token": access_token,
            "refresh_token": refresh_token,
            "token_type": "bearer",
            "expires_in": expires_in,
        },
    )


class SupabaseAuthClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_primeiro_login_usa_grant_password(self) -> None:
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return token_response("token-1", "refresh-1")

        client = SupabaseAuthClient(
            "http://localhost:54321",
            "anon-key",
            "node@example.com",
            "senha-secreta",
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )

        token = await client.access_token()

        self.assertEqual(token, "token-1")
        self.assertEqual(len(requests), 1)
        request = requests[0]
        self.assertEqual(request.url.path, "/auth/v1/token")
        self.assertEqual(request.url.params["grant_type"], "password")
        self.assertEqual(request.headers["apikey"], "anon-key")
        body = json.loads(request.content)
        self.assertEqual(body, {"email": "node@example.com", "password": "senha-secreta"})

    async def test_token_valido_nao_faz_nova_chamada(self) -> None:
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return token_response("token-1", "refresh-1")

        client = SupabaseAuthClient(
            "http://localhost:54321",
            "anon-key",
            "node@example.com",
            "senha-secreta",
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )

        await client.access_token()
        await client.access_token()

        self.assertEqual(calls, 1)

    async def test_token_perto_de_expirar_dispara_refresh(self) -> None:
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            grant_type = request.url.params["grant_type"]
            if grant_type == "password":
                return token_response("token-1", "refresh-1", expires_in=1)
            return token_response("token-2", "refresh-2")

        client = SupabaseAuthClient(
            "http://localhost:54321",
            "anon-key",
            "node@example.com",
            "senha-secreta",
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )

        first = await client.access_token()
        second = await client.access_token()

        self.assertEqual(first, "token-1")
        self.assertEqual(second, "token-2")
        self.assertEqual(len(requests), 2)
        self.assertEqual(requests[1].url.params["grant_type"], "refresh_token")
        body = json.loads(requests[1].content)
        self.assertEqual(body, {"refresh_token": "refresh-1"})

    async def test_refresh_falhando_cai_para_login_de_novo(self) -> None:
        grant_types: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            grant_type = request.url.params["grant_type"]
            grant_types.append(grant_type)
            if grant_type == "password":
                return token_response("token-1", "refresh-1", expires_in=1)
            return httpx.Response(400, json={"error": "invalid_grant"})

        client = SupabaseAuthClient(
            "http://localhost:54321",
            "anon-key",
            "node@example.com",
            "senha-secreta",
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        await client.access_token()

        # Forca o segundo access_token() a tentar refresh (expira em 1s, ja
        # perto da margem de renovacao) -- refresh falha (400), cai pra login
        # de novo com a senha ainda guardada.
        token = await client.access_token()

        self.assertEqual(token, "token-1")
        self.assertEqual(grant_types, ["password", "refresh_token", "password"])

    async def test_login_falhando_levanta_supabase_auth_error(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(400, json={"error": "invalid_credentials"})

        client = SupabaseAuthClient(
            "http://localhost:54321",
            "anon-key",
            "node@example.com",
            "senha-errada",
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )

        with self.assertRaises(SupabaseAuthError):
            await client.access_token()


class FakeAuthClient:
    def __init__(self, token: str = "token-fixo") -> None:
        self.token = token

    async def access_token(self) -> str:
        return self.token


class AgentApiClientTests(unittest.IsolatedAsyncioTestCase):
    def _make_client(self, handler) -> AgentApiClient:
        return AgentApiClient(
            "http://localhost:54321",
            "anon-key",
            FakeAuthClient(),
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )

    async def test_claim_execution_jobs_monta_requisicao_certa(self) -> None:
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200, json=[{"id": "job-1"}])

        client = self._make_client(handler)

        result = await client.claim_execution_jobs(limit=5, lease_seconds=60)

        self.assertEqual(result, [{"id": "job-1"}])
        request = requests[0]
        self.assertEqual(request.method, "POST")
        self.assertEqual(request.url.path, "/rest/v1/rpc/claim_execution_jobs")
        self.assertEqual(request.headers["apikey"], "anon-key")
        self.assertEqual(request.headers["Authorization"], "Bearer token-fixo")
        self.assertEqual(request.headers["Accept-Profile"], "agent_api")
        self.assertEqual(request.headers["Content-Profile"], "agent_api")
        body = json.loads(request.content)
        self.assertEqual(body, {"p_limit": 5, "p_lease_seconds": 60})

    async def test_start_execution_job_endpoint_e_corpo_certos(self) -> None:
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200, json={"id": "job-1", "status": "executing"})

        client = self._make_client(handler)
        await client.start_execution_job("job-1", "token-abc")

        request = requests[0]
        self.assertEqual(request.url.path, "/rest/v1/rpc/start_execution_job")
        self.assertEqual(
            json.loads(request.content), {"p_job_id": "job-1", "p_reservation_token": "token-abc"}
        )

    async def test_complete_execution_job_omite_p_orders_quando_nao_informado(self) -> None:
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200, json={"id": "job-1", "status": "succeeded"})

        client = self._make_client(handler)
        await client.complete_execution_job(
            "job-1", "token-abc", status="succeeded", result={"mode": "simulation"}
        )

        body = json.loads(requests[0].content)
        self.assertNotIn("p_orders", body)
        self.assertEqual(body["p_status"], "succeeded")
        self.assertEqual(body["p_result"], {"mode": "simulation"})

    async def test_complete_execution_job_inclui_p_orders_quando_informado(self) -> None:
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200, json={"id": "job-1", "status": "succeeded"})

        client = self._make_client(handler)
        orders = [{"tp_index": 1, "execution_key": "abc12345T1"}]
        await client.complete_execution_job(
            "job-1", "token-abc", status="succeeded", result={}, orders=orders
        )

        body = json.loads(requests[0].content)
        self.assertEqual(body["p_orders"], orders)

    async def test_fail_execution_job_endpoint_e_corpo_certos(self) -> None:
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200, json={"id": "job-1", "status": "retry_wait"})

        client = self._make_client(handler)
        await client.fail_execution_job(
            "job-1", "token-abc", error_code="ValueError", error_message="algo deu errado"
        )

        request = requests[0]
        self.assertEqual(request.url.path, "/rest/v1/rpc/fail_execution_job")
        body = json.loads(request.content)
        self.assertEqual(body["p_error_code"], "ValueError")
        self.assertEqual(body["p_error_message"], "algo deu errado")
        self.assertEqual(body["p_retry_delay_seconds"], 30)

    async def test_renew_execution_lease_endpoint_e_corpo_certos(self) -> None:
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200, json={"id": "job-1", "status": "executing"})

        client = self._make_client(handler)
        await client.renew_execution_lease("job-1", "token-abc", lease_seconds=90)

        body = json.loads(requests[0].content)
        self.assertEqual(
            body, {"p_job_id": "job-1", "p_reservation_token": "token-abc", "p_lease_seconds": 90}
        )

    async def test_erro_da_rpc_levanta_agent_api_error_com_texto_real(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(400, json={"message": "node nao autorizado ou inativo"})

        client = self._make_client(handler)

        with self.assertRaises(AgentApiError) as ctx:
            await client.claim_execution_jobs(limit=1, lease_seconds=60)
        self.assertIn("node nao autorizado", str(ctx.exception))


class SimulationExecutionBackendTests(unittest.IsolatedAsyncioTestCase):
    async def test_execute_devolve_resumo_do_payload(self) -> None:
        backend = SimulationExecutionBackend()
        job = {
            "payload": {
                "symbol": "XAUUSD",
                "direction": "BUY",
                "orders": [{"tp_index": 1, "requested_volume": "0.01"}],
            }
        }

        outcome = await backend.execute(job)

        self.assertEqual(outcome.status, "succeeded")
        self.assertIsNone(outcome.orders)
        self.assertEqual(outcome.result["mode"], "simulation")
        self.assertEqual(outcome.result["symbol"], "XAUUSD")
        self.assertEqual(outcome.result["direction"], "BUY")
        self.assertEqual(outcome.result["planned_orders"], [{"tp_index": 1, "requested_volume": "0.01"}])

    async def test_execute_levanta_erro_sem_orders_no_payload(self) -> None:
        backend = SimulationExecutionBackend()

        with self.assertRaises(ExecutionBackendError):
            await backend.execute({"payload": {"symbol": "XAUUSD"}})

    async def test_execute_levanta_erro_sem_payload_nenhum(self) -> None:
        backend = SimulationExecutionBackend()

        with self.assertRaises(ExecutionBackendError):
            await backend.execute({})


class RealExecutionBackendTests(unittest.IsolatedAsyncioTestCase):
    """Etapa 5c: RealExecutionBackend reaproveita PendingOrderExecutor de
    verdade (via execute_for_account) -- testado com SimulatedMT5Client,
    mesmos cenarios ja provados em test_pending_orders.py (sucesso, rejeicao
    de order_check, duplicata), so que disparados por um job da fila em vez
    de um sinal local."""

    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.config = AppConfig.load(
            project_root=self.root,
            env={
                "MT5_CREDENTIAL_KEY": CredentialService.generate_key(),
                "GLOBAL_EXECUTION_KILL_SWITCH": "false",
            },
            create_dirs=True,
        )
        self.users = UserRepository(self.config.database_path)
        self.accounts_service = MT5AccountService(
            self.config.database_path,
            credential_service=CredentialService(self.config.mt5_credential_key),
            terminal_manager=TerminalManager(self.root / "mt5"),
            client_factory=lambda: SimulatedMT5Client(),
        )
        self.user = self.users.get_or_create_user(101, "alice")
        grant_paid_access(self.config.database_path, self.user.id)
        self.account = self.accounts_service.register_account(
            self.user.id,
            MT5AccountForm("Broker", "Broker-Demo", "12345678", "secret", "Demo"),
        )
        self.accounts_service.update_execution_profile_fixed_lot(self.user.id, self.account.id, Decimal("0.04"))

    async def asyncTearDown(self) -> None:
        self.accounts_service.close()
        self.users.close()
        self.temp_dir.cleanup()

    def _payload(self, **overrides) -> dict:
        # Mesmos valores de BUY_SIGNAL (tests/test_pending_orders.py) --
        # ja provados validos (order_check/send passam de verdade) com tick
        # bid/ask=4062, evita inventar numeros que colidam com validacao de
        # risco (ex.: preco ja tendo cruzado o SL antes da entrada).
        base = {
            "symbol": "XAUUSD",
            "direction": "BUY",
            "entry_low": "4059",
            "entry_high": "4061",
            "stop_loss": "4044",
            "take_profits": ["4066", "4071"],
            "raw_text": "XAUUSD BUY\nENTRY 4059-61\nSL 4044\nTP 4066\nTP 4071",
            "source_chat_id": "123456",
            "source_message_id": 1,
            "local_user_id": self.user.id,
            "local_account_id": self.account.id,
        }
        base.update(overrides)
        return base

    async def test_execucao_bem_sucedida_devolve_succeeded_com_orders_reais(self) -> None:
        client = SimulatedMT5Client(tick=TickInfo(bid=Decimal("4062"), ask=Decimal("4062")))
        backend = RealExecutionBackend(self.config, client_factory=lambda: client)

        outcome = await backend.execute({"payload": self._payload()})

        self.assertEqual(outcome.status, "succeeded")
        self.assertIsNotNone(outcome.orders)
        self.assertEqual(len(outcome.orders), 2)
        self.assertTrue(all(o["mt5_order_ticket"] is not None for o in outcome.orders))
        self.assertTrue(all(o["status"] == "sent" for o in outcome.orders))
        self.assertEqual(len(client.order_send_requests), 2)

    async def test_order_check_falhando_devolve_rejected_sem_levantar(self) -> None:
        client = SimulatedMT5Client(
            tick=TickInfo(bid=Decimal("4062"), ask=Decimal("4062")),
            order_check_results=[
                {"retcode": 0, "comment": "ok"},
                {"retcode": 10030, "comment": "invalid stops"},
            ],
        )
        backend = RealExecutionBackend(self.config, client_factory=lambda: client)

        outcome = await backend.execute({"payload": self._payload()})

        self.assertEqual(outcome.status, "rejected")
        self.assertIn("order_check_failed", outcome.result["rejection_code"])
        self.assertIsNone(outcome.orders)
        self.assertEqual(len(client.order_send_requests), 0)

    async def test_mesmo_sinal_duas_vezes_e_idempotente(self) -> None:
        client = SimulatedMT5Client(tick=TickInfo(bid=Decimal("4062"), ask=Decimal("4062")))
        backend = RealExecutionBackend(self.config, client_factory=lambda: client)
        payload = self._payload()

        first = await backend.execute({"payload": payload})
        second = await backend.execute({"payload": payload})

        self.assertEqual(first.status, "succeeded")
        self.assertEqual(second.status, "succeeded")
        self.assertEqual(second.result.get("duplicate"), True)
        self.assertIsNone(second.orders)
        self.assertEqual(len(client.order_send_requests), 2)  # so a primeira chamada enviou de verdade

    async def test_payload_sem_campo_obrigatorio_levanta_execution_backend_error(self) -> None:
        client = SimulatedMT5Client(tick=TickInfo(bid=Decimal("4062"), ask=Decimal("4062")))
        backend = RealExecutionBackend(self.config, client_factory=lambda: client)
        payload = self._payload()
        del payload["stop_loss"]

        with self.assertRaises(ExecutionBackendError):
            await backend.execute({"payload": payload})

    async def test_conta_local_inexistente_levanta_excecao(self) -> None:
        client = SimulatedMT5Client(tick=TickInfo(bid=Decimal("4062"), ask=Decimal("4062")))
        backend = RealExecutionBackend(self.config, client_factory=lambda: client)
        payload = self._payload(local_account_id=999999)

        with self.assertRaises(Exception):
            await backend.execute({"payload": payload})


class FakeAgentApiClientForAgent:
    def __init__(
        self,
        jobs_to_claim: list[dict],
        *,
        claim_error: Exception | None = None,
        start_error: Exception | None = None,
        fail_execution_job_error: Exception | None = None,
    ) -> None:
        self._jobs_to_claim = jobs_to_claim
        self.claim_error = claim_error
        self.start_error = start_error
        self.fail_execution_job_error = fail_execution_job_error
        self.claim_calls = 0
        self.started: list[tuple] = []
        self.completed: list[dict] = []
        self.failed: list[dict] = []

    async def claim_execution_jobs(self, *, limit: int, lease_seconds: int) -> list[dict]:
        self.claim_calls += 1
        if self.claim_error is not None:
            raise self.claim_error
        jobs, self._jobs_to_claim = self._jobs_to_claim, []
        return jobs

    async def start_execution_job(self, job_id: str, reservation_token: str) -> dict:
        if self.start_error is not None:
            raise self.start_error
        self.started.append((job_id, reservation_token))
        return {"id": job_id, "status": "executing"}

    async def complete_execution_job(self, job_id, reservation_token, *, status, result, orders=None):
        self.completed.append({"job_id": job_id, "status": status, "result": result, "orders": orders})
        return {"id": job_id, "status": status}

    async def fail_execution_job(
        self, job_id, reservation_token, *, error_code, error_message=None, retry_delay_seconds=30
    ):
        if self.fail_execution_job_error is not None:
            raise self.fail_execution_job_error
        self.failed.append({"job_id": job_id, "error_code": error_code, "error_message": error_message})
        return {"id": job_id, "status": "retry_wait"}


class FakeSimulationBackend:
    def __init__(self, *, raise_error: Exception | None = None) -> None:
        self.raise_error = raise_error
        self.calls: list[dict] = []

    async def execute(self, job: dict):
        self.calls.append(job)
        if self.raise_error is not None:
            raise self.raise_error
        return ExecutionOutcome(status="succeeded", result={"mode": "simulation"})


def make_job(job_id: str = "job-1", token: str = "reservation-1") -> dict:
    return {"id": job_id, "reservation_token": token, "payload": {"symbol": "XAUUSD", "orders": [{}]}}


class ExecutionAgentTests(unittest.IsolatedAsyncioTestCase):
    async def test_run_once_caminho_feliz_claim_start_complete(self) -> None:
        job = make_job()
        client = FakeAgentApiClientForAgent([job])
        backend = FakeSimulationBackend()
        agent = ExecutionAgent(
            client, backend, claim_limit=5, lease_seconds=60, poll_seconds=0.01, logger=NullLogger()
        )

        processed = await agent.run_once()

        self.assertEqual(processed, 1)
        self.assertEqual(client.started, [("job-1", "reservation-1")])
        self.assertEqual(len(client.completed), 1)
        self.assertEqual(client.completed[0]["status"], "succeeded")
        self.assertEqual(client.completed[0]["result"], {"mode": "simulation"})
        self.assertEqual(client.failed, [])

    async def test_run_once_sem_jobs_nao_chama_start_nem_complete(self) -> None:
        client = FakeAgentApiClientForAgent([])
        backend = FakeSimulationBackend()
        agent = ExecutionAgent(
            client, backend, claim_limit=5, lease_seconds=60, poll_seconds=0.01, logger=NullLogger()
        )

        processed = await agent.run_once()

        self.assertEqual(processed, 0)
        self.assertEqual(client.started, [])
        self.assertEqual(client.completed, [])

    async def test_erro_no_claim_nao_derruba_o_loop(self) -> None:
        client = FakeAgentApiClientForAgent([], claim_error=RuntimeError("supabase indisponivel"))
        backend = FakeSimulationBackend()
        agent = ExecutionAgent(
            client, backend, claim_limit=5, lease_seconds=60, poll_seconds=0.01, logger=NullLogger()
        )

        processed = await agent.run_once()

        self.assertEqual(processed, 0)

    async def test_erro_no_backend_chama_fail_execution_job(self) -> None:
        job = make_job()
        client = FakeAgentApiClientForAgent([job])
        backend = FakeSimulationBackend(raise_error=ExecutionBackendError("payload invalido"))
        agent = ExecutionAgent(
            client, backend, claim_limit=5, lease_seconds=60, poll_seconds=0.01, logger=NullLogger()
        )

        await agent.run_once()

        self.assertEqual(client.completed, [])
        self.assertEqual(len(client.failed), 1)
        self.assertEqual(client.failed[0]["job_id"], "job-1")
        self.assertEqual(client.failed[0]["error_code"], "ExecutionBackendError")

    async def test_erro_no_start_execution_job_tambem_chama_fail(self) -> None:
        job = make_job()
        client = FakeAgentApiClientForAgent([job], start_error=RuntimeError("token expirado"))
        backend = FakeSimulationBackend()
        agent = ExecutionAgent(
            client, backend, claim_limit=5, lease_seconds=60, poll_seconds=0.01, logger=NullLogger()
        )

        await agent.run_once()

        self.assertEqual(backend.calls, [])  # nunca chegou a executar
        self.assertEqual(len(client.failed), 1)

    async def test_fail_execution_job_falhando_tambem_nao_derruba_o_loop(self) -> None:
        job = make_job()
        client = FakeAgentApiClientForAgent(
            [job],
            start_error=RuntimeError("token expirado"),
            fail_execution_job_error=RuntimeError("supabase tambem fora do ar"),
        )
        backend = FakeSimulationBackend()
        agent = ExecutionAgent(
            client, backend, claim_limit=5, lease_seconds=60, poll_seconds=0.01, logger=NullLogger()
        )

        # Nao deve levantar nada -- best-effort, so loga.
        await agent.run_once()

    async def test_processa_varios_jobs_da_mesma_leva(self) -> None:
        jobs = [make_job("job-1", "res-1"), make_job("job-2", "res-2")]
        client = FakeAgentApiClientForAgent(jobs)
        backend = FakeSimulationBackend()
        agent = ExecutionAgent(
            client, backend, claim_limit=5, lease_seconds=60, poll_seconds=0.01, logger=NullLogger()
        )

        processed = await agent.run_once()

        self.assertEqual(processed, 2)
        self.assertEqual(len(client.completed), 2)


if __name__ == "__main__":
    unittest.main()
