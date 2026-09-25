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

Dois backends de execucao: SimulationExecutionBackend (Etapa 4, default,
nunca fala com corretora nenhuma -- testado com jobs inseridos manualmente,
fixture) e RealExecutionBackend (Etapa 5c, EXECUTION_AGENT_MODE=demo_execution
-- executa de verdade numa conta demo dedicada e isolada do caminho local,
reaproveitando o mesmo PendingOrderExecutor/pipeline de seguranca ja
validado localmente, nunca duplicando logica de execucao).
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import logging
from logging.handlers import RotatingFileHandler
import sys
from typing import Any

import httpx

from .central_sync import build_execution_key
from .config import AppConfig
from .models import Direction, TradeSignal, decimal_to_text

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
class ExecutionOutcome:
    """Resultado de um backend de execucao -- succeeded ou rejected sao os
    dois unicos status validos de agent_api.complete_execution_job (rejected
    e desfecho de regra de negocio, ex.: spread/risco/noticia -- diferente de
    uma excecao, que e sempre falha TECNICA e vira fail_execution_job, nunca
    isto aqui). orders so e preenchido quando a execucao chegou a tentar
    enviar ordem de verdade pra um broker (RealExecutionBackend, Etapa 5c) --
    SimulationExecutionBackend nunca preenche, ja que nunca fala com
    corretora nenhuma."""

    status: str
    result: dict[str, Any]
    orders: list[dict[str, Any]] | None = None


class SimulationExecutionBackend:
    """Nunca fala com MT5/corretora nenhuma -- so interpreta o payload do job
    (ver contrato no plano da Etapa 4) e produz um resultado "como se tivesse
    executado". Ponto de extensao pra um backend real (RealExecutionBackend,
    Etapa 5c) entrar depois sem reestruturar o agente, mesmo padrao de
    PendingOrderExecutor(client_factory=...)."""

    async def execute(self, job: dict[str, Any]) -> ExecutionOutcome:
        payload = job.get("payload") or {}
        orders = payload.get("orders")
        if not orders:
            raise ExecutionBackendError("payload sem 'orders' -- nada para simular.")
        return ExecutionOutcome(
            status="succeeded",
            result={
                "mode": "simulation",
                "symbol": payload.get("symbol"),
                "direction": payload.get("direction"),
                "planned_orders": orders,
            },
        )


class RealExecutionBackend:
    """Etapa 5c: modo demo_execution -- executa de verdade no MT5 (conta
    demo, sem risco financeiro real, mas ordem de verdade na corretora).
    Reaproveita o MESMO PendingOrderExecutor/pipeline de seguranca do
    caminho local (kill switch, conexao, limites de risco, janela de
    noticia) via execute_for_account -- nunca duplica logica de execucao.
    Por isso roda NA MESMA maquina/VPS que o terminal MT5 da conta (o
    MT5Client precisa do terminal local de qualquer jeito, igual ao caminho
    local hoje)."""

    def __init__(self, config: AppConfig, *, client_factory=None) -> None:
        # Imports tardios: mt5/* so faz sentido importar quando esse modo
        # realmente esta em uso (SimulationExecutionBackend nunca precisa
        # de nenhuma dependencia local -- mantem esse caminho, o default,
        # livre de qualquer peso extra).
        from .credential_service import CredentialService
        from .market_news import MarketNewsService
        from .mt5.account_service import MT5AccountService
        from .mt5.client import MT5Client
        from .mt5.pending_order_executor import PendingOrderExecutor

        credential_service = CredentialService(config.mt5_credential_key) if config.mt5_credential_key else None
        if credential_service is None:
            raise ExecutionBackendError("MT5_CREDENTIAL_KEY obrigatoria para EXECUTION_AGENT_MODE=demo_execution.")
        # Trava de seguranca essencial: portal.execution_jobs recebe jobs de
        # DUAS origens bem diferentes -- espelhos informativos de execucoes
        # LOCAIS reais (Etapa 2/5a, para QUALQUER conta demo/live, nunca so
        # a piloto) e jobs de verdade da conta piloto (Etapa 5d). claim_execution_jobs
        # nao distingue as duas coisas (nao ha esse conceito no schema remoto)
        # -- sem esta checagem, o agente poderia reivindicar e reexecutar de
        # verdade o job espelho de um cliente real que ja esta executando
        # localmente, causando ordem duplicada. So contas explicitamente
        # listadas aqui podem ser executadas de verdade por este backend.
        self.pilot_account_ids = frozenset(config.queue_pilot_account_ids)
        self.accounts = MT5AccountService(
            config.database_path,
            credential_service=credential_service,
            allow_live_accounts=False,  # nunca real nesta etapa, mesmo se mal configurado
            max_accounts_per_vps=config.mt5_max_accounts_per_vps,
            daily_performance_timezone=config.daily_performance_timezone,
        )
        self.executor = PendingOrderExecutor(
            config.database_path,
            self.accounts,
            execution_mode="demo_execution",
            global_kill_switch=config.global_execution_kill_switch,
            allow_live_accounts=False,
            # client_factory injetavel pra teste (SimulatedMT5Client), mesmo
            # padrao ja usado por PendingOrderExecutor/test_pending_orders.py
            # -- em producao, sempre o MT5Client real (default).
            client_factory=client_factory or MT5Client,
            news_service=MarketNewsService(
                config.database_path,
                minutes_before=config.market_news_minutes_before,
                minutes_after=config.market_news_minutes_after,
                enabled=config.market_news_enabled,
            ),
        )

    def close(self) -> None:
        self.executor.close()

    async def execute(self, job: dict[str, Any]) -> ExecutionOutcome:
        payload = job.get("payload") or {}
        try:
            signal = _signal_from_payload(payload)
            user_id = int(payload["local_user_id"])
            account_id = int(payload["local_account_id"])
        except (KeyError, ValueError, TypeError) as exc:
            raise ExecutionBackendError(f"payload invalido: {exc}") from exc

        if account_id not in self.pilot_account_ids:
            # Job reivindicado que nao pertence a uma conta piloto configurada
            # -- nunca executa. Provavelmente um espelho informativo de uma
            # execucao local real (Etapa 2/5a) que o claim pegou por nao haver
            # como o RPC remoto distinguir as duas origens. Falha explicita em
            # vez de silenciosa, pra aparecer no log/monitoramento.
            raise ExecutionBackendError(
                f"conta {account_id} nao esta em QUEUE_PILOT_ACCOUNT_IDS -- job recusado, nunca executado"
            )

        account = self.accounts.get_account(user_id, account_id)
        profile = self.accounts.get_execution_profile(user_id, account_id)
        if profile is None:
            raise ExecutionBackendError(f"perfil de execucao nao encontrado account_id={account_id}")

        result = await asyncio.to_thread(self.executor.execute_for_account, signal, account, profile)

        if result.group_result.duplicate:
            # Ja foi executado antes (dedupe local, mesma janela de
            # DUPLICATE_WINDOW_MINUTES do caminho normal) -- nao e erro,
            # nao e rejeicao de regra de negocio, so nao ha nada novo a
            # reportar.
            return ExecutionOutcome(status="succeeded", result={"mode": "demo_execution", "duplicate": True})

        if result.group_result.rejected_reason is not None:
            return ExecutionOutcome(
                status="rejected",
                result={
                    "mode": "demo_execution",
                    "rejection_code": result.group_result.rejected_reason,
                    "message": result.message,
                },
            )

        group = result.group_result.group
        assert group is not None  # sucesso sem duplicate/rejeicao sempre tem grupo
        # Reconsulta as ordens no banco local -- o objeto que execute_for_account
        # devolve fica congelado ANTES do order_send (mesma licao da Etapa 2:
        # nunca confiar no objeto em memoria pra ticket/retcode reais).
        orders = self.executor.repository.orders_for_group(group.id)
        order_dicts = [
            {
                "tp_index": order.tp_index,
                "execution_key": build_execution_key(group.signal_id, order.tp_index),
                "requested_volume": decimal_to_text(order.requested_volume),
                "normalized_volume": decimal_to_text(order.normalized_volume),
                "entry_price": decimal_to_text(order.entry_price),
                "stop_loss": decimal_to_text(order.stop_loss),
                "take_profit": decimal_to_text(order.take_profit),
                "status": "sent" if order.mt5_order_ticket else "failed",
                "mt5_order_ticket": order.mt5_order_ticket,
                "mt5_position_ticket": order.mt5_position_ticket,
                "retcode": order.broker_retcode,
                "retcode_message": order.broker_message,
            }
            for order in orders
        ]
        return ExecutionOutcome(
            status="succeeded",
            result={"mode": "demo_execution", "group_id": group.id},
            orders=order_dicts,
        )


def _signal_from_payload(payload: dict[str, Any]) -> TradeSignal:
    return TradeSignal(
        symbol=payload["symbol"],
        direction=Direction(payload["direction"]),
        entry_low=Decimal(payload["entry_low"]),
        entry_high=Decimal(payload["entry_high"]),
        stop_loss=Decimal(payload["stop_loss"]),
        take_profits=tuple(Decimal(value) for value in payload["take_profits"]),
        raw_text=payload.get("raw_text", ""),
        source_chat_id=payload.get("source_chat_id"),
        source_message_id=payload.get("source_message_id"),
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
            outcome = await self.backend.execute(job)
            await self.client.complete_execution_job(
                job_id, reservation_token, status=outcome.status, result=outcome.result, orders=outcome.orders
            )
            self.logger.info("execution_agent_job_%s id=%s", outcome.status, job_id)
        except Exception as exc:
            # Qualquer falha aqui (start/execute/complete) nunca derruba o
            # loop -- mesma disciplina de run_central_sync_drain_loop.
            # fail_execution_job (nao complete com status='rejected') porque
            # isso e sempre falha TECNICA -- uma rejeicao de regra de
            # negocio (spread/risco/noticia) ja volta como
            # ExecutionOutcome(status="rejected", ...) do backend, nunca
            # como excecao.
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
    if config.execution_agent_mode == "demo_execution":
        backend: SimulationExecutionBackend | RealExecutionBackend = RealExecutionBackend(config)
    else:
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
        logger.info("execution_agent_started modo=%s", config.execution_agent_mode)
        await agent.run_forever()
        return 0
    finally:
        await client.close()
        await auth.close()
        if isinstance(backend, RealExecutionBackend):
            backend.close()


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
