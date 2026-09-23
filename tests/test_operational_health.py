from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest

from telegram_mt5_copier.config import AppConfig
from telegram_mt5_copier.database import (
    CENTRAL_SYNC_SERVICE_NAME,
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


class CentralSyncHealthTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.now = datetime(2026, 7, 28, 20, 0, tzinfo=timezone.utc)
        self.config = AppConfig.load(
            project_root=self.root,
            env={
                "HEALTH_STALE_AFTER_SECONDS": "90",
                "OPERATIONAL_ALERT_REPEAT_MINUTES": "360",
                "CENTRAL_SYNC_ENABLED": "true",
                "CENTRAL_SYNC_DATABASE_URL": "postgresql://postgres:postgres@127.0.0.1:54322/postgres",
                "CENTRAL_SYNC_DELIVERY_LAG_SECONDS": "300",
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
        # Passa da janela de graca inicial (_account_issues/_signal_monitor_issues
        # e drain_stale suprimem tudo logo apos o start).
        self.now += timedelta(seconds=91)
        # Heartbeats "saudaveis" por padrao -- os testes de central_sync_*
        # so devem introduzir o UNICO problema que estao testando, sem que o
        # heartbeat do monitor de sinais (checagem nao relacionada) ou o
        # heartbeat do drain loop (quando nao e o alvo do teste) contaminem a
        # contagem de notificacoes.
        with connect_database(self.config.database_path) as connection:
            connection.execute(
                "INSERT INTO service_heartbeats (service_name, heartbeat_at, details) VALUES (?, ?, NULL)",
                (SIGNAL_MONITOR_SERVICE_NAME, self.now.isoformat()),
            ).close()
        self._set_central_sync_heartbeat(self.now.isoformat())

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _insert_signal(self, created_at: str, source_message_id: int) -> int:
        with connect_database(self.config.database_path) as connection:
            cursor = connection.execute(
                """
                INSERT INTO signals (
                    signature, content_signature, symbol, direction, entry_low, entry_high,
                    stop_loss, take_profits, source_chat_id, source_message_id, raw_text,
                    formatted_message, created_at
                ) VALUES (?, ?, 'XAUUSD', 'BUY', '4100', '4105', '4090', '[]', '123456', ?, 'raw', 'fmt', ?)
                """,
                (f"sig-{source_message_id}", f"sig-{source_message_id}", str(source_message_id), created_at),
            )
            signal_id = int(cursor.lastrowid)
            cursor.close()
        return signal_id

    def _insert_activation(self, activation_signal_id: int) -> None:
        with connect_database(self.config.database_path) as connection:
            connection.execute(
                "INSERT INTO central_sync_activation (id, activation_signal_id, activated_at) VALUES (1, ?, ?)",
                (activation_signal_id, self.now.isoformat()),
            ).close()

    def _insert_outbox_row(
        self,
        *,
        source_signal_id: int,
        status: str,
        created_at: str,
        next_attempt_at: str | None = None,
    ) -> int:
        with connect_database(self.config.database_path) as connection:
            cursor = connection.execute(
                """
                INSERT INTO central_sync_outbox (
                    kind, source_signal_id, payload, status, attempts, created_at, updated_at, next_attempt_at
                ) VALUES ('signal_shadow_write', ?, '{}', ?, 0, ?, ?, ?)
                """,
                (source_signal_id, status, created_at, created_at, next_attempt_at or created_at),
            )
            row_id = int(cursor.lastrowid)
            cursor.close()
        return row_id

    def _touch_healthy_heartbeats(self) -> None:
        with connect_database(self.config.database_path) as connection:
            connection.execute(
                "UPDATE service_heartbeats SET heartbeat_at = ? WHERE service_name = ?",
                (self.now.isoformat(), SIGNAL_MONITOR_SERVICE_NAME),
            ).close()
        self._set_central_sync_heartbeat(self.now.isoformat())

    def _set_central_sync_heartbeat(self, heartbeat_at: str) -> None:
        with connect_database(self.config.database_path) as connection:
            connection.execute(
                """
                INSERT INTO service_heartbeats (service_name, heartbeat_at, details)
                VALUES (?, ?, NULL)
                ON CONFLICT(service_name) DO UPDATE SET heartbeat_at = excluded.heartbeat_at
                """,
                (CENTRAL_SYNC_SERVICE_NAME, heartbeat_at),
            ).close()

    def test_gap_alerta_quando_sinal_sem_outbox_apos_baseline(self) -> None:
        self._insert_activation(activation_signal_id=0)
        self._insert_signal(self.now.isoformat(), source_message_id=1)  # sem outbox correspondente

        issues = self.monitor.check_once()

        keys = [issue.key for issue in issues]
        self.assertIn("central_sync:enqueue_gap", keys)

    def test_gap_nao_alerta_para_sinal_anterior_ao_baseline(self) -> None:
        old_signal_id = self._insert_signal(
            (self.now - timedelta(days=1)).isoformat(), source_message_id=1
        )
        self._insert_activation(activation_signal_id=old_signal_id)  # baseline == esse sinal (historico)

        issues = self.monitor.check_once()

        keys = [issue.key for issue in issues]
        self.assertNotIn("central_sync:enqueue_gap", keys)

    def test_gap_nao_alerta_quando_outbox_existe_no_mesmo_instante_do_sinal(self) -> None:
        self._insert_activation(activation_signal_id=0)
        timestamp = self.now.isoformat()
        signal_id = self._insert_signal(timestamp, source_message_id=1)
        self._insert_outbox_row(source_signal_id=signal_id, status="pending", created_at=timestamp)

        issues = self.monitor.check_once()

        keys = [issue.key for issue in issues]
        self.assertNotIn("central_sync:enqueue_gap", keys)

    def test_lag_alerta_mesmo_com_next_attempt_at_no_futuro(self) -> None:
        self._insert_activation(activation_signal_id=0)
        old_timestamp = (self.now - timedelta(minutes=20)).isoformat()
        signal_id = self._insert_signal(old_timestamp, source_message_id=1)
        future_retry = (self.now + timedelta(minutes=5)).isoformat()
        self._insert_outbox_row(
            source_signal_id=signal_id,
            status="failed",
            created_at=old_timestamp,
            next_attempt_at=future_retry,  # retry legitimamente agendado pro futuro
        )

        issues = self.monitor.check_once()

        lag_issues = [issue for issue in issues if issue.key == "central_sync:delivery_lag"]
        self.assertEqual(len(lag_issues), 1)
        self.assertIn("atrasad", lag_issues[0].summary.lower())
        self.assertNotIn("travad", lag_issues[0].summary.lower())

    def test_lag_resumo_estavel_nao_reenvia_dentro_da_janela_de_repeticao(self) -> None:
        self._insert_activation(activation_signal_id=0)
        old_timestamp = (self.now - timedelta(minutes=20)).isoformat()
        signal_id = self._insert_signal(old_timestamp, source_message_id=1)
        self._insert_outbox_row(source_signal_id=signal_id, status="failed", created_at=old_timestamp)

        self.monitor.check_once()
        self.assertEqual(len(self.notifier.messages), 1)

        # Avanca o relogio varias vezes dentro da janela de repeticao (360 min);
        # o resumo usa timestamp absoluto do item mais antigo, entao nao muda
        # a cada tick -- nao deve reenviar.
        for _ in range(5):
            self.now += timedelta(minutes=1)
            self._touch_healthy_heartbeats()
            self.monitor.check_once()

        self.assertEqual(len(self.notifier.messages), 1)

    def test_drain_stale_alerta_quando_heartbeat_parado(self) -> None:
        stale_heartbeat = (self.now - timedelta(minutes=5)).isoformat()
        self._set_central_sync_heartbeat(stale_heartbeat)

        issues = self.monitor.check_once()

        keys = [issue.key for issue in issues]
        self.assertIn("central_sync:drain_stale", keys)

    def test_drain_stale_nao_alerta_quando_heartbeat_recente(self) -> None:
        self._set_central_sync_heartbeat(self.now.isoformat())

        issues = self.monitor.check_once()

        keys = [issue.key for issue in issues]
        self.assertNotIn("central_sync:drain_stale", keys)

    def test_central_sync_desabilitado_nao_gera_nenhum_alerta(self) -> None:
        disabled_config = AppConfig.load(
            project_root=self.root,
            env={
                "HEALTH_STALE_AFTER_SECONDS": "90",
                "OPERATIONAL_ALERT_REPEAT_MINUTES": "360",
                "CENTRAL_SYNC_ENABLED": "false",
            },
            create_dirs=True,
        )
        monitor = OperationalHealthMonitor(
            disabled_config, notifier=self.notifier, logger=_NullLogger(), now=lambda: self.now  # type: ignore[arg-type]
        )
        # Outbox visivelmente quebrado -- mas central_sync_enabled=false.
        self._insert_activation(activation_signal_id=0)
        self._insert_signal(self.now.isoformat(), source_message_id=1)
        self._set_central_sync_heartbeat((self.now - timedelta(hours=1)).isoformat())

        issues = monitor.check_once()

        keys = [issue.key for issue in issues]
        self.assertNotIn("central_sync:enqueue_gap", keys)
        self.assertNotIn("central_sync:delivery_lag", keys)
        self.assertNotIn("central_sync:drain_stale", keys)

    def test_desligar_central_sync_com_alerta_ativo_nao_anuncia_falsa_recuperacao(self) -> None:
        self._insert_activation(activation_signal_id=0)
        self._insert_signal(self.now.isoformat(), source_message_id=1)  # gera gap
        self.monitor.check_once()
        self.assertEqual(len(self.notifier.messages), 1)  # alerta de problema

        disabled_config = AppConfig.load(
            project_root=self.root,
            env={
                "HEALTH_STALE_AFTER_SECONDS": "90",
                "OPERATIONAL_ALERT_REPEAT_MINUTES": "360",
                "CENTRAL_SYNC_ENABLED": "false",
            },
            create_dirs=True,
        )
        monitor = OperationalHealthMonitor(
            disabled_config, notifier=self.notifier, logger=_NullLogger(), now=lambda: self.now  # type: ignore[arg-type]
        )
        monitor.check_once()

        # Nenhuma mensagem nova -- em especial, nenhuma de "recuperado".
        self.assertEqual(len(self.notifier.messages), 1)
        with connect_database(self.config.database_path) as connection:
            row = connection.execute(
                "SELECT is_active FROM operational_alert_states WHERE alert_key = 'central_sync:enqueue_gap'"
            ).fetchone()
        self.assertEqual(row, (0,))


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
