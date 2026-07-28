from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import logging
import sys
import time
from typing import Callable

from .config import AppConfig
from .database import (
    SIGNAL_MONITOR_SERVICE_NAME,
    connect_database,
    initialize_database,
    utc_now,
)
from .telegram_notifier import TelegramAdminNotifier


@dataclass(frozen=True)
class HealthIssue:
    key: str
    alert_type: str
    entity_id: str | None
    title: str
    summary: str


class OperationalHealthMonitor:
    def __init__(
        self,
        config: AppConfig,
        *,
        notifier: TelegramAdminNotifier,
        logger: logging.Logger,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.config = config
        self.notifier = notifier
        self.logger = logger
        self.now = now or (lambda: datetime.now(tz=timezone.utc))
        self.started_at = self.now()
        initialize_database(config.database_path)

    def check_once(self) -> tuple[HealthIssue, ...]:
        current_time = self.now()
        issues = {
            issue.key: issue
            for issue in (
                *self._account_issues(current_time),
                *self._signal_monitor_issues(current_time),
            )
        }
        self._synchronize(issues, current_time)
        return tuple(issues.values())

    def _account_issues(self, current_time: datetime) -> list[HealthIssue]:
        with connect_database(self.config.database_path) as connection:
            cursor = connection.execute(
                """
                SELECT a.id, a.account_alias, a.login, a.connection_status,
                       a.last_error, a.worker_heartbeat_at, a.last_connected_at,
                       a.updated_at
                FROM mt5_accounts a
                JOIN users u ON u.id = a.user_id
                JOIN execution_profiles p
                  ON p.user_id = a.user_id AND p.mt5_account_id = a.id
                WHERE u.status = 'active'
                  AND p.enabled = 1
                  AND a.id = (
                      SELECT MAX(a2.id)
                      FROM mt5_accounts a2
                      JOIN execution_profiles p2
                        ON p2.user_id = a2.user_id
                       AND p2.mt5_account_id = a2.id
                      WHERE a2.user_id = a.user_id
                        AND p2.enabled = 1
                  )
                ORDER BY a.id
                """
            )
            try:
                rows = cursor.fetchall()
            finally:
                cursor.close()

        issues: list[HealthIssue] = []
        stale_after = timedelta(seconds=self.config.health_stale_after_seconds)
        for row in rows:
            account_id = int(row[0])
            alias = str(row[1])
            masked_login = mask_login(str(row[2]))
            status = str(row[3])
            last_error = str(row[4]) if row[4] else None
            if status != "connected" or last_error:
                detail = last_error or f"status={status}"
                issues.append(
                    HealthIssue(
                        key=f"account:{account_id}:connection",
                        alert_type="mt5_connection",
                        entity_id=str(account_id),
                        title=f"{alias} ({masked_login})",
                        summary=detail,
                    )
                )
                continue

            heartbeat_at = parse_datetime(row[5] or row[6] or row[7])
            if heartbeat_at is None or current_time - heartbeat_at > stale_after:
                if current_time - self.started_at <= stale_after:
                    continue
                detail = (
                    "Worker ainda nao registrou heartbeat."
                    if heartbeat_at is None
                    else f"Heartbeat parado desde {format_timestamp(heartbeat_at)}."
                )
                issues.append(
                    HealthIssue(
                        key=f"account:{account_id}:heartbeat",
                        alert_type="mt5_worker_heartbeat",
                        entity_id=str(account_id),
                        title=f"{alias} ({masked_login})",
                        summary=detail,
                    )
                )
        return issues

    def _signal_monitor_issues(self, current_time: datetime) -> list[HealthIssue]:
        with connect_database(self.config.database_path) as connection:
            cursor = connection.execute(
                "SELECT heartbeat_at FROM service_heartbeats WHERE service_name = ?",
                (SIGNAL_MONITOR_SERVICE_NAME,),
            )
            try:
                row = cursor.fetchone()
            finally:
                cursor.close()

        heartbeat_at = parse_datetime(row[0]) if row else None
        stale_after = timedelta(seconds=self.config.health_stale_after_seconds)
        if current_time - self.started_at <= stale_after:
            return []
        if heartbeat_at is None:
            summary = "Monitor de sinais ainda nao registrou heartbeat."
        elif current_time - heartbeat_at <= stale_after:
            return []
        else:
            summary = f"Heartbeat parado desde {format_timestamp(heartbeat_at)}."
        return [
            HealthIssue(
                key="service:signal-monitor:heartbeat",
                alert_type="signal_monitor_heartbeat",
                entity_id=SIGNAL_MONITOR_SERVICE_NAME,
                title="Monitor de sinais",
                summary=summary,
            )
        ]

    def _synchronize(
        self,
        issues: dict[str, HealthIssue],
        current_time: datetime,
    ) -> None:
        timestamp = current_time.isoformat()
        repeat_after = timedelta(
            minutes=self.config.operational_alert_repeat_minutes
        )
        pending_notifications: list[tuple[str, str, str]] = []
        with connect_database(self.config.database_path) as connection:
            rows = connection.execute(
                """
                SELECT alert_key, alert_type, entity_id, summary, is_active,
                       opened_at, last_seen_at, last_notified_at, resolved_at
                FROM operational_alert_states
                """
            ).fetchall()
            stored = {str(row[0]): row for row in rows}

            for key, issue in issues.items():
                row = stored.get(key)
                should_notify = False
                if row is None:
                    connection.execute(
                        """
                        INSERT INTO operational_alert_states (
                            alert_key, alert_type, entity_id, summary, is_active,
                            opened_at, last_seen_at, last_notified_at, resolved_at
                        ) VALUES (?, ?, ?, ?, 1, ?, ?, NULL, NULL)
                        """,
                        (
                            key,
                            issue.alert_type,
                            issue.entity_id,
                            issue.summary,
                            timestamp,
                            timestamp,
                        ),
                    ).close()
                    should_notify = True
                elif not bool(row[4]):
                    connection.execute(
                        """
                        UPDATE operational_alert_states
                        SET alert_type = ?, entity_id = ?, summary = ?,
                            is_active = 1, opened_at = ?, last_seen_at = ?,
                            last_notified_at = NULL, resolved_at = NULL
                        WHERE alert_key = ?
                        """,
                        (
                            issue.alert_type,
                            issue.entity_id,
                            issue.summary,
                            timestamp,
                            timestamp,
                            key,
                        ),
                    ).close()
                    should_notify = True
                else:
                    last_notified_at = parse_datetime(row[7])
                    should_notify = (
                        last_notified_at is None
                        or current_time - last_notified_at >= repeat_after
                        or str(row[3]) != issue.summary
                    )
                    connection.execute(
                        """
                        UPDATE operational_alert_states
                        SET summary = ?, last_seen_at = ?
                        WHERE alert_key = ?
                        """,
                        (issue.summary, timestamp, key),
                    ).close()

                if should_notify:
                    pending_notifications.append(
                        (key, "problem", format_problem(issue))
                    )

            for key, row in stored.items():
                if not bool(row[4]) or key in issues:
                    continue
                title = alert_title(str(row[1]), row[2])
                pending_notifications.append(
                    (key, "recovery", format_recovery(title))
                )

        for key, notification_type, message in pending_notifications:
            if not self.notifier.send(message):
                continue
            with connect_database(self.config.database_path) as connection:
                if notification_type == "problem":
                    connection.execute(
                        """
                        UPDATE operational_alert_states
                        SET last_notified_at = ?
                        WHERE alert_key = ?
                        """,
                        (timestamp, key),
                    ).close()
                else:
                    connection.execute(
                        """
                        UPDATE operational_alert_states
                        SET is_active = 0, resolved_at = ?, last_seen_at = ?
                        WHERE alert_key = ?
                        """,
                        (timestamp, timestamp, key),
                    ).close()


def parse_datetime(value: object) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def mask_login(login: str) -> str:
    return f"••••{login[-4:]}" if login else "conta"


def format_timestamp(value: datetime) -> str:
    local_time = value.astimezone(timezone(timedelta(hours=-3)))
    return local_time.strftime("%d/%m/%Y %H:%M:%S")


def format_problem(issue: HealthIssue) -> str:
    return (
        "🚨 ALERTA OPERACIONAL\n\n"
        f"{issue.title}\n"
        f"Problema: {issue.summary}\n\n"
        "O sistema continuará tentando recuperar automaticamente."
    )


def alert_title(alert_type: str, entity_id: object) -> str:
    if alert_type == "signal_monitor_heartbeat":
        return "Monitor de sinais"
    if alert_type == "mt5_worker_heartbeat":
        return f"Worker da conta {entity_id}"
    if alert_type == "mt5_connection":
        return f"Conexão MT5 da conta {entity_id}"
    return str(entity_id or "Serviço")


def format_recovery(title: str) -> str:
    return f"✅ SERVIÇO RECUPERADO\n\n{title} voltou a funcionar normalmente."


def build_health_logger() -> logging.Logger:
    logger = logging.getLogger("telegram_mt5_operational_health")
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(handler)
    return logger


def main() -> int:
    try:
        config = AppConfig.load(create_dirs=True)
        logger = build_health_logger()
        if not config.operational_alerts_enabled:
            logger.info("Alertas operacionais desativados.")
            return 0
        notifier = TelegramAdminNotifier(
            config.telegram_bot_token,
            config.bot_admin_ids,
            logger=logger,
        )
        monitor = OperationalHealthMonitor(
            config,
            notifier=notifier,
            logger=logger,
        )
        logger.info(
            "Monitor operacional iniciado. intervalo=%ss stale=%ss admins=%s",
            config.health_check_interval_seconds,
            config.health_stale_after_seconds,
            len(config.bot_admin_ids),
        )
        while True:
            try:
                issues = monitor.check_once()
                if issues:
                    logger.warning("Problemas operacionais ativos: %s", len(issues))
            except Exception as exc:
                logger.error("Falha na verificacao operacional: %s", exc)
            time.sleep(config.health_check_interval_seconds)
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        print(f"Falha no monitor operacional: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
