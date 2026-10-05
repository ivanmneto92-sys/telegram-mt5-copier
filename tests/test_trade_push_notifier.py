from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
import tempfile
import unittest

from telegram_mt5_copier.credential_service import CredentialService
from telegram_mt5_copier.mt5.account_service import MT5AccountForm, MT5AccountService
from telegram_mt5_copier.mt5.client import SimulatedMT5Client
from telegram_mt5_copier.mt5.daily_performance import DailyPerformance
from telegram_mt5_copier.mt5.terminal_manager import TerminalManager
from telegram_mt5_copier.mt5.trade_push_notifier import TradePushNotifier
from telegram_mt5_copier.users import UserRepository
from telegram_mt5_copier.web_push import ExpiredPushSubscription, WebPushSender, generate_vapid_keypair


class RecordingSender(WebPushSender):
    def __init__(self) -> None:
        super().__init__(
            vapid_public_key="pub",
            vapid_private_key="priv",
            vapid_contact="ops@example.com",
        )
        self.sent: list[tuple[str, str, str]] = []
        self.raise_expired_for: set[str] = set()

    def send(self, subscription, *, title, body, tag=None, data=None):
        if subscription.endpoint in self.raise_expired_for:
            raise ExpiredPushSubscription(subscription.endpoint)
        self.sent.append((subscription.endpoint, title, body))
        return True


def deal(ticket: str, profit: str, *, entry: int = 1, symbol: str = "XAUUSD") -> dict[str, object]:
    return {
        "ticket": ticket,
        "entry": entry,
        "profit": Decimal(profit),
        "commission": Decimal("0"),
        "swap": Decimal("0"),
        "fee": Decimal("0"),
        "symbol": symbol,
        "time": datetime.now(tz=timezone.utc).timestamp(),
    }


class TradePushNotifierTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.database_path = self.root / "push.sqlite3"
        self.users = UserRepository(self.database_path)
        self.accounts = MT5AccountService(
            self.database_path,
            credential_service=CredentialService(CredentialService.generate_key()),
            terminal_manager=TerminalManager(self.root / "mt5"),
            client_factory=lambda: SimulatedMT5Client(),
        )
        self.user = self.users.get_or_create_user(101, "alice")
        self.account = self.accounts.register_account(
            self.user.id,
            MT5AccountForm("Broker", "Broker-Demo", "12345678", "secret", "Demo"),
        )
        self.accounts.save_push_subscription(
            self.user.id,
            endpoint="https://push.example/device-a",
            p256dh_key="p256dh",
            auth_key="auth",
            user_agent="pytest",
        )
        # Simula inscricao ja ativa ha tempo; a rodada de "armar" tem teste proprio.
        self.accounts.mark_push_subscriptions_primed(["https://push.example/device-a"])

    def tearDown(self) -> None:
        self.accounts.close()
        self.users.close()
        self.temp_dir.cleanup()

    def notifier(self, client: SimulatedMT5Client, sender: RecordingSender) -> TradePushNotifier:
        return TradePushNotifier(self.accounts, sender, client_factory=lambda: client)

    def test_operacao_fechada_gera_push_e_nao_repete(self) -> None:
        client = SimulatedMT5Client(history_deals=(deal("T1", "300"),))
        sender = RecordingSender()

        self.notifier(client, sender).process_account(self.account)
        self.assertEqual(len(sender.sent), 1)
        self.assertIn("+US$ 300.00", sender.sent[0][2])

        sender.sent.clear()
        self.notifier(client, sender).process_account(self.account)
        self.assertEqual(sender.sent, [])

    def test_operacao_negativa_e_nunca_e_renotificada(self) -> None:
        client = SimulatedMT5Client(history_deals=(deal("T2", "-50"),))
        sender = RecordingSender()

        self.notifier(client, sender).process_account(self.account)
        self.assertEqual(len(sender.sent), 1)
        self.assertEqual(sender.sent[0][1], "Operação negativa")

    def test_deal_de_entrada_nao_gera_push(self) -> None:
        client = SimulatedMT5Client(history_deals=(deal("T3", "0"),))
        sender = RecordingSender()

        self.notifier(client, sender).process_account(self.account)
        self.assertEqual(sender.sent, [])

    def test_inscricao_expirada_e_removida_apos_falha(self) -> None:
        client = SimulatedMT5Client(history_deals=(deal("T4", "10"),))
        sender = RecordingSender()
        sender.raise_expired_for.add("https://push.example/device-a")

        self.notifier(client, sender).process_account(self.account)

        self.assertEqual(self.accounts.push_subscriptions_for_user(self.user.id), [])

    def test_subscricao_desabilitada_para_operacoes_nao_recebe_push(self) -> None:
        self.accounts.remove_push_subscription(self.user.id, "https://push.example/device-a")
        self.accounts.save_push_subscription(
            self.user.id,
            endpoint="https://push.example/device-b",
            p256dh_key="p256dh",
            auth_key="auth",
            user_agent="pytest",
            trade_alerts_enabled=False,
        )
        client = SimulatedMT5Client(history_deals=(deal("T5", "10"),))
        sender = RecordingSender()

        self.notifier(client, sender).process_account(self.account)
        self.assertEqual(sender.sent, [])

    def test_inscricao_nova_nao_recebe_historico_pendente(self) -> None:
        self.accounts.remove_push_subscription(self.user.id, "https://push.example/device-a")
        self.accounts.save_push_subscription(
            self.user.id,
            endpoint="https://push.example/device-new",
            p256dh_key="p256dh",
            auth_key="auth",
            user_agent="pytest",
        )
        yesterday = (datetime.now(tz=timezone.utc) - timedelta(days=1)).date().isoformat()
        self.accounts.update_daily_performance(
            self.account.id,
            DailyPerformance(
                performance_date=yesterday,
                realized_profit=Decimal("30"),
                starting_balance=Decimal("1000"),
                return_percent=Decimal("3"),
                updated_at=datetime.now(tz=timezone.utc).isoformat(),
            ),
        )
        backlog = tuple(deal(f"OLD{index}", "10") for index in range(30))
        sender = RecordingSender()

        self.notifier(SimulatedMT5Client(history_deals=backlog), sender).process_account(
            self.account
        )
        self.assertEqual(sender.sent, [])

        client = SimulatedMT5Client(history_deals=(*backlog, deal("NEW", "25")))
        self.notifier(client, sender).process_account(self.account)
        self.assertEqual(len(sender.sent), 1)
        self.assertIn("+US$ 25.00", sender.sent[0][2])

    def test_resumo_diario_e_enviado_uma_vez_quando_o_dia_vira(self) -> None:
        yesterday = (datetime.now(tz=timezone.utc) - timedelta(days=1)).date().isoformat()
        self.accounts.update_daily_performance(
            self.account.id,
            DailyPerformance(
                performance_date=yesterday,
                realized_profit=Decimal("30"),
                starting_balance=Decimal("1000"),
                return_percent=Decimal("3"),
                updated_at=datetime.now(tz=timezone.utc).isoformat(),
            ),
        )
        client = SimulatedMT5Client(history_deals=())
        sender = RecordingSender()

        self.notifier(client, sender).process_account(self.account)

        summaries = [entry for entry in sender.sent if "Resultado do dia" in entry[1]]
        self.assertEqual(len(summaries), 1)
        self.assertIn("+3.00%", summaries[0][2])

        sender.sent.clear()
        self.notifier(client, sender).process_account(self.account)
        self.assertEqual([e for e in sender.sent if "Resultado do dia" in e[1]], [])


class PushSubscriptionRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.database_path = self.root / "sub.sqlite3"
        self.users = UserRepository(self.database_path)
        self.accounts = MT5AccountService(
            self.database_path,
            credential_service=CredentialService(CredentialService.generate_key()),
            terminal_manager=TerminalManager(self.root / "mt5"),
            client_factory=lambda: SimulatedMT5Client(),
        )
        self.user = self.users.get_or_create_user(101, "alice")

    def tearDown(self) -> None:
        self.accounts.close()
        self.users.close()
        self.temp_dir.cleanup()

    def test_salvar_inscricao_duplicada_pelo_endpoint_atualiza_em_vez_de_duplicar(self) -> None:
        self.accounts.save_push_subscription(
            self.user.id, endpoint="https://push.example/x", p256dh_key="a", auth_key="b", user_agent=None
        )
        self.accounts.save_push_subscription(
            self.user.id, endpoint="https://push.example/x", p256dh_key="c", auth_key="d", user_agent=None
        )

        subscriptions = self.accounts.push_subscriptions_for_user(self.user.id)
        self.assertEqual(len(subscriptions), 1)
        self.assertEqual(subscriptions[0].p256dh_key, "c")

    def test_remover_inscricao_de_outro_usuario_nao_apaga_nada(self) -> None:
        other = self.users.get_or_create_user(202, "bob")
        self.accounts.save_push_subscription(
            self.user.id, endpoint="https://push.example/x", p256dh_key="a", auth_key="b", user_agent=None
        )

        self.accounts.remove_push_subscription(other.id, "https://push.example/x")

        self.assertEqual(len(self.accounts.push_subscriptions_for_user(self.user.id)), 1)


class VapidKeypairTests(unittest.TestCase):
    def test_gera_chaves_base64url_sem_padding(self) -> None:
        public_key, private_key = generate_vapid_keypair()

        self.assertNotIn("=", public_key)
        self.assertNotIn("=", private_key)
        self.assertGreater(len(public_key), 80)
        self.assertGreater(len(private_key), 30)

    def test_sender_nao_configurado_sem_chaves(self) -> None:
        sender = WebPushSender(vapid_public_key=None, vapid_private_key=None, vapid_contact="a@b.com")
        self.assertFalse(sender.configured)
