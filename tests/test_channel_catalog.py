from __future__ import annotations

from pathlib import Path
from decimal import Decimal
import tempfile
import unittest

from telegram_mt5_copier.admin_panel import AdminPanelService, render_admin_panel, render_admin_script
from telegram_mt5_copier.database import (
    SignalDatabase,
    connect_database,
    initialize_database,
)
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


class FakePrivateTelegramClient(FakeTelegramClient):
    async def get_entity(self, chat_id: int) -> FakeEntity:
        assert chat_id == -1001234567890
        self.entity.username = None
        return self.entity


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

    def test_normaliza_e_armazena_links_privados(self) -> None:
        invite = normalize_channel_link("https://t.me/+AbCdEfGh1234")
        internal = normalize_channel_link("https://t.me/c/1234567890/42")

        self.assertIsNone(invite.username)
        self.assertEqual(invite.normalized_key, "invite:AbCdEfGh1234")
        self.assertEqual(internal.normalized_key, "chat_id:-1001234567890")
        result = self.catalog.submit_request(self.user.id, invite.canonical_link)
        self.assertEqual(result.title, "Canal privado")
        self.assertEqual(len(self.catalog.admin_channels()["requests"]), 1)

    def test_link_privado_invalido_e_rejeitado(self) -> None:
        with self.assertRaisesRegex(ValueError, "convite privado inválido"):
            self.catalog.submit_request(self.user.id, "https://t.me/+curto")

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

    async def test_valida_link_interno_de_canal_privado_quando_monitor_participa(self) -> None:
        self.catalog.submit_request(self.user.id, "https://t.me/c/1234567890/42")
        await validate_pending_channels_once(
            FakePrivateTelegramClient(left=False),
            self.catalog,
            CaptureLogger(),
        )
        request = self.catalog.admin_channels()["requests"][0]
        self.assertEqual(request["status"], REQUEST_READY_REVIEW)
        self.assertEqual(request["canonical_link"], "https://t.me/c/1234567890")

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

    def test_nome_real_do_canal_fica_mascarado_para_o_cliente(self) -> None:
        channel_id = self.catalog.register_configured_channel(
            telegram_chat_id="-1001234567890",
            title="Fornecedor Secreto Gold",
            username="FornecedorSecreto",
            content_protected=True,
            history_accessible=True,
            last_message_id=10,
        )

        overview = self.catalog.user_overview(self.user.id)
        self.assertEqual(overview["channels"][0]["title"], f"Sala de Sinais {channel_id:02d}")
        self.assertNotIn("Fornecedor Secreto Gold", str(overview))

        admin_channel = self.catalog.admin_channels()["channels"][0]
        self.assertEqual(admin_channel["title"], "Fornecedor Secreto Gold")
        self.assertEqual(admin_channel["display_name"], f"Sala de Sinais {channel_id:02d}")

    def test_admin_define_apelido_publico_sem_alterar_identidade_tecnica(self) -> None:
        channel_id = self.catalog.register_configured_channel(
            telegram_chat_id="-1001234567890",
            title="Fornecedor Secreto Gold",
            username="FornecedorSecreto",
            content_protected=False,
            history_accessible=True,
            last_message_id=10,
        )

        self.assertEqual(
            self.catalog.set_display_name(channel_id, "  Sala Ouro Premium  "),
            "Sala Ouro Premium",
        )
        overview = self.catalog.user_overview(self.user.id)
        self.assertEqual(overview["channels"][0]["title"], "Sala Ouro Premium")
        admin_channel = self.catalog.admin_channels()["channels"][0]
        self.assertEqual(admin_channel["telegram_chat_id"], "-1001234567890")
        self.assertEqual(admin_channel["title"], "Fornecedor Secreto Gold")

        self.assertEqual(
            self.catalog.set_display_name(channel_id, ""),
            f"Sala de Sinais {channel_id:02d}",
        )
        with self.assertRaisesRegex(ValueError, "60"):
            self.catalog.set_display_name(channel_id, "x" * 61)

    def test_canal_novo_exige_adesao_explicita_de_clientes_existentes(self) -> None:
        first_channel_id = self.catalog.register_configured_channel(
            telegram_chat_id="-1001111111111",
            title="Canal atual",
            username="CanalAtual",
            content_protected=False,
            history_accessible=True,
            last_message_id=1,
        )
        self.catalog.set_selection_mode(self.user.id, "all")
        first_overview = self.catalog.user_overview(self.user.id)
        self.assertEqual(first_overview["selection_mode"], "all")
        self.assertTrue(first_overview["channels"][0]["enabled"])

        second_channel_id = self.catalog.register_configured_channel(
            telegram_chat_id="-1002222222222",
            title="Canal novo",
            username="CanalNovo",
            content_protected=False,
            history_accessible=True,
            last_message_id=1,
        )
        overview = self.catalog.user_overview(self.user.id)
        enabled = {channel["id"]: channel["enabled"] for channel in overview["channels"]}
        self.assertEqual(overview["selection_mode"], "custom")
        self.assertTrue(enabled[first_channel_id])
        self.assertFalse(enabled[second_channel_id])

    def test_migracao_preserva_salas_atuais_sem_assinar_salas_futuras(self) -> None:
        channel_id = self.catalog.register_configured_channel(
            telegram_chat_id="-1003333333333",
            title="Canal legado",
            username="CanalLegado",
            content_protected=False,
            history_accessible=True,
            last_message_id=1,
        )
        with connect_database(self.database_path) as connection:
            connection.execute(
                "DELETE FROM user_channel_subscriptions WHERE user_id = ?",
                (self.user.id,),
            )
            connection.execute(
                """
                INSERT INTO user_channel_settings (user_id, selection_mode, updated_at)
                VALUES (?, 'all', 'legacy')
                ON CONFLICT(user_id) DO UPDATE SET selection_mode = 'all'
                """,
                (self.user.id,),
            )
            connection.execute(
                "DELETE FROM schema_migrations WHERE migration_key = ?",
                ("explicit_channel_opt_in_v1",),
            )

        initialize_database(self.database_path)
        overview = self.catalog.user_overview(self.user.id)
        self.assertEqual(overview["selection_mode"], "custom")
        self.assertEqual(overview["channels"], [
            {
                "id": channel_id,
                "title": f"Sala de Sinais {channel_id:02d}",
                "enabled": True,
            }
        ])

    def test_admin_desativa_e_reativa_canal_sem_perder_historico(self) -> None:
        channel_id = self.catalog.register_configured_channel(
            telegram_chat_id="-1001234567890",
            title="Fornecedor Secreto Gold",
            username="FornecedorSecreto",
            content_protected=True,
            history_accessible=True,
            last_message_id=10,
        )
        self.assertEqual(self.catalog.set_channel_status(channel_id, "suspended"), "suspended")
        self.assertFalse(self.catalog.is_active_chat("-1001234567890"))
        self.assertEqual(self.catalog.user_overview(self.user.id)["channels"], [])

        # Uma reinicialização do monitor atualiza metadados, mas não reativa o canal removido.
        self.catalog.register_configured_channel(
            telegram_chat_id="-1001234567890",
            title="Fornecedor atualizado",
            username="FornecedorSecreto",
            content_protected=True,
            history_accessible=True,
            last_message_id=11,
        )
        self.assertFalse(self.catalog.is_active_chat("-1001234567890"))
        self.assertEqual(self.catalog.set_channel_status(channel_id, "active"), "active")
        self.assertTrue(self.catalog.is_active_chat("-1001234567890"))

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
            self.catalog.set_selection_mode(self.user.id, "all")
            self.catalog.set_selection_mode(self.user.id, "custom")
            self.assertFalse(self.catalog.toggle_subscription(self.user.id, channel_id))
            self.catalog.set_selection_mode(bob.id, "all")

            selected = accounts.accounts_for_approved_users("-1001234567890")
            self.assertEqual([account.user_id for account, _profile in selected], [bob.id])
            self.assertEqual(len(accounts.accounts_for_approved_users("-1009999999999")), 0)
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

    async def test_aprovacao_propaga_para_instancia_peer_ja_confirmada(self) -> None:
        peer_database_path = Path(self.temp_dir.name) / "peer.sqlite3"
        peer_catalog = ChannelCatalogService(peer_database_path)
        peer_user = UserRepository(peer_database_path).get_or_create_user(303, "carol")
        peer_catalog.submit_request(peer_user.id, "@SNIPERGOLD_SIGNALS")
        await validate_pending_channels_once(
            FakeTelegramClient(left=False),
            peer_catalog,
            CaptureLogger(),
        )
        peer_channel_id = int(
            peer_catalog.admin_channels()["requests"][0]["source_channel_id"]
        )
        self.assertEqual(
            peer_catalog.admin_channels()["channels"][0]["status"],
            "pending_review",
        )

        catalog = ChannelCatalogService(
            self.database_path,
            peer_database_paths=(peer_database_path,),
        )
        request = catalog.submit_request(self.user.id, "@SNIPERGOLD_SIGNALS")
        await validate_pending_channels_once(
            FakeTelegramClient(left=False),
            catalog,
            CaptureLogger(),
        )
        channel_id = catalog.approve_request(int(request.request_id), 9001)
        catalog.set_display_name(channel_id, "Sala Ouro Premium")

        peer_channel = peer_catalog.admin_channels()["channels"][0]
        self.assertEqual(peer_channel["id"], peer_channel_id)
        self.assertEqual(peer_channel["status"], "active")
        self.assertEqual(peer_channel["display_name"], "Sala Ouro Premium")

    async def test_aprovacao_nao_ativa_peer_que_ainda_nao_confirmou_acesso(self) -> None:
        peer_database_path = Path(self.temp_dir.name) / "peer.sqlite3"
        peer_catalog = ChannelCatalogService(peer_database_path)
        peer_user = UserRepository(peer_database_path).get_or_create_user(303, "carol")
        peer_request = peer_catalog.submit_request(peer_user.id, "@SNIPERGOLD_SIGNALS")

        catalog = ChannelCatalogService(
            self.database_path,
            peer_database_paths=(peer_database_path,),
        )
        request = catalog.submit_request(self.user.id, "@SNIPERGOLD_SIGNALS")
        await validate_pending_channels_once(
            FakeTelegramClient(left=False),
            catalog,
            CaptureLogger(),
        )
        catalog.approve_request(int(request.request_id), 9001)

        # A conta tecnica do peer ainda nao confirmou acesso ao canal: a
        # aprovacao nao pode ser propagada, apenas registrada localmente aqui.
        peer_pending = peer_catalog.admin_channels()["requests"]
        self.assertEqual(peer_pending[0]["id"], int(peer_request.request_id))
        self.assertEqual(peer_pending[0]["status"], "pending_access")

    async def test_resync_reconcilia_canais_ja_divergentes_antes_da_sincronizacao(self) -> None:
        # Simula o cenario real: Instituto e Robo Braba aprovaram e apelidaram
        # o mesmo canal de forma independente antes de configurar
        # PEER_CHANNEL_SYNC_DATABASES.
        peer_database_path = Path(self.temp_dir.name) / "peer.sqlite3"
        peer_catalog = ChannelCatalogService(peer_database_path)
        peer_user = UserRepository(peer_database_path).get_or_create_user(303, "carol")
        peer_catalog.submit_request(peer_user.id, "@SNIPERGOLD_SIGNALS")
        await validate_pending_channels_once(
            FakeTelegramClient(left=False),
            peer_catalog,
            CaptureLogger(),
        )
        # O peer ja tinha confirmado acesso, mas nunca chegou a aprovar: fica
        # pendente ate o resync desta instancia empurrar o estado atual.

        catalog = ChannelCatalogService(
            self.database_path,
            peer_database_paths=(peer_database_path,),
        )
        request = catalog.submit_request(self.user.id, "@SNIPERGOLD_SIGNALS")
        await validate_pending_channels_once(
            FakeTelegramClient(left=False),
            catalog,
            CaptureLogger(),
        )
        # Aprovado ANTES de existir sincronizacao (sem peer_database_paths),
        # como aconteceu de fato nas duas instancias em producao.
        ChannelCatalogService(self.database_path).approve_request(
            int(request.request_id), 9001
        )
        catalog.set_display_name(
            int(catalog.admin_channels()["channels"][0]["id"]), "Sala Ouro Premium"
        )

        report = catalog.resync_active_channels_with_peers()

        self.assertEqual(len(report), 1)
        self.assertEqual(report[0]["peers"], [{"peer": str(peer_database_path), "outcome": "activated"}])
        peer_channel = peer_catalog.admin_channels()["channels"][0]
        self.assertEqual(peer_channel["status"], "active")
        self.assertEqual(peer_channel["display_name"], "Sala Ouro Premium")

    async def test_resync_reporta_canal_que_peer_ainda_nao_conhece(self) -> None:
        peer_database_path = Path(self.temp_dir.name) / "peer.sqlite3"
        ChannelCatalogService(peer_database_path)  # inicializa o schema, sem canais

        catalog = ChannelCatalogService(
            self.database_path,
            peer_database_paths=(peer_database_path,),
        )
        request = catalog.submit_request(self.user.id, "@SNIPERGOLD_SIGNALS")
        await validate_pending_channels_once(
            FakeTelegramClient(left=False),
            catalog,
            CaptureLogger(),
        )
        catalog.approve_request(int(request.request_id), 9001)

        report = catalog.resync_active_channels_with_peers()

        self.assertEqual(report[0]["peers"], [{"peer": str(peer_database_path), "outcome": "not_found"}])

    def test_propagacao_para_peer_indisponivel_nao_quebra_aprovacao_local(self) -> None:
        catalog = ChannelCatalogService(
            self.database_path,
            peer_database_paths=(Path(self.temp_dir.name),),  # diretorio, nao arquivo
        )
        channel_id = catalog.register_configured_channel(
            telegram_chat_id="-1001234567890",
            title="Canal principal",
            username="CanalPrincipal",
            content_protected=False,
            history_accessible=True,
            last_message_id=1,
        )

        self.assertEqual(catalog.set_display_name(channel_id, "Sala X"), "Sala X")

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
        alias_result = panel.update_channel_display_name(
            admin_telegram_user_id=9001,
            channel_id=int(result["channel_id"]),
            display_name="Sala Premium A",
        )
        self.assertEqual(alias_result["display_name"], "Sala Premium A")
        self.assertEqual(
            self.catalog.user_overview(self.user.id)["channels"][0]["title"],
            "Sala Premium A",
        )
        self.assertEqual(panel.dashboard()["summary"]["active_channels"], 1)
        self.assertIn("Canais de sinais", render_admin_panel())
        self.assertIn("/api/admin/channel-", render_admin_script())
        self.assertIn("/api/admin/channel-display-name", render_admin_script())
        self.assertIn("/api/admin/channel-status", render_admin_script())
