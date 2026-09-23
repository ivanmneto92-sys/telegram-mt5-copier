"""Etapa 2: shadow-write do listener para o backend central (Supabase, schema
"portal"). So sinais/revisoes + o minimo de registro (nodes/instances/channels)
necessario pra sustentar isso -- nao inclui execution_jobs (etapa propria,
futura). Nao afeta o caminho real de processamento: falha aqui nunca propaga
pra quem chama (SignalProcessor.process), e a fila local em SQLite garante que
uma indisponibilidade do Supabase nunca perde um evento, so atrasa.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import logging
from pathlib import Path
from typing import Any

from .config import AppConfig
from .database import as_text, connect_database, utc_now
from .models import TradeSignal, decimal_to_text

try:
    import asyncpg
except ImportError:  # pragma: no cover - so acontece se a dependencia nao foi instalada
    asyncpg = None  # type: ignore[assignment]

OUTBOX_KIND_SIGNAL_SHADOW_WRITE = "signal_shadow_write"
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


class CentralSyncOutbox:
    """Fila local (SQLite) de eventos a replicar no Supabase. Escrita sincrona,
    barata (mesmo arquivo/conexao do banco de sinais) -- drenada de forma
    assincrona por run_central_sync_drain_loop."""

    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path

    def enqueue_signal_shadow_write(self, signal: TradeSignal, formatted_message: str) -> None:
        if signal.source_message_id is None:
            return
        channel = resolve_local_channel(self.database_path, signal.source_chat_id)
        if channel is None:
            # Canal nao registrado localmente (hoje so acontece em testes
            # unitarios que criam SignalProcessor sem passar pelo startup real,
            # onde register_configured_channel ja roda) -- nada a replicar.
            return
        payload = build_outbox_payload(signal, formatted_message, channel)
        now = utc_now()
        with connect_database(self.database_path) as connection:
            cursor = connection.execute(
                """
                INSERT INTO central_sync_outbox (
                    kind, payload, status, attempts, created_at, updated_at, next_attempt_at
                )
                VALUES (?, ?, 'pending', 0, ?, ?, ?)
                """,
                (OUTBOX_KIND_SIGNAL_SHADOW_WRITE, json.dumps(payload), now, now, now),
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


async def _drain_one(client: CentralSyncClient, config: AppConfig, row: OutboxRow) -> None:
    if row.kind != OUTBOX_KIND_SIGNAL_SHADOW_WRITE:
        return
    payload = row.payload
    channel_id = await client.upsert_channel(config.instance_id, payload)
    signal_id = await client.upsert_signal(config.instance_id, channel_id, payload)
    raw_payload = {
        "raw_text": payload["raw_text"],
        "formatted_message": payload["formatted_message"],
        "source_chat_id": payload["telegram_chat_id"],
        "source_message_id": payload["source_message_id"],
        "received_at": payload["received_at"],
    }
    await client.append_signal_revision(signal_id, payload["content_signature"], raw_payload)


async def run_central_sync_drain_loop(
    outbox: CentralSyncOutbox,
    client: CentralSyncClient,
    config: AppConfig,
    logger: logging.Logger,
) -> None:
    while True:
        try:
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
