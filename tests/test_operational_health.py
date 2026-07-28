from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest

from telegram_mt5_copier.config import AppConfig
from telegram_mt5_copier.database import (
    SIGNAL_MONITOR_SERVICE_NAME,
    connect_database,
    initialize_database,
)
from telegram_mt5_copier.operational_health import OperationalHealthMonitor


class FakeNotifier:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def send(self, message: str) -> bool:
        self.messages.append(message)
        return True


class OperationalHealthTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.now = datetime(2026, 7, 28, 20, 0, tzinfo=timezone.utc)
        self.config = AppConfig.load(
            project_root=self.root,
            env={
                "HEALTH_STALE_AFTER_SECONDS": "90",
                "OPERATIONAL_ALERT_REPEAT_MINUTES": "360",
            },
            create_dirs=True,
        )
        initialize_database(self.config.database_path)
        self.notifier = FakeNotifier()
        self.monitor = OperationalHealthMonitor(
            self.config,
            notifier=self.notifier,  # type: ignore[arg-type]
            logger=_NullLogger(),
            now=lambda: self.now,
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_alerta_heartbeat_nao_repete_e_notifica_recuperacao(self) -> None:
        stale = (self.now - timedelta(minutes=5)).isoformat()
        with connect_database(self.config.database_path) as connection:
            connection.execute(
                """
                INSERT INTO service_heartbeats (service_name, heartbeat_at, details)
                VALUES (?, ?, NULL)
                """,
                (SIGNAL_MONITOR_SERVICE_NAME, stale),
            ).close()

        self.now += timedelta(seconds=91)
        self.monitor.check_once()
        self.monitor.check_once()
        self.assertEqual(len(self.notifier.messages), 1)
        self.assertIn("Monitor de sinais", self.notifier.messages[0])

        with connect_database(self.config.database_path) as connection:
            connection.execute(
                """
                UPDATE service_heartbeats SET heartbeat_at = ?
                WHERE service_name = ?
                """,
                (self.now.isoformat(), SIGNAL_MONITOR_SERVICE_NAME),
            ).close()
        self.monitor.check_once()

        self.assertEqual(len(self.notifier.messages), 2)
        self.assertIn("SERVIÇO RECUPERADO", self.notifier.messages[1])
        with connect_database(self.config.database_path) as connection:
            row = connection.execute(
                """
                SELECT is_active FROM operational_alert_states
                WHERE alert_key = 'service:signal-monitor:heartbeat'
                """
            ).fetchone()
        self.assertEqual(row, (0,))

    def test_apenas_conta_mais_recente_do_usuario_gera_alerta(self) -> None:
        fresh = self.now.isoformat()
        old = (self.now - timedelta(minutes=10)).isoformat()
        with connect_database(self.config.database_path) as connection:
            connection.execute(
                """
                INSERT INTO users (
                    telegram_user_id, telegram_username, status, created_at, updated_at
                ) VALUES (1001, 'cliente', 'active', ?, ?)
                """,
                (fresh, fresh),
            ).close()
            user_id = int(
                connection.execute(
                    "SELECT id FROM users WHERE telegram_user_id = 1001"
                ).fetchone()[0]
            )
            old_id = insert_account(
                connection,
                user_id=user_id,
                login="1111",
                alias="Antiga",
                status="failed",
                last_error="IPC timeout",
                heartbeat=old,
                timestamp=fresh,
            )
            new_id = insert_account(
                connection,
                user_id=user_id,
                login="2222",
                alias="Atual",
                status="connected",
                last_error=None,
                heartbeat=fresh,
                timestamp=fresh,
            )
            insert_profile(connection, user_id, old_id, fresh)
            insert_profile(connection, user_id, new_id, fresh)
            connection.execute(
                """
                INSERT INTO service_heartbeats (service_name, heartbeat_at, details)
                VALUES (?, ?, NULL)
                """,
                (SIGNAL_MONITOR_SERVICE_NAME, fresh),
            ).close()

        self.monitor.check_once()
        self.assertEqual(self.notifier.messages, [])

        self.now += timedelta(seconds=91)
        with connect_database(self.config.database_path) as connection:
            connection.execute(
                "UPDATE mt5_accounts SET worker_heartbeat_at = ? WHERE id = ?",
                (old, new_id),
            ).close()
            connection.execute(
                """
                UPDATE service_heartbeats SET heartbeat_at = ?
                WHERE service_name = ?
                """,
                (self.now.isoformat(), SIGNAL_MONITOR_SERVICE_NAME),
            ).close()
        issues = self.monitor.check_once()

        self.assertEqual([issue.entity_id for issue in issues], [str(new_id)])
        self.assertEqual(len(self.notifier.messages), 1)
        self.assertIn("Atual", self.notifier.messages[0])
        self.assertNotIn("Antiga", self.notifier.messages[0])


def insert_account(
    connection: object,
    *,
    user_id: int,
    login: str,
    alias: str,
    status: str,
    last_error: str | None,
    heartbeat: str,
    timestamp: str,
) -> int:
    cursor = connection.execute(  # type: ignore[attr-defined]
        """
        INSERT INTO mt5_accounts (
            user_id, broker_name, server_name, login, encrypted_password,
            account_alias, terminal_path, account_type, account_mode,
            connection_status, last_error, last_connected_at, balance, equity,
            worker_heartbeat_at, created_at, updated_at
        ) VALUES (?, 'HFM', 'Live3', ?, 'encrypted', ?, ?, 'real', 'hedging',
                  ?, ?, ?, '1000', '1000', ?, ?, ?)
        """,
        (
            user_id,
            login,
            alias,
            f"C:\\MT5Accounts\\{login}\\terminal64.exe",
            status,
            last_error,
            timestamp,
            heartbeat,
            timestamp,
            timestamp,
        ),
    )
    try:
        return int(cursor.lastrowid)
    finally:
        cursor.close()


def insert_profile(
    connection: object,
    user_id: int,
    account_id: int,
    timestamp: str,
) -> None:
    connection.execute(  # type: ignore[attr-defined]
        """
        INSERT INTO execution_profiles (
            user_id, mt5_account_id, enabled, risk_mode, fixed_lot, risk_percent,
            max_spread_points, max_slippage_points, daily_profit_target,
            daily_loss_limit, max_open_signals, split_tps, breakeven_enabled,
            trailing_enabled, entry_execution_mode, entry_price_mode,
            pending_expiration_minutes, take_profit_limit, updated_at
        ) VALUES (?, ?, 1, 'fixed_lot', '0.01', '1', 100, 20, '0', '0',
                  1, 1, 1, 0, 'pending_order', 'first_touch', 120, 0, ?)
        """,
        (user_id, account_id, timestamp),
    ).close()


class _NullLogger:
    def warning(self, *_args: object, **_kwargs: object) -> None:
        pass

    def error(self, *_args: object, **_kwargs: object) -> None:
        pass


if __name__ == "__main__":
    unittest.main()
