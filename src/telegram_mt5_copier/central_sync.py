"""Etapa 2: shadow-write do listener para o backend central (Supabase, schema
"portal"). So sinais/revisoes + o minimo de registro (nodes/instances/channels)
necessario pra sustentar isso -- nao inclui execution_jobs (etapa propria,
futura). Nao afeta o caminho real de processamento: falha aqui nunca propaga
pra quem chama (SignalProcessor.process), e a fila local em SQLite garante que
uma indisponibilidade do Supabase nunca perde um evento, so atrasa.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
import json
import logging
from pathlib import Path
from typing import Any

from .config import AppConfig
from .database import (
    CENTRAL_SYNC_SERVICE_NAME,
    as_text,
    connect_database,
    update_service_heartbeat,
    utc_now,
)
from .models import TradeSignal, decimal_to_text
from .mt5.models import ExecutionGroup, ExecutionOrder, MT5Account

try:
    import asyncpg
except ImportError:  # pragma: no cover - so acontece se a dependencia nao foi instalada
    asyncpg = None  # type: ignore[assignment]

OUTBOX_KIND_SIGNAL_SHADOW_WRITE = "signal_shadow_write"
OUTBOX_KIND_EXECUTION_JOB_SHADOW_WRITE = "execution_job_shadow_write"
_BACKOFF_SECONDS = (1, 2, 5, 10, 30, 60, 120, 300)


@dataclass(frozen=True)
class ChannelRow:
    id: int
    telegram_chat_id: str | None
    title: str
    status: str
    access_status: str


@dataclass(frozen=True)
class OutboxRow:
    id: int
    kind: str
    payload: dict[str, Any]
    attempts: int


def resolve_local_channel(database_path: Path, source_chat_id: int | str | None) -> ChannelRow | None:
    if source_chat_id is None:
        return None
    with connect_database(database_path) as connection:
        cursor = connection.execute(
            """
            SELECT id, telegram_chat_id, title, status, access_status
            FROM source_channels
            WHERE telegram_chat_id = ?
            """,
            (as_text(source_chat_id),),
        )
        try:
            row = cursor.fetchone()
        finally:
            cursor.close()
    if row is None:
        return None
    return ChannelRow(id=row[0], telegram_chat_id=row[1], title=row[2], status=row[3], access_status=row[4])


@dataclass(frozen=True)
class CustomerAccountRow:
    """Dados locais minimos (cliente + conta MT5) necessarios para popular
    portal.customers/portal.mt5_accounts -- nunca inclui senha nem login
    completo (so os 4 ultimos digitos, ja truncados aqui)."""

    source_user_id: int
    telegram_user_id: int | None
    user_status: str
    user_created_at: str
    customer_name: str | None
    email: str | None
    phone: str | None
    plan_name: str
    monthly_amount: str
    due_date: str | None
    billing_status: str
    last_paid_at: str | None
    source_account_id: int
    broker_name: str
    server_name: str
    login_last4: str = field(repr=False)
    account_alias: str
    account_type: str
    account_mode: str
    connection_status: str
    last_error: str | None
    balance: str | None
    equity: str | None
    worker_heartbeat_at: str | None
    account_created_at: str


def resolve_local_customer_and_account(database_path: Path, account_id: int) -> CustomerAccountRow | None:
    with connect_database(database_path) as connection:
        cursor = connection.execute(
            """
            SELECT u.id, u.telegram_user_id, u.status, u.created_at,
                   b.customer_name, b.email, b.phone, b.plan_name, b.monthly_amount,
                   b.due_date, b.billing_status, b.last_paid_at,
                   a.id, a.broker_name, a.server_name, a.login, a.account_alias,
                   a.account_type, a.account_mode, a.connection_status, a.last_error,
                   a.balance, a.equity, a.worker_heartbeat_at, a.created_at
            FROM mt5_accounts a
            JOIN users u ON u.id = a.user_id
            JOIN customer_billing b ON b.user_id = u.id
            WHERE a.id = ?
            """,
            (account_id,),
        )
        try:
            row = cursor.fetchone()
        finally:
            cursor.close()
    if row is None:
        return None
    login = str(row[15])
    return CustomerAccountRow(
        source_user_id=row[0],
        telegram_user_id=row[1],
        user_status=row[2],
        user_created_at=row[3],
        customer_name=row[4],
        email=row[5],
        phone=row[6],
        plan_name=row[7],
        monthly_amount=row[8],
        due_date=row[9],
        billing_status=row[10],
        last_paid_at=row[11],
        source_account_id=row[12],
        broker_name=row[13],
        server_name=row[14],
        login_last4=login[-4:] if len(login) > 4 else login,
        account_alias=row[16],
        account_type=row[17],
        account_mode=row[18],
        connection_status=row[19],
        last_error=row[20],
        balance=row[21],
        equity=row[22],
        worker_heartbeat_at=row[23],
        account_created_at=row[24],
    )


def build_execution_key(group_signal_id: str, tp_index: int) -> str:
    # Mesmo prefixo (8 chars hex) que mt5/trade_comment.py grava no comentario
    # real da ordem no MT5 -- formato compativel com parse_trade_comment(), pra
    # a Etapa 4 conseguir cruzar o execution_key com o que o terminal mostra.
    return f"{group_signal_id[:8].lower()}T{tp_index}"


def build_outbox_payload(
    signal: TradeSignal,
    formatted_message: str,
    channel: ChannelRow,
) -> dict[str, Any]:
    return {
        "source_channel_id": channel.id,
        "telegram_chat_id": channel.telegram_chat_id,
        "channel_title": channel.title,
        "channel_status": channel.status,
        "channel_access_status": channel.access_status,
        "source_message_id": as_text(signal.source_message_id),
        "content_signature": signal.content_signature,
        "symbol": signal.symbol,
        "direction": signal.direction.value,
        "entry_low": decimal_to_text(signal.entry_low),
        "entry_high": decimal_to_text(signal.entry_high),
        "stop_loss": decimal_to_text(signal.stop_loss),
        "take_profits": [decimal_to_text(value) for value in signal.take_profits],
        "raw_text": signal.raw_text,
        "formatted_message": formatted_message,
        "received_at": utc_now(),
    }


def build_execution_job_outbox_payload(
    signal: TradeSignal,
    channel: ChannelRow,
    customer_account: CustomerAccountRow,
    group: ExecutionGroup,
    orders: tuple[ExecutionOrder, ...],
    *,
    rejected_reason: str | None,
) -> dict[str, Any]:
    return {
        "source_channel_id": channel.id,
        "telegram_chat_id": channel.telegram_chat_id,
        "channel_title": channel.title,
        "channel_status": channel.status,
        "channel_access_status": channel.access_status,
        "source_message_id": as_text(signal.source_message_id),
        "content_signature": signal.content_signature,
        "customer": {
            "source_user_id": customer_account.source_user_id,
            "telegram_user_id": customer_account.telegram_user_id,
            "status": customer_account.user_status,
            "created_at": customer_account.user_created_at,
            "customer_name": customer_account.customer_name,
            "email": customer_account.email,
            "phone": customer_account.phone,
            "plan_name": customer_account.plan_name,
            "monthly_amount": customer_account.monthly_amount,
            "due_date": customer_account.due_date,
            "billing_status": customer_account.billing_status,
            "last_paid_at": customer_account.last_paid_at,
        },
        "account": {
            "source_account_id": customer_account.source_account_id,
            "broker_name": customer_account.broker_name,
            "server_name": customer_account.server_name,
            "login_last4": customer_account.login_last4,
            "account_alias": customer_account.account_alias,
            "account_type": customer_account.account_type,
            "account_mode": customer_account.account_mode,
            "connection_status": customer_account.connection_status,
            "last_error": customer_account.last_error,
            "balance": customer_account.balance,
            "equity": customer_account.equity,
            "worker_heartbeat_at": customer_account.worker_heartbeat_at,
            "created_at": customer_account.account_created_at,
        },
        "status": "rejected" if rejected_reason is not None else "succeeded",
        "last_error_code": rejected_reason,
        "last_error_message": rejected_reason,
        "orders": [
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
        ],
    }


class CentralSyncOutbox:
    """Fila local (SQLite) de eventos a replicar no Supabase. Escrita sincrona,
    barata (mesmo arquivo/conexao do banco de sinais) -- drenada de forma
    assincrona por run_central_sync_drain_loop."""

    def __init__(self, database_path: Path, *, logger: logging.Logger | None = None) -> None:
        self.database_path = database_path
        self.logger = logger

    def ensure_activation_baseline(self) -> None:
        """Grava, uma unica vez, o signals.id mais alto que ja existia quando o
        shadow-write comecou a rodar nesta instalacao -- e o corte usado por
        operational_health.py pra nao alertar sobre historico anterior a
        Etapa 2. Idempotente: chamadas seguintes sao no-op."""
        with connect_database(self.database_path) as connection:
            row = connection.execute(
                "SELECT activation_signal_id FROM central_sync_activation WHERE id = 1"
            ).fetchone()
            if row is not None:
                return
            baseline_row = connection.execute("SELECT COALESCE(MAX(id), 0) FROM signals").fetchone()
            baseline = int(baseline_row[0])
            connection.execute(
                """
                INSERT INTO central_sync_activation (id, activation_signal_id, activated_at)
                VALUES (1, ?, ?)
                """,
                (baseline, utc_now()),
            ).close()

    def enqueue_signal_shadow_write(
        self, signal: TradeSignal, formatted_message: str, local_signal_id: int
    ) -> None:
        if signal.source_message_id is None:
            if self.logger is not None:
                self.logger.info(
                    "central_sync_enqueue_skipped: sinal local_id=%s sem source_message_id",
                    local_signal_id,
                )
            return
        channel = resolve_local_channel(self.database_path, signal.source_chat_id)
        if channel is None:
            # Canal nao registrado localmente (hoje so acontece em testes
            # unitarios que criam SignalProcessor sem passar pelo startup real,
            # onde register_configured_channel ja roda) -- nada a replicar.
            if self.logger is not None:
                self.logger.info(
                    "central_sync_enqueue_skipped: canal nao registrado para sinal local_id=%s chat_id=%s",
                    local_signal_id,
                    signal.source_chat_id,
                )
            return
        payload = build_outbox_payload(signal, formatted_message, channel)
        now = utc_now()
        with connect_database(self.database_path) as connection:
            cursor = connection.execute(
                """
                INSERT INTO central_sync_outbox (
                    kind, source_signal_id, payload, status, attempts, created_at, updated_at, next_attempt_at
                )
                VALUES (?, ?, ?, 'pending', 0, ?, ?, ?)
                """,
                (OUTBOX_KIND_SIGNAL_SHADOW_WRITE, local_signal_id, json.dumps(payload), now, now, now),
            )
            cursor.close()

    def enqueue_execution_job_shadow_write(
        self,
        signal: TradeSignal,
        account: MT5Account,
        group: ExecutionGroup,
        orders: tuple[ExecutionOrder, ...],
        *,
        rejected_reason: str | None,
        local_group_id: int,
    ) -> None:
        channel = resolve_local_channel(self.database_path, signal.source_chat_id)
        if channel is None:
            if self.logger is not None:
                self.logger.info(
                    "central_sync_execution_enqueue_skipped: canal nao registrado group_id=%s",
                    local_group_id,
                )
            return
        customer_account = resolve_local_customer_and_account(self.database_path, account.id)
        if customer_account is None:
            if self.logger is not None:
                self.logger.info(
                    "central_sync_execution_enqueue_skipped: conta/cliente local nao encontrado group_id=%s account_id=%s",
                    local_group_id,
                    account.id,
                )
            return
        payload = build_execution_job_outbox_payload(
            signal, channel, customer_account, group, orders, rejected_reason=rejected_reason
        )
        now = utc_now()
        with connect_database(self.database_path) as connection:
            cursor = connection.execute(
                """
                INSERT INTO central_sync_outbox (
                    kind, source_execution_group_id, payload, status, attempts, created_at, updated_at, next_attempt_at
                )
                VALUES (?, ?, ?, 'pending', 0, ?, ?, ?)
                """,
                (
                    OUTBOX_KIND_EXECUTION_JOB_SHADOW_WRITE,
                    local_group_id,
                    json.dumps(payload),
                    now,
                    now,
                    now,
                ),
            )
            cursor.close()

    def claim_batch(self, limit: int) -> list[OutboxRow]:
        now = utc_now()
        with connect_database(self.database_path) as connection:
            cursor = connection.execute(
                """
                SELECT id, kind, payload, attempts
                FROM central_sync_outbox
                WHERE status = 'pending' OR (status = 'failed' AND next_attempt_at <= ?)
                ORDER BY id
                LIMIT ?
                """,
                (now, limit),
            )
            try:
                rows = cursor.fetchall()
            finally:
                cursor.close()
        return [
            OutboxRow(id=row[0], kind=row[1], payload=json.loads(row[2]), attempts=row[3])
            for row in rows
        ]

    def mark_done(self, row_id: int) -> None:
        now = utc_now()
        with connect_database(self.database_path) as connection:
            cursor = connection.execute(
                "UPDATE central_sync_outbox SET status = 'done', updated_at = ? WHERE id = ?",
                (now, row_id),
            )
            cursor.close()

    def mark_failed(self, row_id: int, error: str) -> None:
        now = utc_now()
        with connect_database(self.database_path) as connection:
            cursor = connection.execute(
                "SELECT attempts FROM central_sync_outbox WHERE id = ?",
                (row_id,),
            )
            try:
                row = cursor.fetchone()
            finally:
                cursor.close()
            attempts = (row[0] if row else 0) + 1
            delay_seconds = _BACKOFF_SECONDS[min(attempts - 1, len(_BACKOFF_SECONDS) - 1)]
            next_attempt_at = (
                datetime.now(tz=timezone.utc) + timedelta(seconds=delay_seconds)
            ).isoformat()
            cursor = connection.execute(
                """
                UPDATE central_sync_outbox
                SET status = 'failed', attempts = ?, last_error = ?, updated_at = ?, next_attempt_at = ?
                WHERE id = ?
                """,
                (attempts, error[:2000], now, next_attempt_at, row_id),
            )
            cursor.close()


def _parse_date(value: str | None) -> date | None:
    # asyncpg exige um datetime.date de verdade pra um parametro com destino
    # "date" (o cast ::date no SQL faz o Postgres reportar esse tipo na fase
    # de Parse, e o codec binario do asyncpg rejeita string nesse caso) --
    # os valores locais vem como texto ISO do SQLite.
    if not value:
        return None
    return date.fromisoformat(value[:10])


def _parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value)


def _parse_int(value: str | int | None) -> int | None:
    # Mesmo motivo de _parse_date/_parse_timestamp: um parametro com destino
    # bigint/integer no SQL (mt5_order_ticket, retcode) precisa de um int de
    # verdade -- os valores locais vem como texto (mt5_order_ticket e
    # broker_retcode sao TEXT no SQLite local).
    if value is None or value == "":
        return None
    return int(value)


class CentralSyncClient:
    """Conexao direta a Postgres (asyncpg) contra o schema "portal" -- esse
    schema deliberadamente NAO e exposto pela API REST do Supabase (ver
    supabase/config.toml [api].schemas), entao nao ha como falar com ele via
    PostgREST/supabase-py, so via conexao Postgres com privilegio de escrita."""

    def __init__(self, database_url: str) -> None:
        self.database_url = database_url
        self._pool: Any = None

    @property
    def connected(self) -> bool:
        return self._pool is not None

    async def connect(self) -> None:
        if asyncpg is None:
            raise RuntimeError(
                "asyncpg nao instalado -- necessario para CENTRAL_SYNC_ENABLED=true."
            )
        self._pool = await asyncpg.create_pool(self.database_url, min_size=1, max_size=2)

    async def reset(self) -> None:
        pool, self._pool = self._pool, None
        if pool is not None:
            await pool.close()

    async def close(self) -> None:
        await self.reset()

    async def upsert_node(self, node_id: str, node_label: str) -> None:
        await self._pool.execute(
            """
            insert into portal.nodes (id, label)
            values ($1, $2)
            on conflict (id) do update set label = excluded.label
            """,
            node_id,
            node_label or node_id,
        )

    async def upsert_instance(self, instance_id: str, brand_name: str, *, node_id: str) -> None:
        await self._pool.execute(
            """
            insert into portal.instances (id, brand_name, node_id)
            values ($1, $2, $3)
            on conflict (id) do update set
                brand_name = excluded.brand_name,
                node_id = excluded.node_id
            """,
            instance_id,
            brand_name,
            node_id,
        )

    async def upsert_channel(self, instance_id: str, payload: dict[str, Any]) -> Any:
        row = await self._pool.fetchrow(
            """
            insert into portal.channels (
                instance_id, source_channel_id, telegram_chat_id, title, status, access_status, created_at
            ) values ($1, $2, $3, $4, $5, $6, now())
            on conflict (instance_id, source_channel_id) do update set
                telegram_chat_id = excluded.telegram_chat_id,
                title = excluded.title,
                status = excluded.status,
                access_status = excluded.access_status
            returning id
            """,
            instance_id,
            payload["source_channel_id"],
            payload["telegram_chat_id"],
            payload["channel_title"],
            payload["channel_status"],
            payload["channel_access_status"],
        )
        return row["id"]

    async def upsert_signal(self, instance_id: str, channel_id: Any, payload: dict[str, Any]) -> Any:
        source_message_id = int(payload["source_message_id"])
        row = await self._pool.fetchrow(
            """
            insert into portal.signals (
                instance_id, channel_id, source_message_id, content_signature,
                symbol, direction, entry_low, entry_high, stop_loss, take_profits
            ) values ($1, $2, $3, $4, $5, $6, $7::numeric, $8::numeric, $9::numeric, $10::jsonb)
            on conflict (instance_id, channel_id, source_message_id) do nothing
            returning id
            """,
            instance_id,
            channel_id,
            source_message_id,
            payload["content_signature"],
            payload["symbol"],
            payload["direction"],
            payload["entry_low"],
            payload["entry_high"],
            payload["stop_loss"],
            json.dumps(payload["take_profits"]),
        )
        if row is not None:
            return row["id"]
        row = await self._pool.fetchrow(
            """
            select id from portal.signals
            where instance_id = $1 and channel_id = $2 and source_message_id = $3
            """,
            instance_id,
            channel_id,
            source_message_id,
        )
        return row["id"]

    async def append_signal_revision(self, signal_id: Any, content_signature: str, raw_payload: dict[str, Any]) -> None:
        await self._pool.execute(
            "select portal.append_signal_revision($1, $2, $3::jsonb)",
            signal_id,
            content_signature,
            json.dumps(raw_payload),
        )

    async def find_signal_id(self, instance_id: str, channel_id: Any, source_message_id: str) -> Any:
        row = await self._pool.fetchrow(
            """
            select id from portal.signals
            where instance_id = $1 and channel_id = $2 and source_message_id = $3
            """,
            instance_id,
            channel_id,
            int(source_message_id),
        )
        return row["id"] if row is not None else None

    async def upsert_customer(self, instance_id: str, customer: dict[str, Any]) -> Any:
        row = await self._pool.fetchrow(
            """
            insert into portal.customers (
                instance_id, source_user_id, telegram_user_id, status, customer_name, email, phone,
                plan_name, monthly_amount, due_date, billing_status, last_paid_at, created_at
            ) values ($1, $2, $3, $4, $5, $6, $7, $8, $9::numeric, $10::date, $11, $12::date, $13::timestamptz)
            on conflict (instance_id, source_user_id) do update set
                telegram_user_id = excluded.telegram_user_id,
                status = excluded.status,
                customer_name = excluded.customer_name,
                email = excluded.email,
                phone = excluded.phone,
                plan_name = excluded.plan_name,
                monthly_amount = excluded.monthly_amount,
                due_date = excluded.due_date,
                billing_status = excluded.billing_status,
                last_paid_at = excluded.last_paid_at
            returning id
            """,
            instance_id,
            customer["source_user_id"],
            customer["telegram_user_id"],
            customer["status"],
            customer["customer_name"],
            customer["email"],
            customer["phone"],
            customer["plan_name"],
            customer["monthly_amount"],
            _parse_date(customer["due_date"]),
            customer["billing_status"],
            _parse_date(customer["last_paid_at"]),
            _parse_timestamp(customer["created_at"]),
        )
        return row["id"]

    async def upsert_account(self, instance_id: str, customer_id: Any, account: dict[str, Any], *, node_id: str) -> Any:
        row = await self._pool.fetchrow(
            """
            insert into portal.mt5_accounts (
                customer_id, instance_id, source_account_id, node_id, broker_name, server_name,
                login_last4, account_alias, account_type, account_mode, connection_status, last_error,
                balance, equity, worker_heartbeat_at, created_at
            ) values ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13::numeric, $14::numeric, $15::timestamptz, $16::timestamptz)
            on conflict (instance_id, source_account_id) do update set
                customer_id = excluded.customer_id,
                node_id = excluded.node_id,
                broker_name = excluded.broker_name,
                server_name = excluded.server_name,
                login_last4 = excluded.login_last4,
                account_alias = excluded.account_alias,
                account_type = excluded.account_type,
                account_mode = excluded.account_mode,
                connection_status = excluded.connection_status,
                last_error = excluded.last_error,
                balance = excluded.balance,
                equity = excluded.equity,
                worker_heartbeat_at = excluded.worker_heartbeat_at
            returning id
            """,
            customer_id,
            instance_id,
            account["source_account_id"],
            node_id,
            account["broker_name"],
            account["server_name"],
            account["login_last4"],
            account["account_alias"],
            account["account_type"],
            account["account_mode"],
            account["connection_status"],
            account["last_error"],
            account["balance"],
            account["equity"],
            _parse_timestamp(account["worker_heartbeat_at"]),
            _parse_timestamp(account["created_at"]),
        )
        return row["id"]

    async def upsert_execution_job(
        self,
        instance_id: str,
        mt5_account_id: Any,
        node_id: str,
        signal_id: Any,
        content_signature: str,
        status: str,
        *,
        last_error_code: str | None,
        last_error_message: str | None,
        payload: dict[str, Any],
    ) -> Any:
        row = await self._pool.fetchrow(
            """
            insert into portal.execution_jobs (
                instance_id, mt5_account_id, node_id, signal_id, content_signature, status,
                finished_at, last_error_code, last_error_message, payload
            ) values ($1, $2, $3, $4, $5, $6, now(), $7, $8, $9::jsonb)
            on conflict (mt5_account_id, signal_id) do update set
                status = excluded.status,
                finished_at = excluded.finished_at,
                last_error_code = excluded.last_error_code,
                last_error_message = excluded.last_error_message,
                payload = excluded.payload
            returning id
            """,
            instance_id,
            mt5_account_id,
            node_id,
            signal_id,
            content_signature,
            status,
            last_error_code,
            last_error_message,
            json.dumps(payload),
        )
        return row["id"]

    async def upsert_execution_job_orders(self, execution_job_id: Any, orders: list[dict[str, Any]]) -> None:
        for order in orders:
            await self._pool.execute(
                """
                insert into portal.execution_job_orders (
                    execution_job_id, tp_index, execution_key, requested_volume, normalized_volume,
                    entry_price, stop_loss, take_profit, status, mt5_order_ticket, mt5_position_ticket,
                    retcode, retcode_message, sent_at
                ) values (
                    $1, $2, $3, $4::numeric, $5::numeric, $6::numeric, $7::numeric, $8::numeric, $9,
                    $10::bigint, $11::bigint, $12::integer, $13,
                    case when $10::bigint is not null then now() else null end
                )
                on conflict (execution_job_id, tp_index) do update set
                    execution_key = excluded.execution_key,
                    requested_volume = excluded.requested_volume,
                    normalized_volume = excluded.normalized_volume,
                    entry_price = excluded.entry_price,
                    stop_loss = excluded.stop_loss,
                    take_profit = excluded.take_profit,
                    status = excluded.status,
                    mt5_order_ticket = excluded.mt5_order_ticket,
                    mt5_position_ticket = excluded.mt5_position_ticket,
                    retcode = excluded.retcode,
                    retcode_message = excluded.retcode_message,
                    sent_at = coalesce(portal.execution_job_orders.sent_at, excluded.sent_at)
                """,
                execution_job_id,
                order["tp_index"],
                order["execution_key"],
                order["requested_volume"],
                order["normalized_volume"],
                order["entry_price"],
                order["stop_loss"],
                order["take_profit"],
                order["status"],
                _parse_int(order["mt5_order_ticket"]),
                _parse_int(order["mt5_position_ticket"]),
                _parse_int(order["retcode"]),
                order["retcode_message"],
            )


async def _drain_one(client: CentralSyncClient, config: AppConfig, row: OutboxRow) -> None:
    if row.kind == OUTBOX_KIND_SIGNAL_SHADOW_WRITE:
        payload = row.payload
        channel_id = await client.upsert_channel(config.instance_id, payload)
        signal_id = await client.upsert_signal(config.instance_id, channel_id, payload)
        # payload completo (inclui symbol/direction/entry_low/entry_high/stop_loss/
        # take_profits) -- portal.append_signal_revision extrai os campos
        # estruturados daqui pra manter portal.signals atualizado a cada revisao,
        # nao so o content_signature.
        await client.append_signal_revision(signal_id, payload["content_signature"], payload)
        return

    if row.kind == OUTBOX_KIND_EXECUTION_JOB_SHADOW_WRITE:
        payload = row.payload
        customer_id = await client.upsert_customer(config.instance_id, payload["customer"])
        account_id = await client.upsert_account(
            config.instance_id, customer_id, payload["account"], node_id=config.node_id
        )
        channel_id = await client.upsert_channel(config.instance_id, payload)
        signal_id = await client.find_signal_id(config.instance_id, channel_id, payload["source_message_id"])
        if signal_id is None:
            # Sinal ainda nao drenado pro Supabase (o enqueue do sinal e o da
            # execucao sao dois itens separados do mesmo outbox) -- levanta pra
            # cair no backoff normal e ser retentado, nao um erro permanente.
            raise ValueError("sinal ainda nao replicado no Supabase -- retentando")
        job_id = await client.upsert_execution_job(
            config.instance_id,
            account_id,
            config.node_id,
            signal_id,
            payload["content_signature"],
            payload["status"],
            last_error_code=payload["last_error_code"],
            last_error_message=payload["last_error_message"],
            payload={"orders": payload["orders"]},
        )
        await client.upsert_execution_job_orders(job_id, payload["orders"])
        return

    # Nao existe outro "kind" hoje -- levanta em vez de retornar silenciosamente
    # pra nao deixar o chamador marcar a linha como "done" sem ter processado
    # nada (run_central_sync_drain_loop so chama mark_done apos _drain_one
    # terminar sem excecao).
    raise ValueError(f"tipo de outbox desconhecido: {row.kind!r}")


async def run_central_sync_drain_loop(
    outbox: CentralSyncOutbox,
    client: CentralSyncClient,
    config: AppConfig,
    logger: logging.Logger,
) -> None:
    while True:
        try:
            # Heartbeat proprio, sempre, mesmo se o resto do corpo do loop
            # falhar -- mede "o loop de drenagem esta vivo", separado de "o
            # Supabase esta alcancavel" (que a conexao abaixo pode falhar sem
            # travar o loop). Fica DENTRO do try: uma falha transitoria aqui
            # (ex.: SQLite bloqueado por outro escritor no mesmo instante) nao
            # pode matar a task inteira pro resto da vida do processo -- so
            # essa iteracao falha, o loop continua na proxima.
            await asyncio.to_thread(update_service_heartbeat, outbox.database_path, CENTRAL_SYNC_SERVICE_NAME)
            if not client.connected:
                await client.connect()
            await client.upsert_node(config.node_id, config.node_label)
            await client.upsert_instance(config.instance_id, config.brand_name, node_id=config.node_id)
            rows = await asyncio.to_thread(outbox.claim_batch, config.central_sync_max_batch)
            for row in rows:
                try:
                    await _drain_one(client, config, row)
                    await asyncio.to_thread(outbox.mark_done, row.id)
                except Exception as exc:
                    logger.warning("central_sync_row_failed id=%s erro=%s", row.id, exc)
                    await asyncio.to_thread(outbox.mark_failed, row.id, str(exc))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("central_sync_drain_loop_error: %s", exc)
            try:
                await client.reset()
            except Exception:
                pass
        await asyncio.sleep(config.central_sync_poll_seconds)
