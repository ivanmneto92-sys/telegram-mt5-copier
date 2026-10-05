from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
import tempfile
import unittest

from telegram_mt5_copier.credential_service import CredentialService
from telegram_mt5_copier.database import connect_database
from telegram_mt5_copier.mt5.account_service import MT5AccountForm, MT5AccountService
from telegram_mt5_copier.mt5.client import SimulatedMT5Client
from telegram_mt5_copier.mt5.daily_performance import calculate_history_performance
from telegram_mt5_copier.mt5.terminal_manager import TerminalManager
from telegram_mt5_copier.users import UserRepository

NOW = datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc)


def deal(when: str, profit: str, *, deal_type: int = 1, commission: str = "0") -> dict[str, object]:
    return {
        "time": int(datetime.fromisoformat(when).replace(tzinfo=timezone.utc).timestamp()),
        "type": deal_type,
        "profit": Decimal(profit),
        "commission": Decimal(commission),
        "swap": Decimal("0"),
        "fee": Decimal("0"),
    }


class HistoryPerformanceCalculationTests(unittest.TestCase):
    def test_resultado_por_dia_com_saldo_inicial_reconstruido(self) -> None:
        client = SimulatedMT5Client(
            history_deals=(
                deal("2026-10-01T10:00:00", "1000", deal_type=2),  # deposito
                deal("2026-10-02T10:00:00", "60", commission="-10"),
                deal("2026-10-05T10:00:00", "-40"),
                deal("2026-10-06T09:00:00", "100"),  # hoje: fica de fora
            )
        )

        history = calculate_history_performance(
            client, Decimal("1110"), now=NOW, timezone_name="UTC"
        )

        self.assertEqual(["2026-10-02", "2026-10-05"], [d.performance_date for d in history])
        oct2, oct5 = history
        self.assertEqual(Decimal("50"), oct2.realized_profit)
        self.assertEqual(Decimal("-10"), oct2.trading_costs)
        self.assertEqual(Decimal("1000"), oct2.starting_balance)
        self.assertEqual(Decimal("5"), oct2.return_percent)
        self.assertEqual(Decimal("-40"), oct5.realized_profit)
        self.assertEqual(Decimal("1050"), oct5.starting_balance)


class HistoryBackfillOnConnectTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.database_path = self.root / "history.sqlite3"
        self.users = UserRepository(self.database_path)
        self.client = SimulatedMT5Client(
            history_deals=(deal("2020-01-01T10:00:00", "0"),)
        )
        self.accounts = MT5AccountService(
            self.database_path,
            credential_service=CredentialService(CredentialService.generate_key()),
            terminal_manager=TerminalManager(self.root / "mt5"),
            client_factory=lambda: self.client,
        )
        self.user = self.users.get_or_create_user(101, "alice")

    def tearDown(self) -> None:
        self.accounts.close()
        self.users.close()
        self.temp_dir.cleanup()

    def rows(self, account_id: int) -> list[tuple[object, ...]]:
        with connect_database(self.database_path) as db:
            return db.execute(
                """
                SELECT performance_date, realized_profit, push_notified_at
                FROM account_daily_performance WHERE mt5_account_id = ?
                ORDER BY performance_date
                """,
                (account_id,),
            ).fetchall()

    def test_conectar_importa_historico_uma_vez_sem_gerar_push(self) -> None:
        yesterday = datetime.now(tz=timezone.utc).replace(hour=12).timestamp() - 86400
        self.client._history_deals = (
            {"time": int(yesterday), "type": 1, "profit": Decimal("80"),
             "commission": Decimal("0"), "swap": Decimal("0"), "fee": Decimal("0")},
        )
        account = self.accounts.register_account(
            self.user.id,
            MT5AccountForm("Broker", "Broker-Demo", "12345678", "secret", "Real"),
        )

        history = [row for row in self.rows(account.id) if row[2] is not None]
        self.assertEqual(1, len(history))
        self.assertEqual(Decimal("80"), Decimal(str(history[0][1])))

        with connect_database(self.database_path) as db:
            db.execute(
                "DELETE FROM account_daily_performance WHERE mt5_account_id = ?", (account.id,)
            )
        self.accounts.test_connection(self.user.id, account.id)
        self.assertEqual([], [row for row in self.rows(account.id) if row[2] is not None])

    def test_historico_importado_nao_sobrescreve_dia_registrado_ao_vivo(self) -> None:
        account = self.accounts.register_account(
            self.user.id,
            MT5AccountForm("Broker", "Broker-Demo", "12345678", "secret", "Real"),
        )
        from telegram_mt5_copier.mt5.daily_performance import DailyPerformance

        self.accounts.store_history_performance(
            account.id,
            [
                DailyPerformance(
                    performance_date="2026-01-01",
                    realized_profit=Decimal("10"),
                    starting_balance=Decimal("100"),
                    return_percent=Decimal("10"),
                    updated_at=NOW.isoformat(),
                )
            ],
        )
        self.accounts.store_history_performance(
            account.id,
            [
                DailyPerformance(
                    performance_date="2026-01-01",
                    realized_profit=Decimal("999"),
                    starting_balance=Decimal("100"),
                    return_percent=Decimal("999"),
                    updated_at=NOW.isoformat(),
                )
            ],
        )
        row = [r for r in self.rows(account.id) if r[0] == "2026-01-01"][0]
        self.assertEqual(Decimal("10"), Decimal(str(row[1])))


if __name__ == "__main__":
    unittest.main()
