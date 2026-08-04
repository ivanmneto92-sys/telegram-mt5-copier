from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import unittest

from telegram_mt5_copier.mt5.client import SimulatedMT5Client
from telegram_mt5_copier.mt5.daily_performance import calculate_daily_performance


class DailyPerformanceTests(unittest.TestCase):
    def test_resultado_realizado_e_percentual_sobre_banca_inicial(self) -> None:
        client = SimulatedMT5Client(
            history_deals=(
                {"type": 0, "profit": "120", "commission": "-5", "swap": "-2", "fee": "0"},
                {"type": 1, "profit": "-30", "commission": "-3", "swap": "0", "fee": "-1"},
                {"type": 2, "profit": "5000"},  # depósito não é resultado operacional
            )
        )

        result = calculate_daily_performance(
            client,
            Decimal("10079"),
            now=datetime(2026, 7, 28, 15, 0, tzinfo=timezone.utc),
        )

        self.assertEqual(result.performance_date, "2026-07-28")
        self.assertEqual(result.realized_profit, Decimal("79"))
        self.assertEqual(result.gross_profit, Decimal("90"))
        self.assertEqual(result.trading_costs, Decimal("-11"))
        self.assertEqual(result.starting_balance, Decimal("10000"))
        self.assertEqual(result.return_percent, Decimal("0.7900"))

    def test_resultado_negativo(self) -> None:
        client = SimulatedMT5Client(
            history_deals=({"type": 0, "profit": "-100", "commission": "-5"},)
        )

        result = calculate_daily_performance(
            client,
            Decimal("9895"),
            now=datetime(2026, 7, 28, 15, 0, tzinfo=timezone.utc),
        )

        self.assertEqual(result.realized_profit, Decimal("-105"))
        self.assertEqual(result.gross_profit, Decimal("-100"))
        self.assertEqual(result.trading_costs, Decimal("-5"))
        self.assertEqual(result.starting_balance, Decimal("10000"))
        self.assertEqual(result.return_percent, Decimal("-1.0500"))

    def test_dia_segue_fuso_do_servidor_mt5(self) -> None:
        client = SimulatedMT5Client(history_deals=())

        calculate_daily_performance(
            client,
            Decimal("10000"),
            now=datetime(2026, 8, 4, 1, 30, tzinfo=timezone.utc),
            timezone_name="Europe/Athens",
        )

        date_from, date_to = client.history_deal_queries[-1]
        self.assertEqual(date_from, datetime(2026, 8, 3, 21, 0, tzinfo=timezone.utc))
        self.assertEqual(date_to, datetime(2026, 8, 4, 1, 30, tzinfo=timezone.utc))
