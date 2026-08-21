from __future__ import annotations

from dataclasses import dataclass
import logging
from pathlib import Path
import re
from urllib.parse import urlsplit

from .database import connect_database, initialize_database, utc_now

logger = logging.getLogger(__name__)

CHANNEL_STATUS_PENDING = "pending_review"
CHANNEL_STATUS_ACTIVE = "active"
CHANNEL_STATUS_SUSPENDED = "suspended"

REQUEST_PENDING_ACCESS = "pending_access"
REQUEST_AWAITING_MEMBERSHIP = "awaiting_monitor_membership"
REQUEST_READY_REVIEW = "ready_for_admin_review"
REQUEST_APPROVED = "approved"
REQUEST_REJECTED = "rejected"

USERNAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{3,31}$")
INVITE_HASH_RE = re.compile(r"^[A-Za-z0-9_-]{8,128}$")
PRIVATE_CHAT_ID_RE = re.compile(r"^\d{5,20}$")
TOGGLE_CALLBACK_RE = re.compile(r"^v1:ch:t:(?P<channel_id>\d+)$")
DEFAULT_CHANNEL_DISPLAY_PREFIX = "Sala de Sinais"


@dataclass(frozen=True)
class NormalizedChannelLink:
    username: str | None
    normalized_key: str
    canonical_link: str


@dataclass(frozen=True)
class ChannelRequestResult:
    request_id: int | None
    channel_id: int | None
    status: str
    title: str
    canonical_link: str


class ChannelCatalogService:
    def __init__(
        self,
        database_path: Path,
        *,
        peer_database_paths: tuple[Path, ...] = (),
    ) -> None:
        self.database_path = database_path
        self.peer_database_paths = peer_database_paths
        initialize_database(database_path)

    def submit_request(self, user_id: int, raw_link: str) -> ChannelRequestResult:
        normalized = normalize_channel_link(raw_link)
        now = utc_now()
        with connect_database(self.database_path) as connection:
            channel = None
            if normalized.username:
                channel = connection.execute(
                    """
                    SELECT id, display_name, status FROM source_channels
                    WHERE username = ? COLLATE NOCASE
                    """,
                    (normalized.username,),
                ).fetchone()
            if channel is not None and str(channel[2]) == CHANNEL_STATUS_ACTIVE:
                self._enable_subscription(connection, user_id, int(channel[0]), now)
                return ChannelRequestResult(
                    None,
                    int(channel[0]),
                    REQUEST_APPROVED,
                    public_channel_title(int(channel[0]), channel[1]),
                    normalized.canonical_link,
                )
            duplicate = connection.execute(
                """
                SELECT id, source_channel_id, status FROM channel_requests
                WHERE user_id = ? AND normalized_key = ?
                  AND status NOT IN ('rejected')
                ORDER BY id DESC LIMIT 1
                """,
                (user_id, normalized.normalized_key),
            ).fetchone()
            if duplicate is not None:
                return ChannelRequestResult(
                    int(duplicate[0]),
                    int(duplicate[1]) if duplicate[1] is not None else None,
                    str(duplicate[2]),
                    channel_request_title(normalized),
                    normalized.canonical_link,
                )
            cursor = connection.execute(
                """
                INSERT INTO channel_requests (
                    user_id, source_channel_id, submitted_link, normalized_key,
                    username, canonical_link, status, admin_notes,
                    created_at, updated_at
                )
                VALUES (?, NULL, ?, ?, ?, ?, ?, NULL, ?, ?)
                """,
                (
                    user_id,
                    normalized.canonical_link,
                    normalized.normalized_key,
                    normalized.username,
                    normalized.canonical_link,
                    REQUEST_PENDING_ACCESS,
                    now,
                    now,
                ),
            )
            request_id = int(cursor.lastrowid)
            cursor.close()
        return ChannelRequestResult(
            request_id,
            None,
            REQUEST_PENDING_ACCESS,
            channel_request_title(normalized),
            normalized.canonical_link,
        )

    def user_overview(self, user_id: int) -> dict[str, object]:
        with connect_database(self.database_path) as connection:
            setting = connection.execute(
                "SELECT selection_mode FROM user_channel_settings WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            channels = connection.execute(
                """
                SELECT c.id, c.display_name, COALESCE(s.enabled, 0)
                FROM source_channels c
                LEFT JOIN user_channel_subscriptions s
                    ON s.source_channel_id = c.id AND s.user_id = ?
                WHERE c.status = 'active'
                ORDER BY c.id
                """,
                (user_id,),
            ).fetchall()
            requests = connection.execute(
                """
                SELECT id, username, canonical_link, status, created_at
                FROM channel_requests
                WHERE user_id = ? AND status != 'approved'
                ORDER BY id DESC LIMIT 8
                """,
                (user_id,),
            ).fetchall()
        return {
            "selection_mode": str(setting[0]) if setting else "custom",
            "channels": [
                {
                    "id": int(row[0]),
                    "title": public_channel_title(int(row[0]), row[1]),
                    "enabled": bool(row[2]),
                }
                for row in channels
            ],
            "requests": [
                {
                    "id": int(row[0]),
                    "username": str(row[1]) if row[1] else None,
                    "canonical_link": str(row[2]),
                    "status": str(row[3]),
                    "created_at": str(row[4]),
                }
                for row in requests
            ],
        }

    def set_selection_mode(self, user_id: int, mode: str) -> None:
        if mode not in {"all", "custom"}:
            raise ValueError("Modo de seleção de canais inválido.")
        now = utc_now()
        with connect_database(self.database_path) as connection:
            connection.execute(
                """
                INSERT INTO user_channel_settings (user_id, selection_mode, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    selection_mode = excluded.selection_mode,
                    updated_at = excluded.updated_at
                """,
                (user_id, mode, now),
            )
            if mode == "all":
                connection.execute(
                    """
                    INSERT INTO user_channel_subscriptions (
                        user_id, source_channel_id, enabled, created_at, updated_at
                    )
                    SELECT ?, id, 1, ?, ? FROM source_channels WHERE status = 'active'
                    ON CONFLICT(user_id, source_channel_id) DO UPDATE SET
                        enabled = 1,
                        updated_at = excluded.updated_at
                    """,
                    (user_id, now, now),
                )

    def toggle_subscription(self, user_id: int, channel_id: int) -> bool:
        now = utc_now()
        with connect_database(self.database_path) as connection:
            channel = connection.execute(
                "SELECT id FROM source_channels WHERE id = ? AND status = 'active'",
                (channel_id,),
            ).fetchone()
            if channel is None:
                raise ValueError("Canal indisponível.")
            self.set_selection_mode_in_connection(connection, user_id, "custom", now)
            row = connection.execute(
                """
                SELECT enabled FROM user_channel_subscriptions
                WHERE user_id = ? AND source_channel_id = ?
                """,
                (user_id, channel_id),
            ).fetchone()
            enabled = not bool(row[0]) if row is not None else True
            connection.execute(
                """
                INSERT INTO user_channel_subscriptions (
                    user_id, source_channel_id, enabled, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(user_id, source_channel_id) DO UPDATE SET
                    enabled = excluded.enabled,
                    updated_at = excluded.updated_at
                """,
                (user_id, channel_id, int(enabled), now, now),
            )
        return enabled

    def set_display_name(self, channel_id: int, display_name: str) -> str:
        normalized = normalize_channel_display_name(display_name)
        now = utc_now()
        with connect_database(self.database_path) as connection:
            row = connection.execute(
                "SELECT telegram_chat_id FROM source_channels WHERE id = ?",
                (channel_id,),
            ).fetchone()
            if row is None:
                raise ValueError("Canal não encontrado.")
            telegram_chat_id = row[0]
            connection.execute(
                "UPDATE source_channels SET display_name = ?, updated_at = ? WHERE id = ?",
                (normalized, now, channel_id),
            )
        if telegram_chat_id is not None:
            self._propagate_to_peers(
                telegram_chat_id=str(telegram_chat_id),
                display_name=normalized,
                target_status=None,
                admin_id=None,
                now=now,
            )
        return public_channel_title(channel_id, normalized)

    def set_channel_status(self, channel_id: int, status: str) -> str:
        if status not in {CHANNEL_STATUS_ACTIVE, CHANNEL_STATUS_SUSPENDED}:
            raise ValueError("Status de canal inválido.")
        now = utc_now()
        with connect_database(self.database_path) as connection:
            row = connection.execute(
                "SELECT access_status, telegram_chat_id, display_name FROM source_channels WHERE id = ?",
                (channel_id,),
            ).fetchone()
            if row is None:
                raise ValueError("Canal não encontrado.")
            if status == CHANNEL_STATUS_ACTIVE and str(row[0]) != "confirmed":
                raise ValueError("O acesso da conta de monitoramento não está confirmado.")
            telegram_chat_id, display_name = row[1], row[2]
            connection.execute(
                "UPDATE source_channels SET status = ?, updated_at = ? WHERE id = ?",
                (status, now, channel_id),
            )
        if telegram_chat_id is not None:
            self._propagate_to_peers(
                telegram_chat_id=str(telegram_chat_id),
                display_name=display_name,
                target_status=status,
                admin_id=None,
                now=now,
            )
        return status

    def register_configured_channel(
        self,
        *,
        telegram_chat_id: int | str,
        title: str,
        username: str | None,
        content_protected: bool,
        history_accessible: bool,
        last_message_id: int | str | None,
    ) -> int:
        now = utc_now()
        canonical = f"https://t.me/{username}" if username else None
        with connect_database(self.database_path) as connection:
            row = connection.execute(
                """
                SELECT id FROM source_channels
                WHERE telegram_chat_id = ?
                   OR (? IS NOT NULL AND username = ? COLLATE NOCASE)
                ORDER BY id LIMIT 1
                """,
                (str(telegram_chat_id), username, username),
            ).fetchone()
            if row is None:
                self._freeze_follow_all_modes(connection, now)
                cursor = connection.execute(
                    """
                    INSERT INTO source_channels (
                        telegram_chat_id, username, title, canonical_link,
                        status, access_status, content_protected,
                        history_accessible, last_message_id, last_error,
                        created_at, updated_at, validated_at, approved_at,
                        approved_by_admin_id
                    )
                    VALUES (?, ?, ?, ?, 'active', 'confirmed', ?, ?, ?, NULL, ?, ?, ?, ?, NULL)
                    """,
                    (
                        str(telegram_chat_id),
                        username,
                        title,
                        canonical,
                        int(content_protected),
                        int(history_accessible),
                        str(last_message_id) if last_message_id is not None else None,
                        now,
                        now,
                        now,
                        now,
                    ),
                )
                channel_id = int(cursor.lastrowid)
                cursor.close()
                return channel_id
            channel_id = int(row[0])
            connection.execute(
                """
                UPDATE source_channels SET
                    username = COALESCE(?, username),
                    title = ?, canonical_link = COALESCE(?, canonical_link),
                    status = CASE WHEN status = 'suspended' THEN status ELSE 'active' END,
                    access_status = 'confirmed',
                    content_protected = ?, history_accessible = ?,
                    last_message_id = ?, last_error = NULL,
                    updated_at = ?, validated_at = COALESCE(validated_at, ?)
                WHERE id = ?
                """,
                (
                    username,
                    title,
                    canonical,
                    int(content_protected),
                    int(history_accessible),
                    str(last_message_id) if last_message_id is not None else None,
                    now,
                    now,
                    channel_id,
                ),
            )
        return channel_id

    def pending_validations(self) -> list[dict[str, object]]:
        with connect_database(self.database_path) as connection:
            rows = connection.execute(
                """
                SELECT MIN(id), normalized_key, username, canonical_link
                FROM channel_requests
                WHERE status IN ('pending_access', 'awaiting_monitor_membership')
                GROUP BY normalized_key, username, canonical_link
                ORDER BY MIN(id)
                """
            ).fetchall()
        return [
            {
                "request_id": int(row[0]),
                "normalized_key": str(row[1]),
                "username": str(row[2]) if row[2] else None,
                "canonical_link": str(row[3]),
            }
            for row in rows
        ]

    def mark_awaiting_membership(self, normalized_key: str, error: str | None = None) -> None:
        with connect_database(self.database_path) as connection:
            connection.execute(
                """
                UPDATE channel_requests SET status = ?, updated_at = ?
                WHERE normalized_key = ? AND status IN (?, ?)
                """,
                (
                    REQUEST_AWAITING_MEMBERSHIP,
                    utc_now(),
                    normalized_key,
                    REQUEST_PENDING_ACCESS,
                    REQUEST_AWAITING_MEMBERSHIP,
                ),
            )

    def mark_access_confirmed(
        self,
        *,
        normalized_key: str,
        telegram_chat_id: int | str,
        title: str,
        username: str | None,
        canonical_link: str | None = None,
        content_protected: bool,
        history_accessible: bool,
        last_message_id: int | str | None,
    ) -> int:
        now = utc_now()
        canonical = canonical_link or (f"https://t.me/{username}" if username else None)
        with connect_database(self.database_path) as connection:
            row = connection.execute(
                """
                SELECT id FROM source_channels
                WHERE telegram_chat_id = ?
                   OR (? IS NOT NULL AND username = ? COLLATE NOCASE)
                ORDER BY id LIMIT 1
                """,
                (str(telegram_chat_id), username, username),
            ).fetchone()
            if row is None:
                self._freeze_follow_all_modes(connection, now)
                cursor = connection.execute(
                    """
                    INSERT INTO source_channels (
                        telegram_chat_id, username, title, canonical_link,
                        status, access_status, content_protected,
                        history_accessible, last_message_id, last_error,
                        created_at, updated_at, validated_at
                    )
                    VALUES (?, ?, ?, ?, 'pending_review', 'confirmed', ?, ?, ?, NULL, ?, ?, ?)
                    """,
                    (
                        str(telegram_chat_id),
                        username,
                        title,
                        canonical,
                        int(content_protected),
                        int(history_accessible),
                        str(last_message_id) if last_message_id is not None else None,
                        now,
                        now,
                        now,
                    ),
                )
                channel_id = int(cursor.lastrowid)
                cursor.close()
            else:
                channel_id = int(row[0])
                connection.execute(
                    """
                    UPDATE source_channels SET telegram_chat_id = ?, username = ?,
                        title = ?, canonical_link = COALESCE(?, canonical_link),
                        access_status = 'confirmed', content_protected = ?,
                        history_accessible = ?, last_message_id = ?,
                        last_error = NULL, updated_at = ?, validated_at = ?
                    WHERE id = ?
                    """,
                    (
                        str(telegram_chat_id),
                        username,
                        title,
                        canonical,
                        int(content_protected),
                        int(history_accessible),
                        str(last_message_id) if last_message_id is not None else None,
                        now,
                        now,
                        channel_id,
                    ),
                )
            connection.execute(
                """
                UPDATE channel_requests SET source_channel_id = ?, status = ?, updated_at = ?
                WHERE normalized_key = ? AND status IN (?, ?)
                """,
                (
                    channel_id,
                    REQUEST_READY_REVIEW,
                    now,
                    normalized_key,
                    REQUEST_PENDING_ACCESS,
                    REQUEST_AWAITING_MEMBERSHIP,
                ),
            )
        return channel_id

    def admin_channels(self) -> dict[str, object]:
        with connect_database(self.database_path) as connection:
            requests = connection.execute(
                """
                SELECT r.id, r.user_id, u.telegram_user_id, u.telegram_username,
                       r.username, r.canonical_link, r.status, r.source_channel_id,
                       c.title, c.telegram_chat_id, c.access_status,
                       c.content_protected, c.history_accessible, r.created_at
                FROM channel_requests r
                JOIN users u ON u.id = r.user_id
                LEFT JOIN source_channels c ON c.id = r.source_channel_id
                WHERE r.status NOT IN ('approved', 'rejected')
                ORDER BY r.id DESC
                """
            ).fetchall()
            channels = connection.execute(
                """
                SELECT c.id, c.telegram_chat_id, c.username, c.title, c.display_name, c.status,
                       c.access_status, c.content_protected, c.history_accessible,
                       c.last_message_id,
                       (SELECT COUNT(*) FROM user_channel_subscriptions s
                        WHERE s.source_channel_id = c.id AND s.enabled = 1)
                FROM source_channels c ORDER BY c.title COLLATE NOCASE
                """
            ).fetchall()
        return {
            "requests": [
                {
                    "id": int(r[0]),
                    "user_id": int(r[1]),
                    "telegram_user_id": int(r[2]),
                    "telegram_username": str(r[3]) if r[3] else None,
                    "username": str(r[4]) if r[4] else None,
                    "canonical_link": str(r[5]),
                    "status": str(r[6]),
                    "source_channel_id": int(r[7]) if r[7] is not None else None,
                    "title": str(r[8]) if r[8] else None,
                    "telegram_chat_id": str(r[9]) if r[9] else None,
                    "access_status": str(r[10]) if r[10] else None,
                    "content_protected": bool(r[11]) if r[11] is not None else None,
                    "history_accessible": bool(r[12]) if r[12] is not None else None,
                    "created_at": str(r[13]),
                }
                for r in requests
            ],
            "channels": [
                {
                    "id": int(c[0]),
                    "telegram_chat_id": str(c[1]) if c[1] else None,
                    "username": str(c[2]) if c[2] else None,
                    "title": str(c[3]),
                    "display_name": public_channel_title(int(c[0]), c[4]),
                    "status": str(c[5]),
                    "access_status": str(c[6]),
                    "content_protected": bool(c[7]),
                    "history_accessible": bool(c[8]),
                    "last_message_id": str(c[9]) if c[9] else None,
                    "subscriber_count": int(c[10] or 0),
                }
                for c in channels
            ],
        }

    def approve_request(self, request_id: int, admin_id: int) -> int:
        now = utc_now()
        with connect_database(self.database_path) as connection:
            request_row = connection.execute(
                """
                SELECT normalized_key, source_channel_id FROM channel_requests
                WHERE id = ?
                """,
                (request_id,),
            ).fetchone()
            if request_row is None:
                raise ValueError("Solicitação de canal não encontrada.")
            if request_row[1] is None:
                raise ValueError("A conta de monitoramento ainda não confirmou acesso ao canal.")
            channel_id = int(request_row[1])
            channel = connection.execute(
                "SELECT access_status FROM source_channels WHERE id = ?",
                (channel_id,),
            ).fetchone()
            if channel is None or str(channel[0]) != "confirmed":
                raise ValueError("O acesso da conta de monitoramento ainda não foi confirmado.")
            connection.execute(
                """
                UPDATE source_channels SET status = 'active', approved_at = ?,
                    approved_by_admin_id = ?, updated_at = ?
                WHERE id = ?
                """,
                (now, admin_id, now, channel_id),
            )
            channel_identity = connection.execute(
                "SELECT telegram_chat_id, display_name FROM source_channels WHERE id = ?",
                (channel_id,),
            ).fetchone()
            requesters = connection.execute(
                """
                SELECT DISTINCT user_id FROM channel_requests
                WHERE normalized_key = ? AND status IN (?, ?, ?)
                """,
                (
                    str(request_row[0]),
                    REQUEST_PENDING_ACCESS,
                    REQUEST_AWAITING_MEMBERSHIP,
                    REQUEST_READY_REVIEW,
                ),
            ).fetchall()
            connection.execute(
                """
                UPDATE channel_requests SET source_channel_id = ?, status = ?, updated_at = ?
                WHERE normalized_key = ? AND status IN (?, ?, ?)
                """,
                (
                    channel_id,
                    REQUEST_APPROVED,
                    now,
                    str(request_row[0]),
                    REQUEST_PENDING_ACCESS,
                    REQUEST_AWAITING_MEMBERSHIP,
                    REQUEST_READY_REVIEW,
                ),
            )
            for requester in requesters:
                self._enable_subscription(connection, int(requester[0]), channel_id, now)
        if channel_identity is not None and channel_identity[0] is not None:
            self._propagate_to_peers(
                telegram_chat_id=str(channel_identity[0]),
                display_name=channel_identity[1],
                target_status=CHANNEL_STATUS_ACTIVE,
                admin_id=admin_id,
                now=now,
            )
        return channel_id

    def reject_request(self, request_id: int, notes: str = "") -> None:
        with connect_database(self.database_path) as connection:
            cursor = connection.execute(
                """
                UPDATE channel_requests SET status = ?, admin_notes = ?, updated_at = ?
                WHERE id = ?
                """,
                (REQUEST_REJECTED, notes[:500] or None, utc_now(), request_id),
            )
            changed = cursor.rowcount
            cursor.close()
        if changed != 1:
            raise ValueError("Solicitação de canal não encontrada.")

    def request_revalidation(self, request_id: int) -> None:
        with connect_database(self.database_path) as connection:
            cursor = connection.execute(
                """
                UPDATE channel_requests SET status = ?, updated_at = ?
                WHERE id = ? AND status NOT IN ('approved', 'rejected')
                """,
                (REQUEST_PENDING_ACCESS, utc_now(), request_id),
            )
            changed = cursor.rowcount
            cursor.close()
        if changed != 1:
            raise ValueError("Solicitação indisponível para nova verificação.")

    def is_active_chat(self, telegram_chat_id: int | str | None) -> bool:
        if telegram_chat_id is None:
            return False
        with connect_database(self.database_path) as connection:
            row = connection.execute(
                """
                SELECT 1 FROM source_channels
                WHERE telegram_chat_id = ? AND status = 'active'
                """,
                (str(telegram_chat_id),),
            ).fetchone()
        return row is not None

    @staticmethod
    def set_selection_mode_in_connection(
        connection: object,
        user_id: int,
        mode: str,
        now: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO user_channel_settings (user_id, selection_mode, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                selection_mode = excluded.selection_mode,
                updated_at = excluded.updated_at
            """,
            (user_id, mode, now),
        )

    @staticmethod
    def _freeze_follow_all_modes(connection: object, now: str) -> None:
        """Um canal novo nunca herda automaticamente assinantes antigos."""
        connection.execute(
            """
            UPDATE user_channel_settings
            SET selection_mode = 'custom', updated_at = ?
            WHERE selection_mode = 'all'
            """,
            (now,),
        )

    def _propagate_to_peers(
        self,
        *,
        telegram_chat_id: str,
        display_name: object | None,
        target_status: str | None,
        admin_id: int | None,
        now: str,
    ) -> list[dict[str, object]]:
        """Replica apelido e status de aprovação para instâncias white-label irmãs.

        Casa o canal pelo `telegram_chat_id` real do Telegram, que é o mesmo em
        todas as marcas. Nunca cria um canal na instância peer: só atualiza um
        canal que a conta técnica daquela instância já conhece. Uma ativação só
        é propagada quando o peer já confirmou o próprio acesso ao canal; caso
        contrário, apenas o apelido é sincronizado e o status fica para o admin
        confirmar manualmente naquela instância. Falhas de propagação nunca
        interrompem a ação local (a instância de origem já foi salva).

        Retorna um resultado por instância peer, usado pelo relatório de
        `resync_active_channels_with_peers`; chamadas do dia a dia (aprovar,
        renomear, suspender um canal) ignoram o retorno.
        """
        outcomes: list[dict[str, object]] = []
        for peer_path in self.peer_database_paths:
            try:
                with connect_database(peer_path) as connection:
                    row = connection.execute(
                        "SELECT id, access_status FROM source_channels WHERE telegram_chat_id = ?",
                        (telegram_chat_id,),
                    ).fetchone()
                    if row is None:
                        outcomes.append({"peer": str(peer_path), "outcome": "not_found"})
                        continue
                    peer_channel_id, peer_access_status = int(row[0]), str(row[1])
                    if display_name is not None:
                        connection.execute(
                            "UPDATE source_channels SET display_name = ?, updated_at = ? WHERE id = ?",
                            (display_name, now, peer_channel_id),
                        )
                    if target_status == CHANNEL_STATUS_ACTIVE:
                        if peer_access_status == "confirmed":
                            connection.execute(
                                """
                                UPDATE source_channels SET status = 'active', approved_at = ?,
                                    approved_by_admin_id = COALESCE(?, approved_by_admin_id),
                                    updated_at = ?
                                WHERE id = ?
                                """,
                                (now, admin_id, now, peer_channel_id),
                            )
                            outcomes.append({"peer": str(peer_path), "outcome": "activated"})
                        else:
                            logger.warning(
                                "Canal %s aprovado, mas instancia peer %s ainda nao confirmou "
                                "acesso; aprovacao nao propagada, apenas o apelido.",
                                telegram_chat_id,
                                peer_path,
                            )
                            outcomes.append({"peer": str(peer_path), "outcome": "pending_confirmation"})
                    elif target_status == CHANNEL_STATUS_SUSPENDED:
                        connection.execute(
                            "UPDATE source_channels SET status = 'suspended', updated_at = ? WHERE id = ?",
                            (now, peer_channel_id),
                        )
                        outcomes.append({"peer": str(peer_path), "outcome": "suspended"})
                    else:
                        outcomes.append({"peer": str(peer_path), "outcome": "display_name_only"})
            except Exception:
                logger.exception(
                    "Falha ao propagar canal %s para a instancia peer %s",
                    telegram_chat_id,
                    peer_path,
                )
                outcomes.append({"peer": str(peer_path), "outcome": "error"})
        return outcomes

    def resync_active_channels_with_peers(self) -> list[dict[str, object]]:
        """Reconciliação única: replica o estado atual de todo canal ativo aqui.

        Uso pontual, disparado manualmente (CLI `--sync-channels`), para
        alinhar catálogos que já divergiram antes de `PEER_CHANNEL_SYNC_DATABASES`
        existir ou antes de ser configurado numa instância. Aprovações,
        renomeações e suspensões novas já propagam sozinhas; isto não precisa
        rodar de novo depois disso, exceto para varrer divergência histórica.
        """
        now = utc_now()
        with connect_database(self.database_path) as connection:
            rows = connection.execute(
                "SELECT id, telegram_chat_id, display_name FROM source_channels WHERE status = 'active'"
            ).fetchall()

        report: list[dict[str, object]] = []
        for row in rows:
            channel_id, telegram_chat_id, display_name = int(row[0]), row[1], row[2]
            if telegram_chat_id is None:
                continue
            outcomes = self._propagate_to_peers(
                telegram_chat_id=str(telegram_chat_id),
                display_name=display_name,
                target_status=CHANNEL_STATUS_ACTIVE,
                admin_id=None,
                now=now,
            )
            report.append(
                {
                    "channel_id": channel_id,
                    "display_name": public_channel_title(channel_id, display_name),
                    "telegram_chat_id": str(telegram_chat_id),
                    "peers": outcomes,
                }
            )
        return report

    @staticmethod
    def _enable_subscription(connection: object, user_id: int, channel_id: int, now: str) -> None:
        connection.execute(
            """
            INSERT INTO user_channel_subscriptions (
                user_id, source_channel_id, enabled, created_at, updated_at
            )
            VALUES (?, ?, 1, ?, ?)
            ON CONFLICT(user_id, source_channel_id) DO UPDATE SET
                enabled = 1, updated_at = excluded.updated_at
            """,
            (user_id, channel_id, now, now),
        )


def normalize_channel_link(raw_link: str) -> NormalizedChannelLink:
    value = raw_link.strip()
    if not value or len(value) > 500:
        raise ValueError("Envie um link público, privado ou @username válido.")
    username: str | None = None
    invite_hash: str | None = None
    private_chat_id: str | None = None
    if value.startswith("@"):
        username = value[1:]
    else:
        candidate = value if "://" in value else f"https://{value}"
        parsed = urlsplit(candidate)
        host = parsed.netloc.lower().split(":", 1)[0]
        if host in {"web.telegram.org"}:
            fragment = parsed.fragment
            if fragment.startswith("@"):
                username = fragment[1:].split("/", 1)[0]
        elif host in {"t.me", "www.t.me", "telegram.me", "www.telegram.me"}:
            parts = [part for part in parsed.path.split("/") if part]
            if parts and parts[0].lower() == "s":
                parts = parts[1:]
            if parts and parts[0].startswith("+"):
                invite_hash = parts[0][1:]
            elif len(parts) >= 2 and parts[0].lower() == "joinchat":
                invite_hash = parts[1]
            elif len(parts) >= 2 and parts[0].lower() == "c":
                private_chat_id = parts[1]
            elif parts:
                username = parts[0]
    if invite_hash is not None:
        invite_hash = invite_hash.split("?", 1)[0].strip()
        if not INVITE_HASH_RE.fullmatch(invite_hash):
            raise ValueError("Link de convite privado inválido.")
        return NormalizedChannelLink(
            username=None,
            normalized_key=f"invite:{invite_hash}",
            canonical_link=f"https://t.me/+{invite_hash}",
        )
    if private_chat_id is not None:
        private_chat_id = private_chat_id.split("?", 1)[0].strip()
        if not PRIVATE_CHAT_ID_RE.fullmatch(private_chat_id):
            raise ValueError("Link interno do canal privado inválido.")
        return NormalizedChannelLink(
            username=None,
            normalized_key=f"chat_id:-100{private_chat_id}",
            canonical_link=f"https://t.me/c/{private_chat_id}",
        )
    if username is None:
        raise ValueError(
            "Não reconheci o canal. Envie @username ou um link público/privado do Telegram."
        )
    username = username.split("?", 1)[0].strip().lstrip("@")
    if not USERNAME_RE.fullmatch(username):
        raise ValueError("Username público do canal inválido.")
    return NormalizedChannelLink(
        username=username,
        normalized_key=f"username:{username.lower()}",
        canonical_link=f"https://t.me/{username}",
    )


def channel_request_title(channel: NormalizedChannelLink) -> str:
    return f"@{channel.username}" if channel.username else "Canal privado"


def extract_channel_toggle(callback_data: str) -> int | None:
    match = TOGGLE_CALLBACK_RE.fullmatch(callback_data)
    return int(match.group("channel_id")) if match else None


def public_channel_title(channel_id: int, display_name: object | None) -> str:
    if display_name is not None and str(display_name).strip():
        return str(display_name).strip()
    return f"{DEFAULT_CHANNEL_DISPLAY_PREFIX} {channel_id:02d}"


def normalize_channel_display_name(value: str) -> str | None:
    normalized = " ".join(value.strip().split())
    if not normalized:
        return None
    if len(normalized) > 60:
        raise ValueError("Nome exibido deve ter no máximo 60 caracteres.")
    if any(ord(character) < 32 for character in normalized):
        raise ValueError("Nome exibido contém caracteres inválidos.")
    return normalized
