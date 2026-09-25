"""Etapa 4: agente de execucao da VPS -- so modo simulacao nesta etapa (nunca
chama MT5/corretora nenhuma). Consumidor standalone das RPCs de agent_api
(Etapa 1), autenticado via Supabase Auth (JWT) -- diferente de
central_sync.py, que fala com o Postgres direto via asyncpg: as RPCs de
agent_api resolvem auth.uid() de um JWT real do Supabase Auth, uma conexao
Postgres direta nunca vai satisfazer isso.

Sem estado local persistente: o mecanismo de reservation_token/lease ja
garantido pelas proprias RPCs (Etapa 1) e suficiente -- um crash no meio de
um job so deixa o lease expirar e ser reivindicado de novo por qualquer
instancia, sem precisar de nenhuma tabela SQLite nova aqui (ao contrario do
outbox da Etapa 2, que precisa ser duravel porque e a fonte da verdade
local).

Nesta etapa, nada cria jobs 'pending' de verdade em portal.execution_jobs --
isso e trabalho de uma etapa futura. Este modulo e testado com jobs
inseridos manualmente (fixture), provando o ciclo de vida completo
(claim -> start -> executar -> complete/fail) contra o Postgres/Auth locais.
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import logging
from logging.handlers import RotatingFileHandler
import sys
from typing import Any

import httpx

from .config import AppConfig

_TOKEN_REFRESH_MARGIN_SECONDS = 60


class SupabaseAuthError(RuntimeError):
    pass


class SupabaseAuthClient:
    """Login/renovacao de token via Supabase Auth (GoTrue), por email/senha.
    A senha nunca e gerada nem logada por este codigo -- vem de
    EXECUTION_AGENT_PASSWORD, definida por voce direto no painel do Supabase
    (Authentication > Users), mesma disciplina do CENTRAL_SYNC_DATABASE_URL."""

    def __init__(
        self,
        base_url: str,
        anon_key: str,
        email: str,
        password: str,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.anon_key = anon_key
        self.email = email
        self._password: str | None = password
        self._client = client or httpx.AsyncClient(timeout=10.0)
        self._owns_client = client is None
        self._access_token: str | None = None
        self._refresh_token: str | None = None
        self._expires_at: datetime | None = None

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def access_token(self) -> str:
        if self._access_token is None or self._is_expiring_soon():
            await self._login_or_refresh()
        assert self._access_token is not None
        return self._access_token

    def _is_expiring_soon(self) -> bool:
        if self._expires_at is None:
            return True
        margin = timedelta(seconds=_TOKEN_REFRESH_MARGIN_SECONDS)
        return datetime.now(tz=timezone.utc) >= self._expires_at - margin

    async def _login_or_refresh(self) -> None:
        if self._refresh_token is not None:
            try:
                await self._token_request(
                    {"refresh_token": self._refresh_token}, grant_type="refresh_token"
                )
                return
            except SupabaseAuthError:
                pass  # refresh falhou (expirado/revogado) -- tenta login de novo abaixo
        if self._password is None:
            raise SupabaseAuthError("sem senha disponivel para novo login (refresh falhou).")
        await self._token_request(
            {"email": self.email, "password": self._password}, grant_type="password"
        )

    async def _token_request(self, payload: dict[str, Any], *, grant_type: str) -> None:
        response = await self._client.post(
            f"{self.base_url}/auth/v1/token",
            params={"grant_type": grant_type},
            headers={"apikey": self.anon_key, "Content-Type": "application/json"},
            json=payload,
        )
        if response.status_code >= 400:
            raise SupabaseAuthError(
                f"falha de autenticacao Supabase ({response.status_code}): {response.text}"
            )
        data = response.json()
        self._access_token = data["access_token"]
        self._refresh_token = data["refresh_token"]
        self._expires_at = datetime.now(tz=timezone.utc) + timedelta(seconds=int(data["expires_in"]))


class AgentApiError(RuntimeError):
    pass


class AgentApiClient:
    """Chama as RPCs de agent_api (Etapa 1) via PostgREST. Accept-Profile/
    Content-Profile: agent_api sao obrigatorios em toda chamada -- sem eles o
    PostgREST tenta rotear pro schema "public" default e a RPC nao e
    encontrada (confirmado ao vivo contra o Postgres local antes de escrever
    este modulo)."""

    def __init__(
        self,
        base_url: str,
        anon_key: str,
        auth: SupabaseAuthClient,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.anon_key = anon_key
        self.auth = auth
        self._client = client or httpx.AsyncClient(timeout=10.0)
        self._owns_client = client is None

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _rpc(self, function_name: str, payload: dict[str, Any]) -> Any:
        token = await self.auth.access_token()
        response = await self._client.post(
            f"{self.base_url}/rest/v1/rpc/{function_name}",
            headers={
                "apikey": self.anon_key,
                "Authorization": f"Bearer {token}",
                "Accept-Profile": "agent_api",
                "Content-Profile": "agent_api",
                "Content-Type": "application/json",
            },
            json=payload,
        )
        if response.status_code >= 400:
            raise AgentApiError(
                f"agent_api.{function_name} falhou ({response.status_code}): {response.text}"
            )
        return response.json()

    async def claim_execution_jobs(self, *, limit: int, lease_seconds: int) -> list[dict[str, Any]]:
        return await self._rpc(
            "claim_execution_jobs", {"p_limit": limit, "p_lease_seconds": lease_seconds}
        )

    async def start_execution_job(self, job_id: str, reservation_token: str) -> dict[str, Any]:
        return await self._rpc(
            "start_execution_job",
            {"p_job_id": job_id, "p_reservation_token": reservation_token},
        )

    async def renew_execution_lease(
        self, job_id: str, reservation_token: str, *, lease_seconds: int
    ) -> dict[str, Any]:
        return await self._rpc(
            "renew_execution_lease",
            {
                "p_job_id": job_id,
                "p_reservation_token": reservation_token,
                "p_lease_seconds": lease_seconds,
            },
        )

    async def complete_execution_job(
        self,
        job_id: str,
        reservation_token: str,
        *,
        status: str,
        result: dict[str, Any],
        orders: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "p_job_id": job_id,
            "p_reservation_token": reservation_token,
            "p_status": status,
            "p_result": result,
        }
        if orders is not None:
            payload["p_orders"] = orders
        return await self._rpc("complete_execution_job", payload)

    async def fail_execution_job(
        self,
        job_id: str,
        reservation_token: str,
        *,
        error_code: str,
        error_message: str | None = None,
        retry_delay_seconds: int = 30,
    ) -> dict[str, Any]:
        return await self._rpc(
            "fail_execution_job",
            {
                "p_job_id": job_id,
                "p_reservation_token": reservation_token,
                "p_error_code": error_code,
                "p_error_message": error_message,
                "p_retry_delay_seconds": retry_delay_seconds,
            },
        )


class ExecutionBackendError(RuntimeError):
    pass


@dataclass(frozen=True)
class SimulatedExecutionResult:
    summary: dict[str, Any]


class SimulationExecutionBackend:
    """Nunca fala com MT5/corretora nenhuma -- so interpreta o payload do job
    (ver contrato no plano da Etapa 4) e produz um resultado "como se tivesse
    executado". Ponto de extensao pra um backend real (Etapa 5+) entrar
    depois sem reestruturar o agente, mesmo padrao de
    PendingOrderExecutor(client_factory=...)."""

    async def execute(self, job: dict[str, Any]) -> SimulatedExecutionResult:
        payload = job.get("payload") or {}
        orders = payload.get("orders")
        if not orders:
            raise ExecutionBackendError("payload sem 'orders' -- nada para simular.")
        return SimulatedExecutionResult(
            summary={
                "mode": "simulation",
                "symbol": payload.get("symbol"),
                "direction": payload.get("direction"),
                "planned_orders": orders,
            }
        )


class ExecutionAgent:
    def __init__(
        self,
        client: AgentApiClient,
        backend: SimulationExecutionBackend,
        *,
        claim_limit: int,
        lease_seconds: int,
        poll_seconds: float,
        logger: logging.Logger,
    ) -> None:
        self.client = client
        self.backend = backend
        self.claim_limit = claim_limit
        self.lease_seconds = lease_seconds
        self.poll_seconds = poll_seconds
        self.logger = logger

    async def run_forever(self) -> None:
        while True:
            await self.run_once()
            await asyncio.sleep(self.poll_seconds)

    async def run_once(self) -> int:
        """Reivindica e processa um lote de jobs; devolve quantos jobs vieram
        na leva (util em testes, sem precisar do loop infinito)."""
        try:
            jobs = await self.client.claim_execution_jobs(
                limit=self.claim_limit, lease_seconds=self.lease_seconds
            )
        except Exception as exc:
            self.logger.warning("execution_agent_claim_failed: %s", exc)
            return 0
        for job in jobs:
            await self._process_job(job)
        return len(jobs)

    async def _process_job(self, job: dict[str, Any]) -> None:
        job_id = job["id"]
        reservation_token = job["reservation_token"]
        try:
            await self.client.start_execution_job(job_id, reservation_token)
            result = await self.backend.execute(job)
            await self.client.complete_execution_job(
                job_id, reservation_token, status="succeeded", result=result.summary
            )
            self.logger.info("execution_agent_job_succeeded id=%s", job_id)
        except Exception as exc:
            # Qualquer falha aqui (start/execute/complete) nunca derruba o
            # loop -- mesma disciplina de run_central_sync_drain_loop.
            # fail_execution_job (nao complete com status='rejected') porque
            # isso e sempre falha TECNICA neste modo -- nao existe rejeicao
            # de regra de negocio pra simular ainda (essa logica vive no
            # PendingOrderExecutor local, nao neste backend).
            self.logger.warning("execution_agent_job_failed id=%s erro=%s", job_id, exc)
            try:
                await self.client.fail_execution_job(
                    job_id, reservation_token, error_code=type(exc).__name__, error_message=str(exc)
                )
            except Exception:
                self.logger.exception("execution_agent_fail_report_failed id=%s", job_id)


async def run_execution_agent(config: AppConfig, logger: logging.Logger) -> int:
    if not config.supabase_url or not config.supabase_anon_key:
        logger.error("SUPABASE_URL/SUPABASE_ANON_KEY obrigatorios quando EXECUTION_AGENT_ENABLED=true.")
        return 2
    if not config.execution_agent_email or not config.execution_agent_password:
        logger.error(
            "EXECUTION_AGENT_EMAIL/EXECUTION_AGENT_PASSWORD obrigatorios quando EXECUTION_AGENT_ENABLED=true."
        )
        return 2

    auth = SupabaseAuthClient(
        config.supabase_url,
        config.supabase_anon_key,
        config.execution_agent_email,
        config.execution_agent_password,
    )
    client = AgentApiClient(config.supabase_url, config.supabase_anon_key, auth)
    backend = SimulationExecutionBackend()
    agent = ExecutionAgent(
        client,
        backend,
        claim_limit=config.execution_agent_claim_limit,
        lease_seconds=config.execution_agent_lease_seconds,
        poll_seconds=config.execution_agent_poll_seconds,
        logger=logger,
    )
    try:
        logger.info("execution_agent_started modo=simulacao")
        await agent.run_forever()
        return 0
    finally:
        await client.close()
        await auth.close()


def run(config: AppConfig, logger: logging.Logger) -> int:
    return asyncio.run(run_execution_agent(config, logger))


def _configure_logging(config: AppConfig) -> logging.Logger:
    logger = logging.getLogger("telegram_mt5_copier.execution_agent")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    logger.propagate = False

    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)

    config.log_dir.mkdir(parents=True, exist_ok=True)
    file_handler = RotatingFileHandler(
        config.log_dir / "execution_agent.log", maxBytes=2_000_000, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    return logger


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="telegram-mt5-execution-agent")
    parser.parse_args(argv)

    config = AppConfig.load(create_dirs=True)
    if not config.execution_agent_enabled:
        print("EXECUTION_AGENT_ENABLED=false -- nada a fazer.", file=sys.stderr)
        return 0

    logger = _configure_logging(config)
    return run(config, logger)


if __name__ == "__main__":
    sys.exit(main())
