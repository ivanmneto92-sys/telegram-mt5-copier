from __future__ import annotations

from pathlib import Path
from decimal import Decimal
import tempfile
import unittest

from telegram_mt5_copier.admin_panel import AdminPanelService, render_admin_panel, render_admin_script
from telegram_mt5_copier.database import SignalDatabase
from telegram_mt5_copier.bot_keyboards import CB_CHANNELS, CB_CHANNEL_ADD, CB_CHANNEL_MODE_CUSTOM
from telegram_mt5_copier.bot_service import BotService
from telegram_mt5_copier.channel_catalog import (
    ChannelCatalogService,
    REQUEST_AWAITING_MEMBERSHIP,
    REQUEST_READY_REVIEW,
    normalize_channel_link,
)
from telegram_mt5_copier.listener import validate_pending_channels_once
from telegram_mt5_copier.credential_service import CredentialService
from telegram_mt5_copier.mt5.account_service import MT5AccountForm, MT5AccountService
from telegram_mt5_copier.mt5.client import SimulatedMT5Client
from telegram_mt5_copier.mt5.terminal_manager import TerminalManager
from telegram_mt5_copier.models import Direction, TradeSignal
from telegram_mt5_copier.users import UserRepository
from tests.access_helpers import grant_paid_access


class CaptureLogger:
    def info(self, *_args: object, **_kwargs: object) -> None:
        pass


class FakeEntity:
    id = 2151523561
    title = "Gold Sniper"
    username = "SNIPERGOLD_SIGNALS"
    broadcast = True
    megagroup = False
    noforwards = True

    def __init__(self, *, left: bool) -> None:
        self.left = left


class FakeTelegramClient:
    def __init__(self, *, left: bool) -> None:
        self.entity = FakeEntity(left=left)

    async def get_entity(self, username: str) -> FakeEntity:
        assert username == "SNIPERGOLD_SIGNALS"
        return self.entity

    async def get_messages(self, _entity: object, limit: int) -> list[object]:
        assert limit == 1
        return [type("Message", (), {"id": 77})()]


class ChannelCatalogTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp_dir.name) / "channels.sqlite3"
        self.users = UserRepository(self.database_path)
        self.user = self.users.get_or_create_user(101, "alice")
        self.catalog = ChannelCatalogService(self.database_path)

    def tearDown(self) -> None:
        self.users.close()
        self.temp_dir.cleanup()

    def test_normaliza_links_publicos_inclusive_telegram_web(self) -> None:
        expected = "SNIPERGOLD_SIGNALS"
        for value in (
            "@SNIPERGOLD_SIGNALS",
            "https://t.me/SNIPERGOLD_SIGNALS",
            "https://t.me/s/SNIPERGOLD_SIGNALS",
            "https://web.telegram.org/k/#@SNIPERGOLD_SIGNALS",
        ):
            self.assertEqual(normalize_channel_link(value).username, expected)

    def test_link_privado_nao_e_armazenado(self) -> None:
        for value in ("https://t.me/+segredo", "https://t.me/c/123/4"):
            with self.assertRaisesRegex(ValueError, "privados"):
                self.catalog.submit_request(self.user.id, value)
        self.assertEqual(self.catalog.admin_channels()["requests"], [])

    async def test_validacao_exige_participacao_da_conta_principal(self) -> None:
        self.catalog.submit_request(self.user.id, "@SNIPERGOLD_SIGNALS")
        await validate_pending_channels_once(
            FakeTelegramClient(left=True),
            self.catalog,
            CaptureLogger(),
        )
        request = self.catalog.admin_channels()["requests"][0]
        self.assertEqual(request["status"], REQUEST_AWAITING_MEMBERSHIP)

        self.catalog.request_revalidation(int(request["id"]))
        await validate_pending_channels_once(
            FakeTelegramClient(left=False),
            self.catalog,
            CaptureLogger(),
        )
        request = self.catalog.admin_channels()["requests"][0]
        self.assertEqual(request["status"], REQUEST_READY_REVIEW)
        self.assertEqual(request["telegram_chat_id"], "-1002151523561")

    async def test_admin_aprova_e_cliente_pode_personalizar(self) -> None:
        request = self.catalog.submit_request(self.user.id, "@SNIPERGOLD_SIGNALS")
        await validate_pending_channels_once(
            FakeTelegramClient(left=False),
            self.catalog,
            CaptureLogger(),
        )
        self.catalog.approve_request(int(request.request_id), 9001)
        overview = self.catalog.user_overview(self.user.id)
        self.assertEqual(len(overview["channels"]), 1)
        self.assertTrue(overview["channels"][0]["enabled"])

        self.catalog.set_selection_mode(self.user.id, "custom")
        enabled = self.catalog.toggle_subscription(
            self.user.id,
            int(overview["channels"][0]["id"]),
        )
        self.assertFalse(enabled)
        self.assertFalse(self.catalog.user_overview(self.user.id)["channels"][0]["enabled"])

    def test_bot_recebe_sugestao_e_mostra_submenu(self) -> None:
        service = BotService(self.database_path)
        try:
            screen = service.handle_callback(101, "alice", CB_CHANNELS)
            self.assertIn("CANAIS DE SINAIS", screen.text)
            prompt = service.handle_callback(101, "alice", CB_CHANNEL_ADD)
            self.assertIn("conta principal", prompt.text)
            response = service.handle_text(
                101,
                "alice",
                "https://web.telegram.org/k/#@SNIPERGOLD_SIGNALS",
            )
            self.assertIn("SUGESTÃO RECEBIDA", response.text)
            custom = service.handle_callback(101, "alice", CB_CHANNEL_MODE_CUSTOM)
            self.assertIn("seleção personalizada", custom.text)
        finally:
            service.close()

    def test_execucao_seleciona_apenas_clientes_que_seguem_o_canal(self) -> None:
        bob = self.users.get_or_create_user(202, "bob")
        for user in (self.user, bob):
            grant_paid_access(self.database_path, user.id)
        accounts = MT5AccountService(
            self.database_path,
            credential_service=CredentialService(CredentialService.generate_key()),
            terminal_manager=TerminalManager(Path(self.temp_dir.name) / "mt5"),
            client_factory=lambda: SimulatedMT5Client(),
        )
        try:
            for user, login in ((self.user, "111111"), (bob, "222222")):
                accounts.register_account(
                    user.id,
                    MT5AccountForm("Broker", "Broker-Demo", login, "secret", user.telegram_username or "Conta"),
                )
            channel_id = self.catalog.register_configured_channel(
                telegram_chat_id="-1001234567890",
                title="Canal principal",
                username="CanalPrincipal",
                content_protected=False,
                history_accessible=True,
                last_message_id=1,
            )
            self.catalog.set_selection_mode(self.user.id, "custom")
            self.assertFalse(self.catalog.toggle_subscription(self.user.id, channel_id))

            selected = accounts.accounts_for_approved_users("-1001234567890")
            self.assertEqual([account.user_id for account, _profile in selected], [bob.id])
            self.assertEqual(len(accounts.accounts_for_approved_users("-1009999999999")), 1)
        finally:
            accounts.close()

    def test_mesmo_sinal_em_canais_diferentes_nao_bloqueia_assinantes(self) -> None:
        database = SignalDatabase(self.database_path)
        database.initialize()
        signal = TradeSignal(
            symbol="XAUUSD",
            direction=Direction.BUY,
            entry_low=Decimal("4100"),
            entry_high=Decimal("4105"),
            stop_loss=Decimal("4090"),
            take_profits=(Decimal("4110"),),
            raw_text="signal",
            source_chat_id="-100111",
            source_message_id="1",
        )
        database.record_accepted(signal, "formatado")
        self.assertTrue(database.has_duplicate(signal.content_signature, "-100111"))
        self.assertFalse(database.has_duplicate(signal.content_signature, "-100222"))

    async def test_painel_admin_aprova_somente_apos_validacao(self) -> None:
        request = self.catalog.submit_request(self.user.id, "@SNIPERGOLD_SIGNALS")
        panel = AdminPanelService(
            self.database_path,
            bot_token="123:token",
            admin_ids=(9001,),
        )
        with self.assertRaisesRegex(ValueError, "ainda não confirmou"):
            panel.approve_channel_request(
                admin_telegram_user_id=9001,
                request_id=int(request.request_id),
            )
        await validate_pending_channels_once(
            FakeTelegramClient(left=False),
            self.catalog,
            CaptureLogger(),
        )
        result = panel.approve_channel_request(
            admin_telegram_user_id=9001,
            request_id=int(request.request_id),
        )
        self.assertEqual(result["status"], "approved")
        self.assertEqual(panel.dashboard()["summary"]["active_channels"], 1)
        self.assertIn("Canais de sinais", render_admin_panel())
        self.assertIn("/api/admin/channel-", render_admin_script())
