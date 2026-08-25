from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
import tempfile
import unittest

from telegram_mt5_copier.credential_service import CredentialService
from telegram_mt5_copier.database import SignalDatabase, connect_database, utc_now
from telegram_mt5_copier.daily_schedule import SAO_PAULO_TIMEZONE
from telegram_mt5_copier.mt5.account_service import MT5AccountForm, MT5AccountService
from telegram_mt5_copier.mt5.client import SimulatedMT5Client
from telegram_mt5_copier.mt5.execution_repository import ExecutionRepository
from telegram_mt5_copier.mt5.models import (
    ACCOUNT_MODE_NETTING,
    ACCOUNT_TYPE_REAL,
    CONNECTION_STATUS_DISCONNECTED,
    ENTRY_EXECUTION_MARKET_IMMEDIATE,
    ENTRY_PRICE_DISTRIBUTED,
    ENTRY_PRICE_MIDDLE,
    PendingOrderType,
    SymbolInfo,
    TickInfo,
)
from telegram_mt5_copier.mt5.order_type_resolver import resolve_order_type
from telegram_mt5_copier.mt5.pending_order_executor import (
    MT5_MAGIC_NUMBER,
    PendingOrderExecutor,
    format_rejection_message,
)
from telegram_mt5_copier.mt5.pending_order_monitor import PendingOrderMonitor
from telegram_mt5_copier.mt5.position_manager import PositionManager
from telegram_mt5_copier.mt5.settlement_monitor import SettlementMonitor
from telegram_mt5_copier.mt5.pending_order_planner import (
    PendingOrderPlanner,
    PendingOrderPlanningError,
)
from telegram_mt5_copier.mt5.symbol_resolver import SymbolResolver
from telegram_mt5_copier.mt5.terminal_manager import TerminalManager
from telegram_mt5_copier.mt5.trade_comment import (
    build_trade_comment,
    parse_trade_comment,
)
from telegram_mt5_copier.mt5.volume_allocator import VolumeAllocationError, allocate_volume
from telegram_mt5_copier.parser import parse_signal_text
from telegram_mt5_copier.users import USER_STATUS_ACTIVE, USER_STATUS_PAUSED, UserRepository
from tests.access_helpers import grant_paid_access


BUY_SIGNAL = """XAUUSD BUY
ENTRY 4061-59
SL 4044
TP 4066
TP 4071
TP 4076
TP 4096
"""

SELL_SIGNAL = """XAUUSD SELL
ENTRY 4059-4061
SL 4070
TP 4054
TP 4050
TP 4045
TP 4040
"""


class StrictSymbolClient:
    def __init__(
        self,
        names: tuple[str, ...],
        *,
        disabled: tuple[str, ...] = (),
    ) -> None:
        self.names = names
        self.disabled = disabled

    def symbol_info(self, symbol: str) -> SymbolInfo | None:
        return (
            SymbolInfo(name=symbol, trade_allowed=symbol not in self.disabled)
            if symbol in self.names
            else None
        )

    def symbol_names(self, _group: str | None = None) -> tuple[str, ...]:
        return self.names


class SymbolResolverTests(unittest.TestCase):
    def test_resolve_sufixo_b_da_hfm(self) -> None:
        resolver = SymbolResolver(StrictSymbolClient(("XAUUSDb",)))

        self.assertEqual(resolver.resolve("XAUUSD"), "XAUUSDb")

    def test_descobre_sufixo_desconhecido_da_corretora(self) -> None:
        resolver = SymbolResolver(StrictSymbolClient(("EURUSD", "XAUUSD.pro")))

        self.assertEqual(resolver.resolve("XAUUSD"), "XAUUSD.pro")

    def test_prefere_sufixo_negociavel_ao_simbolo_base_desabilitado(self) -> None:
        resolver = SymbolResolver(
            StrictSymbolClient(
                ("XAUUSD", "XAUUSDb"),
                disabled=("XAUUSD",),
            )
        )

        self.assertEqual(resolver.resolve("XAUUSD"), "XAUUSDb")

    def test_aceita_gold_como_nome_do_ativo_na_corretora(self) -> None:
        resolver = SymbolResolver(StrictSymbolClient(("GOLD",)))

        self.assertEqual(resolver.resolve("XAUUSD"), "GOLD")

    def test_aceita_gold_spot_como_nome_do_ativo_na_fxglobe(self) -> None:
        resolver = SymbolResolver(StrictSymbolClient(("Gold_Spot",)))

        self.assertEqual(resolver.resolve("XAUUSD"), "Gold_Spot")

    def test_descobre_sufixo_da_corretora_em_par_forex(self) -> None:
        resolver = SymbolResolver(StrictSymbolClient(("CADCHFb",)))

        self.assertEqual(resolver.resolve("CADCHF"), "CADCHFb")


class PendingOrderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.database_path = self.root / "pending.sqlite3"
        self.users = UserRepository(self.database_path)
        self.credential_service = CredentialService(CredentialService.generate_key())
        self.accounts = MT5AccountService(
            self.database_path,
            credential_service=self.credential_service,
            terminal_manager=TerminalManager(self.root / "mt5"),
            client_factory=lambda: SimulatedMT5Client(),
        )
        self.user = self.users.get_or_create_user(101, "alice")
        grant_paid_access(self.database_path, self.user.id)
        self.account = self.accounts.register_account(
            self.user.id,
            MT5AccountForm("Broker", "Broker-Demo", "12345678", "secret", "Demo"),
        )
        self.accounts.update_execution_profile_fixed_lot(self.user.id, self.account.id, Decimal("0.04"))

    def tearDown(self) -> None:
        self.accounts.close()
        self.users.close()
        self.temp_dir.cleanup()

    def profile(self):
        profile = self.accounts.get_execution_profile(self.user.id, self.account.id)
        self.assertIsNotNone(profile)
        return profile

    def create_extra_account(self, login: str):
        account = self.accounts.register_account(
            self.user.id,
            MT5AccountForm("Broker", "Broker-Demo", login, "secret", f"Demo {login}"),
        )
        self.accounts.update_execution_profile_fixed_lot(self.user.id, account.id, Decimal("0.04"))
        return account

    def plan_buy(self, tick: TickInfo):
        signal = parse_signal_text(BUY_SIGNAL).signal
        return PendingOrderPlanner().plan(
            signal=signal,
            account=self.account,
            profile=self.profile(),
            symbol_info=SymbolInfo(name="XAUUSD"),
            tick=tick,
            execution_mode="simulation",
            now=datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc),
        )

    def test_buy_abaixo_do_mercado_gera_buy_limit(self) -> None:
        plan = self.plan_buy(TickInfo(bid=Decimal("4062"), ask=Decimal("4062")))

        self.assertEqual(plan.selected_entry_price, Decimal("4061.00"))
        self.assertEqual(plan.order_type, PendingOrderType.BUY_LIMIT)

    def test_comentario_mt5_exibe_sala_e_preserva_identificacao(self) -> None:
        comment = build_trade_comment("Gold Precision", "6e87346abcdef", 2)

        self.assertEqual(comment, "Gold Precision 6e87346a T2")
        self.assertLessEqual(len(comment), 31)
        parsed = parse_trade_comment(comment)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.signal_prefix, "6e87346a")
        self.assertEqual(parsed.tp_index, 2)

    def test_execucao_usa_nome_publico_da_sala_no_comentario_mt5(self) -> None:
        signal = replace(
            parse_signal_text(BUY_SIGNAL).signal,
            source_chat_id=-1001234567890,
            source_message_id=55,
        )
        now = utc_now()
        with connect_database(self.database_path) as connection:
            connection.execute(
                """
                INSERT INTO source_channels (
                    telegram_chat_id,title,display_name,status,access_status,
                    created_at,updated_at
                ) VALUES (?,?,?,?,?,?,?)
                """,
                (
                    str(signal.source_chat_id),
                    "Nome secreto",
                    "Gold Alpha",
                    "active",
                    "confirmed",
                    now,
                    now,
                ),
            ).close()
        SignalDatabase(self.database_path).record_accepted(signal, "sinal")
        client = SimulatedMT5Client()

        self.executor(client=client, execution_mode="demo_execution").execute_for_account(
            signal,
            self.account,
            self.profile(),
        )

        self.assertTrue(client.order_send_requests)
        self.assertTrue(
            all(
                str(request["comment"]).startswith("Gold Alpha ")
                for request in client.order_send_requests
            )
        )
        self.assertTrue(
            all(len(str(request["comment"])) <= 31 for request in client.order_send_requests)
        )

    def test_plano_e_ordens_preservam_xauusdb_resolvido_na_hfm(self) -> None:
        signal = parse_signal_text(BUY_SIGNAL).signal
        plan = PendingOrderPlanner().plan(
            signal=signal,
            account=self.account,
            profile=self.profile(),
            symbol_info=SymbolInfo(name="XAUUSDb"),
            tick=TickInfo(bid=Decimal("4062"), ask=Decimal("4062")),
            execution_mode="live_execution",
            now=datetime(2026, 7, 23, 12, 0, tzinfo=timezone.utc),
        )

        self.assertEqual(plan.symbol, "XAUUSDb")

    def test_execucao_forex_resolve_sufixo_e_preserva_precisao(self) -> None:
        signal = parse_signal_text(
            "CADCHF SELL\nENTRY @ 0.58217\nSL 0.58304\n"
            "TP1 0.58136\nTP2 0.58041\nTP3 0.57956"
        ).signal
        client = SimulatedMT5Client(
            symbol_info=SymbolInfo(
                name="CADCHFb",
                point=Decimal("0.00001"),
                trade_tick_size=Decimal("0.00001"),
                digits=5,
            ),
            tick=TickInfo(bid=Decimal("0.58200"), ask=Decimal("0.58202")),
        )

        result = self.executor(client=client).execute_for_account(
            signal, self.account, self.profile()
        )

        self.assertIsNone(result.group_result.rejected_reason)
        self.assertEqual(result.group_result.group.symbol, "CADCHFb")
        self.assertEqual(result.group_result.group.order_type, "SELL_LIMIT")
        self.assertEqual(result.group_result.group.selected_entry_price, Decimal("0.58217"))

    def test_buy_acima_do_mercado_gera_buy_stop(self) -> None:
        plan = self.plan_buy(TickInfo(bid=Decimal("4058"), ask=Decimal("4058")))

        self.assertEqual(plan.selected_entry_price, Decimal("4059.00"))
        self.assertEqual(plan.order_type, PendingOrderType.BUY_STOP)

    def test_sell_acima_do_mercado_gera_sell_limit(self) -> None:
        signal = parse_signal_text(SELL_SIGNAL).signal
        plan = PendingOrderPlanner().plan(
            signal=signal,
            account=self.account,
            profile=self.profile(),
            symbol_info=SymbolInfo(name="XAUUSD"),
            tick=TickInfo(bid=Decimal("4058"), ask=Decimal("4058")),
            execution_mode="simulation",
            now=datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc),
        )

        self.assertEqual(plan.order_type, PendingOrderType.SELL_LIMIT)

    def test_sell_abaixo_do_mercado_gera_sell_stop(self) -> None:
        signal = parse_signal_text(SELL_SIGNAL).signal
        plan = PendingOrderPlanner().plan(
            signal=signal,
            account=self.account,
            profile=self.profile(),
            symbol_info=SymbolInfo(name="XAUUSD"),
            tick=TickInfo(bid=Decimal("4062"), ask=Decimal("4062")),
            execution_mode="simulation",
            now=datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc),
        )

        self.assertEqual(plan.order_type, PendingOrderType.SELL_STOP)

    def test_entrada_a_mercado_dentro_da_zona_com_um_tp(self) -> None:
        signal = parse_signal_text(BUY_SIGNAL).signal
        profile = replace(
            self.profile(),
            entry_execution_mode="market_on_zone",
            split_tps=False,
        )
        plan = PendingOrderPlanner().plan(
            signal=signal,
            account=self.account,
            profile=profile,
            symbol_info=SymbolInfo(name="XAUUSD"),
            tick=TickInfo(bid=Decimal("4060"), ask=Decimal("4060")),
            execution_mode="live_execution",
        )

        self.assertEqual(plan.order_type, PendingOrderType.BUY)
        self.assertEqual(plan.selected_entry_price, Decimal("4060.00"))
        self.assertEqual(len(plan.orders), 1)

    def test_entrada_imediata_usa_mercado_fora_da_zona_com_multiplos_tps(self) -> None:
        signal = parse_signal_text(BUY_SIGNAL).signal
        profile = replace(
            self.profile(),
            entry_execution_mode=ENTRY_EXECUTION_MARKET_IMMEDIATE,
        )
        plan = PendingOrderPlanner().plan(
            signal=signal,
            account=self.account,
            profile=profile,
            symbol_info=SymbolInfo(name="XAUUSD"),
            tick=TickInfo(bid=Decimal("4062"), ask=Decimal("4062")),
            execution_mode="live_execution",
        )

        self.assertEqual(plan.order_type, PendingOrderType.BUY)
        self.assertEqual(plan.selected_entry_price, Decimal("4062.00"))
        self.assertEqual(len(plan.orders), 4)
        self.assertTrue(
            all(order.order_type == PendingOrderType.BUY for order in plan.orders)
        )
        self.assertTrue(
            all(order.entry_price == Decimal("4062.00") for order in plan.orders)
        )

    def test_entrada_imediata_sell_usa_bid_atual_fora_da_zona(self) -> None:
        signal = parse_signal_text(SELL_SIGNAL).signal
        profile = replace(
            self.profile(),
            entry_execution_mode=ENTRY_EXECUTION_MARKET_IMMEDIATE,
        )
        plan = PendingOrderPlanner().plan(
            signal=signal,
            account=self.account,
            profile=profile,
            symbol_info=SymbolInfo(name="XAUUSD"),
            tick=TickInfo(bid=Decimal("4055"), ask=Decimal("4055.20")),
            execution_mode="live_execution",
        )

        self.assertEqual(plan.order_type, PendingOrderType.SELL)
        self.assertEqual(plan.selected_entry_price, Decimal("4055.00"))
        self.assertTrue(
            all(order.order_type == PendingOrderType.SELL for order in plan.orders)
        )

    def test_entrada_imediata_rejeita_quando_preco_ja_passou_tp1(self) -> None:
        signal = parse_signal_text(BUY_SIGNAL).signal
        profile = replace(
            self.profile(),
            entry_execution_mode=ENTRY_EXECUTION_MARKET_IMMEDIATE,
        )
        client = SimulatedMT5Client(
            tick=TickInfo(bid=Decimal("4067"), ask=Decimal("4067"))
        )

        result = self.executor(client=client).execute_for_account(
            signal,
            self.account,
            profile,
        )

        self.assertEqual(
            result.group_result.rejected_reason,
            "buy_take_profit_not_above_entry",
        )

    def test_preco_first_touch_middle_e_distributed(self) -> None:
        first_touch = self.plan_buy(TickInfo(bid=Decimal("4062"), ask=Decimal("4062")))
        self.accounts.update_execution_profile_field(self.user.id, self.account.id, "entry_price_mode", ENTRY_PRICE_MIDDLE)
        middle = self.plan_buy(TickInfo(bid=Decimal("4062"), ask=Decimal("4062")))
        self.accounts.update_execution_profile_field(
            self.user.id,
            self.account.id,
            "entry_price_mode",
            ENTRY_PRICE_DISTRIBUTED,
        )
        distributed = self.plan_buy(TickInfo(bid=Decimal("4062"), ask=Decimal("4062")))

        self.assertEqual(first_touch.selected_entry_price, Decimal("4061.00"))
        self.assertEqual(middle.selected_entry_price, Decimal("4060.00"))
        self.assertEqual(distributed.orders[0].entry_price, Decimal("4059.00"))
        self.assertEqual(distributed.orders[-1].entry_price, Decimal("4061.00"))
        self.assertEqual(len({order.entry_price for order in distributed.orders}), 4)

    def test_cliente_escolhe_quantidade_de_take_profits(self) -> None:
        self.accounts.update_execution_profile_field(
            self.user.id,
            self.account.id,
            "take_profit_limit",
            2,
        )

        plan = self.plan_buy(TickInfo(bid=Decimal("4062"), ask=Decimal("4062")))

        self.assertEqual(len(plan.orders), 2)
        self.assertEqual(
            tuple(order.take_profit for order in plan.orders),
            (Decimal("4066"), Decimal("4071")),
        )
        self.assertEqual(
            tuple(order.normalized_volume for order in plan.orders),
            (Decimal("0.02"), Decimal("0.02")),
        )

    def test_divisao_004_em_quatro_ordens_de_001(self) -> None:
        volumes = allocate_volume(Decimal("0.04"), 4, SymbolInfo(name="XAUUSD"))

        self.assertEqual(volumes, (Decimal("0.01"), Decimal("0.01"), Decimal("0.01"), Decimal("0.01")))

    def test_risco_percentual_calcula_lote_total_pelo_stop(self) -> None:
        signal = parse_signal_text(BUY_SIGNAL).signal
        profile = replace(self.profile(), risk_mode="risk_percent", risk_percent=Decimal("1"))
        plan = PendingOrderPlanner().plan(
            signal=signal,
            account=replace(self.account, equity=Decimal("10000")),
            profile=profile,
            symbol_info=SymbolInfo(
                name="XAUUSD",
                trade_tick_size=Decimal("0.01"),
                trade_tick_value=Decimal("1"),
            ),
            tick=TickInfo(bid=Decimal("4062"), ask=Decimal("4062")),
            execution_mode="live_execution",
        )

        self.assertEqual(plan.total_volume, Decimal("0.05"))
        self.assertEqual(sum(order.normalized_volume for order in plan.orders), Decimal("0.05"))

    def test_risco_percentual_rejeita_quando_lote_minimo_excede_limite(self) -> None:
        signal = parse_signal_text(BUY_SIGNAL).signal
        profile = replace(
            self.profile(),
            risk_mode="risk_percent",
            risk_percent=Decimal("0.5"),
        )

        with self.assertRaises(PendingOrderPlanningError) as context:
            PendingOrderPlanner().plan(
                signal=signal,
                account=replace(self.account, equity=Decimal("2045.38")),
                profile=profile,
                symbol_info=SymbolInfo(
                    name="XAUUSD",
                    trade_tick_size=Decimal("0.01"),
                    trade_tick_value=Decimal("1"),
                ),
                tick=TickInfo(bid=Decimal("4062"), ask=Decimal("4062")),
                execution_mode="live_execution",
            )

        self.assertEqual(
            context.exception.reason,
            "Risco calculado gera lote abaixo do minimo do simbolo.",
        )

        message = format_rejection_message(
            context.exception.reason,
            replace(self.account, equity=Decimal("2045.38")),
            profile=profile,
            symbol_info=SymbolInfo(name="XAUUSD", volume_min=Decimal("0.01")),
            plan=context.exception.plan,
        )
        self.assertIn("Risco configurado: 0.5%", message)
        self.assertIn("Limite estimado por sinal: $ 10.23", message)
        self.assertIn("O lote necessário ficou abaixo do mínimo 0.01", message)
        self.assertIn("continuará aguardando o próximo", message)

    def test_spread_e_limite_diario_bloqueiam_antes_do_order_send(self) -> None:
        wide_spread_client = SimulatedMT5Client(
            tick=TickInfo(bid=Decimal("4050"), ask=Decimal("4060")),
            symbol_info=SymbolInfo(name="XAUUSD", point=Decimal("0.01")),
        )
        spread_result = self.executor(
            client=wide_spread_client,
            execution_mode="demo_execution",
            global_kill_switch=False,
        ).execute_for_account(parse_signal_text(BUY_SIGNAL).signal, self.account, self.profile())
        self.assertEqual(spread_result.group_result.rejected_reason, "max_spread_exceeded")
        self.assertFalse(wide_spread_client.order_send_called)

        loss_profile = replace(self.profile(), daily_loss_limit=Decimal("50"))
        loss_client = SimulatedMT5Client(history_deals=({"magic": 27071301, "profit": -60},))
        loss_result = self.executor(
            client=loss_client,
            execution_mode="demo_execution",
            global_kill_switch=False,
        ).execute_for_account(parse_signal_text(BUY_SIGNAL).signal, self.account, loss_profile)
        self.assertEqual(loss_result.group_result.rejected_reason, "daily_loss_limit_reached")
        self.assertFalse(loss_client.order_send_called)

    def test_meta_diaria_pausa_novos_sinais_ate_as_23h(self) -> None:
        target_profile = replace(self.profile(), daily_profit_target=Decimal("50"))
        target_client = SimulatedMT5Client(
            history_deals=({"magic": MT5_MAGIC_NUMBER, "profit": 60},)
        )

        result = self.executor(
            client=target_client,
            execution_mode="demo_execution",
            global_kill_switch=False,
        ).execute_for_account(
            parse_signal_text(BUY_SIGNAL).signal,
            self.account,
            target_profile,
        )

        paused_user = self.users.get_by_id(self.user.id)
        pause_until = datetime.fromisoformat(paused_user.daily_signal_pause_until)
        self.assertEqual(result.group_result.rejected_reason, "daily_profit_target_reached")
        self.assertEqual(pause_until.astimezone(SAO_PAULO_TIMEZONE).hour, 23)
        self.assertEqual(
            self.executor().execute_for_signal(parse_signal_text(BUY_SIGNAL).signal),
            [],
        )

    def test_meta_em_uma_conta_impede_outra_conta_do_mesmo_usuario(self) -> None:
        self.create_extra_account("87654321")
        with connect_database(self.database_path) as connection:
            connection.execute(
                "UPDATE execution_profiles SET daily_profit_target = 50 WHERE user_id = ?",
                (self.user.id,),
            )
        target_client = SimulatedMT5Client(
            history_deals=({"magic": MT5_MAGIC_NUMBER, "profit": 60},)
        )

        results = self.executor(
            client=target_client,
            execution_mode="demo_execution",
            global_kill_switch=False,
        ).execute_for_signal(parse_signal_text(BUY_SIGNAL).signal)

        self.assertEqual(len(results), 1)
        self.assertEqual(
            results[0].group_result.rejected_reason,
            "daily_profit_target_reached",
        )

    def test_rejeicao_003_para_quatro_tps_quando_minimo_001(self) -> None:
        with self.assertRaises(VolumeAllocationError):
            allocate_volume(Decimal("0.03"), 4, SymbolInfo(name="XAUUSD"))

    def test_normalizacao_por_volume_step_distribui_sobra(self) -> None:
        volumes = allocate_volume(Decimal("0.05"), 2, SymbolInfo(name="XAUUSD"))

        self.assertEqual(volumes, (Decimal("0.03"), Decimal("0.02")))
        self.assertEqual(sum(volumes), Decimal("0.05"))

    def test_resolvedor_de_tipo_de_ordem(self) -> None:
        resolution = resolve_order_type(
            direction=parse_signal_text(BUY_SIGNAL).signal.direction,
            entry_price=Decimal("4059"),
            current_price=Decimal("4062"),
            entry_low=Decimal("4059"),
            entry_high=Decimal("4061"),
            entry_execution_mode="pending_order",
        )

        self.assertEqual(resolution.order_type, PendingOrderType.BUY_LIMIT)

    def test_buy_com_sl_invalido_rejeita(self) -> None:
        signal = parse_signal_text("XAUUSD BUY\nENTRY 4059-4061\nSL 4062\nTP 4066").signal
        result = self.executor().execute_for_account(signal, self.account, self.profile())

        self.assertEqual(result.group_result.rejected_reason, "buy_stop_loss_not_below_entry")
        self.assertEqual(count_execution_orders(self.database_path), 0)

    def test_sell_com_sl_invalido_rejeita(self) -> None:
        signal = parse_signal_text("XAUUSD SELL\nENTRY 4059-4061\nSL 4058\nTP 4050").signal
        result = self.executor(tick=TickInfo(bid=Decimal("4062"), ask=Decimal("4062"))).execute_for_account(
            signal,
            self.account,
            self.profile(),
        )

        self.assertEqual(result.group_result.rejected_reason, "sell_stop_loss_not_above_entry")
        self.assertEqual(count_execution_orders(self.database_path), 0)

    def test_ordem_expirada(self) -> None:
        self.accounts.update_execution_profile_field(self.user.id, self.account.id, "pending_expiration_minutes", 1)
        signal = parse_signal_text(BUY_SIGNAL).signal
        planner = PendingOrderPlanner()
        old_plan = planner.plan(
            signal=signal,
            account=self.account,
            profile=self.profile(),
            symbol_info=SymbolInfo(name="XAUUSD"),
            tick=TickInfo(bid=Decimal("4062"), ask=Decimal("4062")),
            execution_mode="simulation",
            now=datetime.now(tz=timezone.utc) - timedelta(minutes=5),
        )
        from telegram_mt5_copier.mt5.order_validator import OrderValidationError, validate_pending_order_plan

        with self.assertRaises(OrderValidationError):
            validate_pending_order_plan(
                plan=old_plan,
                account=self.account,
                symbol_info=SymbolInfo(name="XAUUSD"),
                tick=TickInfo(bid=Decimal("4062"), ask=Decimal("4062")),
                execution_mode="simulation",
            )

    def test_preco_atingiu_sl_ou_tp_antes_da_entrada(self) -> None:
        signal = parse_signal_text(BUY_SIGNAL).signal
        tp_account = self.create_extra_account("445566")
        sl_result = self.executor(tick=TickInfo(bid=Decimal("4040"), ask=Decimal("4040"))).execute_for_account(
            signal,
            self.account,
            self.profile(),
        )
        tp_result = self.executor(tick=TickInfo(bid=Decimal("4070"), ask=Decimal("4070"))).execute_for_account(
            signal,
            tp_account,
            self.accounts.get_execution_profile(self.user.id, tp_account.id),
        )

        self.assertEqual(sl_result.group_result.rejected_reason, "price_hit_sl_before_entry")
        self.assertEqual(tp_result.group_result.rejected_reason, "price_hit_tp_before_entry")

    def test_usuario_pausado_nao_executa(self) -> None:
        paused_user = self.users.get_or_create_user(202, "bob")
        account = self.accounts.register_account(
            paused_user.id,
            MT5AccountForm("Broker", "Broker-Demo", "998877", "secret", "Bob"),
        )
        self.accounts.update_execution_profile_fixed_lot(paused_user.id, account.id, Decimal("0.04"))

        results = self.executor().execute_for_signal(parse_signal_text(BUY_SIGNAL).signal)

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].account.user_id, self.user.id)

    def test_acesso_expirado_bloqueia_novos_sinais_sem_desligar_worker(self) -> None:
        with connect_database(self.database_path) as connection:
            connection.execute(
                "UPDATE customer_billing SET due_date = '2020-01-01' WHERE user_id = ?",
                (self.user.id,),
            )

        results = self.executor().execute_for_signal(parse_signal_text(BUY_SIGNAL).signal)

        self.assertEqual(results, [])
        self.assertEqual(len(self.accounts.accounts_for_approved_users()), 0)
        self.assertEqual(len(self.accounts.accounts_for_active_users()), 1)

    def test_parada_diaria_bloqueia_novos_sinais_sem_desligar_worker(self) -> None:
        future = (datetime.now(tz=timezone.utc) + timedelta(hours=2)).isoformat()
        past = (datetime.now(tz=timezone.utc) - timedelta(minutes=1)).isoformat()

        self.users.set_daily_signal_pause_until(self.user.id, future)
        blocked = self.executor().execute_for_signal(
            parse_signal_text(BUY_SIGNAL).signal
        )

        self.assertEqual(blocked, [])
        self.assertEqual(len(self.accounts.accounts_for_approved_users()), 0)
        self.assertEqual(len(self.accounts.accounts_for_active_users()), 1)

        self.users.set_daily_signal_pause_until(self.user.id, past)
        self.assertEqual(len(self.accounts.accounts_for_approved_users()), 1)

    def test_usuario_pausado_mantem_worker_enquanto_existe_ordem_aberta(self) -> None:
        result = self.executor().execute_for_account(
            parse_signal_text(BUY_SIGNAL).signal,
            self.account,
            self.profile(),
        )
        with connect_database(self.database_path) as connection:
            connection.execute(
                "UPDATE execution_orders SET status='filled' "
                "WHERE execution_group_id=? AND tp_index=1",
                (result.group_result.group.id,),
            ).close()
        self.users.set_status(self.user.id, "paused")

        active = self.accounts.accounts_for_active_users()

        self.assertEqual(len(active), 1)
        self.assertEqual(active[0][0].id, self.account.id)

    def test_kill_switch_bloqueia_execucao_demo(self) -> None:
        client = SimulatedMT5Client()
        result = self.executor(
            client=client,
            execution_mode="demo_execution",
            global_kill_switch=True,
        ).execute_for_account(
            parse_signal_text(BUY_SIGNAL).signal,
            self.account,
            self.profile(),
        )

        self.assertEqual(result.group_result.rejected_reason, "kill_switch_enabled")
        self.assertFalse(client.initialized)

    def test_conta_desconectada_e_conta_real_bloqueadas(self) -> None:
        signal = parse_signal_text(BUY_SIGNAL).signal
        disconnected = replace(self.account, connection_status=CONNECTION_STATUS_DISCONNECTED)
        real = replace(self.create_extra_account("112233"), account_type=ACCOUNT_TYPE_REAL)

        disconnected_result = self.executor().execute_for_account(signal, disconnected, self.profile())
        real_result = self.executor().execute_for_account(
            signal,
            real,
            self.accounts.get_execution_profile(self.user.id, real.id),
        )

        self.assertEqual(disconnected_result.group_result.rejected_reason, "account_disconnected")
        self.assertEqual(real_result.group_result.rejected_reason, "real_account_blocked")

    def test_hedging_suportado_e_netting_bloqueado_para_execucao_real(self) -> None:
        signal = parse_signal_text(BUY_SIGNAL).signal
        hedging_result = self.executor().execute_for_account(signal, self.account, self.profile())
        netting = replace(self.create_extra_account("334455"), account_mode=ACCOUNT_MODE_NETTING)
        netting_result = self.executor(
            client=SimulatedMT5Client(account_mode=ACCOUNT_MODE_NETTING),
            execution_mode="demo_execution",
            global_kill_switch=False,
        ).execute_for_account(
            signal,
            netting,
            self.accounts.get_execution_profile(self.user.id, netting.id),
        )

        self.assertEqual(len(hedging_result.group_result.orders), 4)
        self.assertEqual(netting_result.group_result.rejected_reason, "netting_multiple_tps_not_supported")

    def test_simulation_nao_chama_order_send_e_nao_envia_parcial(self) -> None:
        client = SimulatedMT5Client()
        signal = parse_signal_text(BUY_SIGNAL).signal
        ok_result = self.executor(client=client).execute_for_account(signal, self.account, self.profile())
        self.assertFalse(client.order_send_called)
        self.assertEqual(len(ok_result.group_result.orders), 4)
        self.assertIn("XAUUSD — BUY LIMIT", ok_result.message)
        self.assertNotIn("BUY BUY LIMIT", ok_result.message)

        other = self.create_extra_account("556677")
        self.accounts.update_execution_profile_fixed_lot(self.user.id, self.account.id, Decimal("0.03"))
        self.accounts.update_execution_profile_fixed_lot(self.user.id, other.id, Decimal("0.03"))
        invalid_result = self.executor().execute_for_account(
            signal,
            other,
            self.accounts.get_execution_profile(self.user.id, other.id),
        )

        self.assertIsNotNone(invalid_result.group_result.group)
        self.assertEqual(
            invalid_result.group_result.rejected_reason,
            "Lote total insuficiente para dividir entre todos os TPs.",
        )
        self.assertEqual(count_execution_orders(self.database_path), 4)

    def test_demo_execution_roda_order_check_em_todas_e_salva_tickets(self) -> None:
        client = SimulatedMT5Client(tick=TickInfo(bid=Decimal("4062"), ask=Decimal("4062")))
        result = self.executor(
            client=client,
            execution_mode="demo_execution",
            global_kill_switch=False,
        ).execute_for_account(parse_signal_text(BUY_SIGNAL).signal, self.account, self.profile())

        self.assertIsNone(result.group_result.rejected_reason)
        self.assertEqual(len(client.order_check_requests), 4)
        self.assertTrue(
            all(isinstance(request["expiration"], int) for request in client.order_check_requests)
        )
        self.assertEqual(len(client.order_send_requests), 4)
        self.assertEqual(client.shutdown_count, 1)
        self.assertEqual(group_status(self.database_path, result.group_result.group.id), "pending_active")
        rows = execution_order_rows(self.database_path, result.group_result.group.id)
        self.assertEqual([row["status"] for row in rows], ["pending_active"] * 4)
        self.assertEqual([row["ticket"] for row in rows], ["100001", "100002", "100003", "100004"])
        self.assertIn("ORDENS PENDENTES ENVIADAS EM CONTA DEMO", result.message)
        self.assertIn("Gestão aplicada", result.message)
        self.assertIn("Lote fixo total: 0.04", result.message)
        self.assertIn("Orientação: mantenha o lote e o Stop Loss", result.message)

    def test_corretora_com_gtc_nao_recebe_expiration(self) -> None:
        client = SimulatedMT5Client(
            tick=TickInfo(bid=Decimal("4062"), ask=Decimal("4062")),
            symbol_info=SymbolInfo(name="XAUUSD", expiration_mode=1),
        )

        result = self.executor(
            client=client,
            execution_mode="demo_execution",
            global_kill_switch=False,
        ).execute_for_account(parse_signal_text(BUY_SIGNAL).signal, self.account, self.profile())

        self.assertIsNone(result.group_result.rejected_reason)
        self.assertTrue(all(request["type_time"] == 0 for request in client.order_send_requests))
        self.assertTrue(all("expiration" not in request for request in client.order_send_requests))

    def test_corretora_com_gtc_e_specified_prefere_gtc(self) -> None:
        client = SimulatedMT5Client(
            tick=TickInfo(bid=Decimal("4062"), ask=Decimal("4062")),
            symbol_info=SymbolInfo(name="XAUUSD", expiration_mode=5),
        )

        result = self.executor(
            client=client,
            execution_mode="demo_execution",
            global_kill_switch=False,
        ).execute_for_account(parse_signal_text(BUY_SIGNAL).signal, self.account, self.profile())

        self.assertIsNone(result.group_result.rejected_reason)
        self.assertTrue(all(request["type_time"] == 0 for request in client.order_send_requests))
        self.assertTrue(all("expiration" not in request for request in client.order_send_requests))

    def test_demo_execution_nao_envia_parcial_se_order_check_falhar(self) -> None:
        client = SimulatedMT5Client(
            tick=TickInfo(bid=Decimal("4062"), ask=Decimal("4062")),
            order_check_results=[
                {"retcode": 0, "comment": "ok"},
                {"retcode": 10030, "comment": "invalid stops"},
            ],
        )

        result = self.executor(
            client=client,
            execution_mode="demo_execution",
            global_kill_switch=False,
        ).execute_for_account(parse_signal_text(BUY_SIGNAL).signal, self.account, self.profile())

        self.assertEqual(len(client.order_check_requests), 4)
        self.assertEqual(len(client.order_send_requests), 0)
        self.assertEqual(result.group_result.rejected_reason, "order_check_failed:invalid stops")
        self.assertEqual(count_execution_orders(self.database_path), 0)

    def test_order_check_sem_resultado_inclui_last_error_do_mt5(self) -> None:
        client = SimulatedMT5Client(
            tick=TickInfo(bid=Decimal("4062"), ask=Decimal("4062")),
            order_check_results=[None],
        )
        client._last_error = (-2, 'Invalid "expiration" argument')

        result = self.executor(
            client=client,
            execution_mode="demo_execution",
            global_kill_switch=False,
        ).execute_for_account(parse_signal_text(BUY_SIGNAL).signal, self.account, self.profile())

        self.assertIn("order_check_failed:sem_resultado", result.group_result.rejected_reason)
        self.assertIn("Invalid", result.group_result.rejected_reason)
        self.assertEqual(len(client.order_send_requests), 0)

    def test_falha_parcial_tenta_cancelar_ordens_ja_enviadas(self) -> None:
        client = SimulatedMT5Client(
            order_send_results=[
                {"retcode": 10008, "order": 501, "comment": "placed"},
                {"retcode": 10030, "comment": "rejected"},
                {"retcode": 10009, "order": 501, "comment": "removed"},
            ]
        )

        result = self.executor(
            client=client,
            execution_mode="demo_execution",
            global_kill_switch=False,
        ).execute_for_account(parse_signal_text(BUY_SIGNAL).signal, self.account, self.profile())

        self.assertIn("order_send_failed", result.group_result.rejected_reason)
        self.assertEqual(client.order_send_requests[-1]["action"], 8)
        self.assertEqual(client.order_send_requests[-1]["order"], 501)

    def test_entrada_imediata_envia_multiplos_tps_sem_expiracao(self) -> None:
        signal = parse_signal_text(BUY_SIGNAL).signal
        profile = replace(
            self.profile(),
            entry_execution_mode=ENTRY_EXECUTION_MARKET_IMMEDIATE,
        )
        client = SimulatedMT5Client(
            tick=TickInfo(bid=Decimal("4062"), ask=Decimal("4062"))
        )

        result = self.executor(
            client=client,
            execution_mode="demo_execution",
            global_kill_switch=False,
        ).execute_for_account(signal, self.account, profile)

        self.assertIsNone(result.group_result.rejected_reason)
        self.assertEqual(len(client.order_send_requests), 4)
        self.assertTrue(
            all(request["action"] == 1 for request in client.order_send_requests)
        )
        self.assertTrue(
            all("expiration" not in request for request in client.order_send_requests)
        )
        self.assertIn("ORDENS A MERCADO", result.message)

    def test_falha_parcial_a_mercado_tenta_fechar_posicao_aberta(self) -> None:
        signal = parse_signal_text(BUY_SIGNAL).signal
        profile = replace(
            self.profile(),
            entry_execution_mode=ENTRY_EXECUTION_MARKET_IMMEDIATE,
            max_open_signals=10,
        )
        client = SimulatedMT5Client(
            tick=TickInfo(bid=Decimal("4062"), ask=Decimal("4062")),
            positions=(
                {
                    "ticket": 9001,
                    "symbol": "XAUUSD",
                    "volume": 0.01,
                    "type": 0,
                    "magic": MT5_MAGIC_NUMBER,
                    "comment": f"tgcp {signal.signature[:8]} TP1",
                },
            ),
            order_send_results=[
                {"retcode": 10009, "order": 501, "comment": "filled"},
                {"retcode": 10030, "comment": "rejected"},
                {"retcode": 10009, "order": 502, "comment": "closed"},
            ],
        )

        result = self.executor(
            client=client,
            execution_mode="demo_execution",
            global_kill_switch=False,
        ).execute_for_account(signal, self.account, profile)

        self.assertIn("order_send_failed", result.group_result.rejected_reason)
        self.assertIn("partial_rollback_completed", result.group_result.rejected_reason)
        self.assertIn("envio parcial foi revertido", result.message)
        close_request = client.order_send_requests[-1]
        self.assertEqual(close_request["action"], 1)
        self.assertEqual(close_request["position"], 9001)
        self.assertEqual(close_request["type"], 1)

    def test_falha_no_rollback_a_mercado_gera_alerta_explicito(self) -> None:
        signal = parse_signal_text(BUY_SIGNAL).signal
        profile = replace(
            self.profile(),
            entry_execution_mode=ENTRY_EXECUTION_MARKET_IMMEDIATE,
        )
        client = SimulatedMT5Client(
            tick=TickInfo(bid=Decimal("4062"), ask=Decimal("4062")),
            order_send_results=[
                {"retcode": 10009, "order": 501, "comment": "filled"},
                {"retcode": 10030, "comment": "rejected"},
            ],
        )

        result = self.executor(
            client=client,
            execution_mode="demo_execution",
            global_kill_switch=False,
        ).execute_for_account(signal, self.account, profile)

        self.assertIn("rollback_failed:501", result.group_result.rejected_reason)
        self.assertIn("pode existir posição aberta", result.message)

    def test_live_execution_exige_autorizacao_explicita(self) -> None:
        client = SimulatedMT5Client()

        result = self.executor(
            client=client,
            execution_mode="live_execution",
            global_kill_switch=False,
        ).execute_for_account(parse_signal_text(BUY_SIGNAL).signal, self.account, self.profile())

        self.assertEqual(result.group_result.rejected_reason, "live_accounts_not_allowed")
        self.assertEqual(len(client.order_send_requests), 0)

    def test_live_execution_envia_para_conta_real_quando_autorizada(self) -> None:
        client = SimulatedMT5Client(account_type="real")
        real_account = replace(self.account, account_type="real")

        result = self.executor(
            client=client,
            execution_mode="live_execution",
            global_kill_switch=False,
            allow_live_accounts=True,
        ).execute_for_account(parse_signal_text(BUY_SIGNAL).signal, real_account, self.profile())

        self.assertIsNone(result.group_result.rejected_reason)
        self.assertEqual(len(client.order_send_requests), 4)

    def test_live_execution_seleciona_conta_real_ativa_do_banco(self) -> None:
        with connect_database(self.database_path) as connection:
            cursor = connection.execute(
                "UPDATE mt5_accounts SET account_type = 'real' WHERE id = ?",
                (self.account.id,),
            )
            cursor.close()
        client = SimulatedMT5Client(account_type="real")
        executor = self.executor(
            client=client,
            execution_mode="live_execution",
            global_kill_switch=False,
            allow_live_accounts=True,
        )

        results = executor.execute_for_signal(parse_signal_text(BUY_SIGNAL).signal)

        self.assertEqual(len(results), 1)
        self.assertIsNone(results[0].group_result.rejected_reason)
        self.assertEqual(len(client.order_send_requests), 4)

    def test_duplicidade_de_sinal_expiracao_120_e_metricas(self) -> None:
        signal = parse_signal_text(BUY_SIGNAL).signal
        first = self.executor().execute_for_account(signal, self.account, self.profile())
        second = self.executor().execute_for_account(signal, self.account, self.profile())

        self.assertFalse(first.group_result.duplicate)
        self.assertTrue(second.group_result.duplicate)
        self.assertEqual(count_execution_orders(self.database_path), 4)
        self.assertEqual(minutes_until_expiration(self.database_path), 120)
        self.assertIsNotNone(total_latency_ms(self.database_path))

    def test_mesmo_sinal_de_salas_diferentes_nao_duplica_na_conta(self) -> None:
        first_signal = parse_signal_text(
            BUY_SIGNAL,
            source_chat_id="sala-1",
            source_message_id=101,
        ).signal
        second_signal = parse_signal_text(
            BUY_SIGNAL,
            source_chat_id="sala-2",
            source_message_id=202,
        ).signal
        executor = self.executor()

        first = executor.execute_for_account(first_signal, self.account, self.profile())
        second = executor.execute_for_account(second_signal, self.account, self.profile())

        self.assertFalse(first.group_result.duplicate)
        self.assertTrue(second.group_result.duplicate)
        self.assertEqual(count_execution_orders(self.database_path), 4)

    def test_isolamento_entre_usuarios(self) -> None:
        bob = self.users.get_or_create_user(202, "bob")
        grant_paid_access(self.database_path, bob.id)
        bob_account = self.accounts.register_account(
            bob.id,
            MT5AccountForm("Broker", "Broker-Demo", "223344", "secret", "Bob"),
        )
        self.accounts.update_execution_profile_fixed_lot(bob.id, bob_account.id, Decimal("0.04"))
        signal = parse_signal_text(BUY_SIGNAL).signal

        results = self.executor().execute_for_signal(signal)

        self.assertEqual(len(results), 2)
        self.assertEqual(count_execution_orders(self.database_path), 8)

    def test_monitor_expira_cancela_e_notificacao_deduplica(self) -> None:
        signal = parse_signal_text(BUY_SIGNAL).signal
        result = self.executor().execute_for_account(signal, self.account, self.profile())
        group_id = result.group_result.group.id
        repository = ExecutionRepository(self.database_path)
        monitor = PendingOrderMonitor(self.database_path)

        try:
            self.assertTrue(repository.record_notification_once(event_type="plan_created", execution_group_id=group_id))
            self.assertFalse(repository.record_notification_once(event_type="plan_created", execution_group_id=group_id))
            self.assertTrue(monitor.cancel_if_price_invalidates(group_id=group_id, current_price=Decimal("4070")))
        finally:
            repository.close()
            monitor.close()

        self.assertEqual(group_status(self.database_path, group_id), "cancelled")

    def test_monitor_expira_ordens(self) -> None:
        signal = parse_signal_text(BUY_SIGNAL).signal
        result = self.executor().execute_for_account(signal, self.account, self.profile())
        monitor = PendingOrderMonitor(self.database_path)

        try:
            expired = monitor.expire_orders(datetime.now(tz=timezone.utc) + timedelta(hours=3))
        finally:
            monitor.close()

        self.assertEqual(expired, 1)
        self.assertEqual(group_status(self.database_path, result.group_result.group.id), "expired")

    def test_worker_aplica_breakeven_e_trailing_em_posicao_identificada(self) -> None:
        signal = parse_signal_text(BUY_SIGNAL).signal
        self.executor().execute_for_account(signal, self.account, self.profile())
        profile = self.accounts.update_execution_profile_field(
            self.user.id, self.account.id, "trailing_enabled", 1
        )
        client = SimulatedMT5Client(
            tick=TickInfo(bid=Decimal("4080"), ask=Decimal("4080")),
            positions=(
                {
                    "magic": 27071301,
                    "comment": f"tgcp {signal.signature[:8]} TP1",
                    "ticket": 9001,
                    "symbol": "XAUUSD",
                    "price_open": 4061,
                    "sl": 4044,
                    "tp": 4066,
                },
            ),
        )
        manager = PositionManager(self.database_path, self.accounts, lambda: client)

        changed = manager.manage_account(self.account, profile)

        self.assertEqual(changed, 1)
        self.assertEqual(client.order_send_requests[-1]["action"], 6)
        self.assertEqual(client.order_send_requests[-1]["position"], 9001)
        self.assertEqual(client.order_send_requests[-1]["sl"], 4063.0)

    def test_fechamento_e_registrado_e_notificado_uma_unica_vez(self) -> None:
        signal = parse_signal_text(BUY_SIGNAL).signal
        result = self.executor().execute_for_account(signal, self.account, self.profile())
        group_id = result.group_result.group.id
        with connect_database(self.database_path) as connection:
            order_id = connection.execute(
                "SELECT id FROM execution_orders WHERE execution_group_id=? AND tp_index=1",
                (group_id,),
            ).fetchone()[0]
            connection.execute(
                "UPDATE execution_orders SET status='filled',mt5_position_ticket='9001' WHERE id=?",
                (order_id,),
            ).close()

        class RecordingNotifier:
            def __init__(self) -> None:
                self.messages: list[tuple[int, str]] = []

            def send(self, telegram_user_id: int, message: str) -> bool:
                self.messages.append((telegram_user_id, message))
                return True

        notifier = RecordingNotifier()
        now = datetime.now(tz=timezone.utc).timestamp()
        deals = (
            {"ticket": 7000, "position_id": 9001, "magic": MT5_MAGIC_NUMBER,
             "entry": 0, "time": now, "profit": 0, "commission": -0.20},
            {"ticket": 7001, "position_id": 9001, "magic": MT5_MAGIC_NUMBER,
             "entry": 1, "reason": 5, "time": now, "price": 4066,
             "profit": 5, "commission": -0.20, "swap": 0, "fee": 0},
        )
        client = SimulatedMT5Client(history_deals=deals)
        monitor = SettlementMonitor(self.database_path, notifier)  # type: ignore[arg-type]

        self.assertEqual(monitor.reconcile(client, self.account), 1)
        monitor.deliver_pending(self.account)
        self.assertEqual(monitor.reconcile(client, self.account), 0)
        monitor.deliver_pending(self.account)
        self.assertEqual(len(notifier.messages), 1)
        self.assertIn("TP1 ATINGIDO", notifier.messages[0][1])
        self.assertIn("+US$ 4.60", notifier.messages[0][1])
        with connect_database(self.database_path) as connection:
            row = connection.execute(
                "SELECT status,net_profit FROM execution_orders WHERE id=?", (order_id,)
            ).fetchone()
        self.assertEqual(row, ("closed", "4.6"))

    def test_worker_move_restantes_para_be_quando_tp1_e_atingido(self) -> None:
        signal = parse_signal_text(BUY_SIGNAL).signal
        result = self.executor().execute_for_account(signal, self.account, self.profile())
        client = SimulatedMT5Client(
            tick=TickInfo(bid=Decimal("4066"), ask=Decimal("4066")),
            positions=(
                {
                    "magic": 27071301,
                    "comment": f"tgcp {signal.signature[:8]} TP2",
                    "ticket": 9002,
                    "symbol": "XAUUSD",
                    "price_open": 4061,
                    "sl": 4044,
                    "tp": 4071,
                },
                {
                    "magic": 27071301,
                    "comment": f"tgcp {signal.signature[:8]} TP3",
                    "ticket": 9003,
                    "symbol": "XAUUSD",
                    "price_open": 4061,
                    "sl": 4044,
                    "tp": 4076,
                },
            ),
        )
        manager = PositionManager(self.database_path, self.accounts, lambda: client)

        changed = manager.manage_account(self.account, self.profile())

        self.assertEqual(changed, 2)
        self.assertEqual(
            [request["position"] for request in client.order_send_requests],
            [9002, 9003],
        )
        self.assertEqual(
            [request["sl"] for request in client.order_send_requests],
            [4061.0, 4061.0],
        )
        with connect_database(self.database_path) as connection:
            protection = connection.execute(
                """
                SELECT tp1_reached_at, breakeven_applied_at
                FROM execution_groups WHERE id = ?
                """,
                (result.group_result.group.id,),
            ).fetchone()
        self.assertIsNotNone(protection[0])
        self.assertIsNotNone(protection[1])

    def test_worker_nao_move_restantes_para_be_quando_cliente_desativa(self) -> None:
        signal = parse_signal_text(BUY_SIGNAL).signal
        result = self.executor().execute_for_account(signal, self.account, self.profile())
        profile = self.accounts.update_execution_profile_field(
            self.user.id,
            self.account.id,
            "tp1_breakeven_enabled",
            0,
        )
        client = SimulatedMT5Client(
            tick=TickInfo(bid=Decimal("4066"), ask=Decimal("4066")),
            positions=(
                {
                    "magic": 27071301,
                    "comment": f"tgcp {signal.signature[:8]} TP2",
                    "ticket": 9002,
                    "symbol": "XAUUSD",
                    "price_open": 4061,
                    "sl": 4044,
                    "tp": 4071,
                },
            ),
        )
        manager = PositionManager(self.database_path, self.accounts, lambda: client)

        changed = manager.manage_account(self.account, profile)

        self.assertEqual(changed, 0)
        self.assertEqual(client.order_send_requests, [])
        with connect_database(self.database_path) as connection:
            protection = connection.execute(
                """
                SELECT tp1_reached_at, breakeven_applied_at
                FROM execution_groups WHERE id = ?
                """,
                (result.group_result.group.id,),
            ).fetchone()
        self.assertIsNotNone(protection[0])
        self.assertIsNone(protection[1])

    def test_worker_recupera_tp1_do_historico_apos_retracao(self) -> None:
        signal = parse_signal_text(BUY_SIGNAL).signal
        self.executor().execute_for_account(signal, self.account, self.profile())
        client = SimulatedMT5Client(
            tick=TickInfo(bid=Decimal("4062"), ask=Decimal("4062")),
            positions=(
                {
                    "magic": 27071301,
                    "comment": f"tgcp {signal.signature[:8]} TP2",
                    "ticket": 9012,
                    "symbol": "XAUUSD",
                    "price_open": 4061,
                    "sl": 4044,
                    "tp": 4071,
                },
            ),
            history_deals=(
                {
                    "magic": 27071301,
                    "comment": f"tgcp {signal.signature[:8]} TP1",
                    "position_id": 9011,
                    "price": 4066,
                },
            ),
        )
        manager = PositionManager(self.database_path, self.accounts, lambda: client)

        changed = manager.manage_account(self.account, self.profile())

        self.assertEqual(changed, 1)
        self.assertEqual(client.order_send_requests[0]["position"], 9012)
        self.assertEqual(client.order_send_requests[0]["sl"], 4061.0)

    def test_worker_liga_entrada_e_saida_tp1_pela_position_id(self) -> None:
        signal = parse_signal_text(BUY_SIGNAL).signal
        self.executor().execute_for_account(signal, self.account, self.profile())
        client = SimulatedMT5Client(
            tick=TickInfo(bid=Decimal("4062"), ask=Decimal("4062")),
            positions=(
                {
                    "magic": 27071301,
                    "comment": f"tgcp {signal.signature[:8]} TP2",
                    "ticket": 9112,
                    "symbol": "XAUUSD",
                    "price_open": 4061,
                    "sl": 4044,
                    "tp": 4071,
                },
            ),
            history_deals=(
                {
                    "magic": 27071301,
                    "comment": f"tgcp {signal.signature[:8]} TP1",
                    "position_id": 9111,
                    "order": 8111,
                    "price": 4061,
                    "reason": 3,
                },
                {
                    "magic": 27071301,
                    "comment": "[tp 4066.00]",
                    "position_id": 9111,
                    "order": 8112,
                    "price": 4066,
                    "reason": 5,
                },
            ),
        )
        manager = PositionManager(self.database_path, self.accounts, lambda: client)

        changed = manager.manage_account(self.account, self.profile())

        self.assertEqual(changed, 1)
        self.assertEqual(client.order_send_requests[0]["position"], 9112)
        self.assertEqual(client.order_send_requests[0]["sl"], 4061.0)

    def test_worker_move_sell_restante_para_be_ao_atingir_tp1(self) -> None:
        signal = parse_signal_text(SELL_SIGNAL).signal
        self.executor().execute_for_account(signal, self.account, self.profile())
        client = SimulatedMT5Client(
            tick=TickInfo(bid=Decimal("4054"), ask=Decimal("4054")),
            positions=(
                {
                    "magic": 27071301,
                    "comment": f"tgcp {signal.signature[:8]} TP2",
                    "ticket": 9022,
                    "symbol": "XAUUSD",
                    "price_open": 4061,
                    "sl": 4070,
                    "tp": 4050,
                },
            ),
        )
        manager = PositionManager(self.database_path, self.accounts, lambda: client)

        changed = manager.manage_account(self.account, self.profile())

        self.assertEqual(changed, 1)
        self.assertEqual(client.order_send_requests[0]["position"], 9022)
        self.assertEqual(client.order_send_requests[0]["sl"], 4061.0)

    def test_worker_encontra_posicao_por_ticket_quando_corretora_altera_comentario(self) -> None:
        signal = parse_signal_text(BUY_SIGNAL).signal
        result = self.executor().execute_for_account(signal, self.account, self.profile())
        group_id = result.group_result.group.id
        with connect_database(self.database_path) as connection:
            connection.execute(
                """
                UPDATE execution_orders
                SET mt5_position_ticket='9202'
                WHERE execution_group_id=? AND tp_index=2
                """,
                (group_id,),
            ).close()
            connection.execute(
                """
                UPDATE execution_orders SET status='cancelled'
                WHERE execution_group_id=? AND tp_index>2
                """,
                (group_id,),
            ).close()
        client = SimulatedMT5Client(
            tick=TickInfo(bid=Decimal("4066"), ask=Decimal("4066")),
            positions=(
                {
                    "magic": MT5_MAGIC_NUMBER,
                    "comment": "comentario alterado pela corretora",
                    "ticket": 9202,
                    "symbol": "XAUUSD",
                    "price_open": 4061,
                    "sl": 4044,
                    "tp": 4071,
                    "volume": 0.01,
                },
            ),
        )

        changed = PositionManager(
            self.database_path, self.accounts, lambda: client
        ).manage_account(self.account, self.profile())

        self.assertEqual(changed, 1)
        with connect_database(self.database_path) as connection:
            row = connection.execute(
                """
                SELECT o.be_status,o.be_confirmed_sl,g.breakeven_applied_at
                FROM execution_orders o JOIN execution_groups g
                  ON g.id=o.execution_group_id
                WHERE o.execution_group_id=? AND o.tp_index=2
                """,
                (group_id,),
            ).fetchone()
        self.assertEqual(row[0], "applied")
        self.assertEqual(Decimal(row[1]), Decimal("4061"))
        self.assertIsNotNone(row[2])

    def test_worker_fecha_restante_quando_preco_retorna_antes_do_be(self) -> None:
        signal = parse_signal_text(BUY_SIGNAL).signal
        result = self.executor().execute_for_account(signal, self.account, self.profile())
        group_id = result.group_result.group.id
        with connect_database(self.database_path) as connection:
            connection.execute(
                "UPDATE execution_orders SET status='cancelled' "
                "WHERE execution_group_id=? AND tp_index>2",
                (group_id,),
            ).close()
        client = SimulatedMT5Client(
            tick=TickInfo(bid=Decimal("4060"), ask=Decimal("4060.2")),
            positions=(
                {
                    "magic": MT5_MAGIC_NUMBER,
                    "comment": f"tgcp {signal.signature[:8]} TP2",
                    "ticket": 9302,
                    "symbol": "XAUUSD",
                    "price_open": 4061,
                    "sl": 4044,
                    "tp": 4071,
                    "volume": 0.01,
                    "type": 0,
                },
            ),
            history_deals=(
                {
                    "magic": MT5_MAGIC_NUMBER,
                    "comment": f"tgcp {signal.signature[:8]} TP1",
                    "position_id": 9301,
                    "price": 4066,
                },
            ),
        )

        changed = PositionManager(
            self.database_path, self.accounts, lambda: client
        ).manage_account(self.account, self.profile())

        self.assertEqual(changed, 1)
        self.assertEqual(client.order_send_requests[0]["action"], 1)
        self.assertEqual(client.order_send_requests[0]["position"], 9302)
        self.assertEqual(client.order_send_requests[0]["type"], 1)
        with connect_database(self.database_path) as connection:
            status = connection.execute(
                "SELECT be_status FROM execution_orders "
                "WHERE execution_group_id=? AND tp_index=2",
                (group_id,),
            ).fetchone()[0]
        self.assertEqual(status, "closed_at_market")

    def test_grupo_nao_e_marcado_quando_uma_posicao_nao_confirma_be(self) -> None:
        signal = parse_signal_text(BUY_SIGNAL).signal
        result = self.executor().execute_for_account(signal, self.account, self.profile())
        group_id = result.group_result.group.id
        with connect_database(self.database_path) as connection:
            connection.execute(
                "UPDATE execution_orders SET status='cancelled' "
                "WHERE execution_group_id=? AND tp_index>3",
                (group_id,),
            ).close()

        class PartiallyFailingClient(SimulatedMT5Client):
            def order_send(self, request: dict[str, object]) -> dict[str, object]:
                if int(request.get("position", 0)) == 9403:
                    self.order_send_requests.append(dict(request))
                    return {"retcode": 10030, "comment": "invalid stops"}
                return super().order_send(request)

        client = PartiallyFailingClient(
            tick=TickInfo(bid=Decimal("4066"), ask=Decimal("4066")),
            positions=(
                {
                    "magic": MT5_MAGIC_NUMBER, "comment": f"tgcp {signal.signature[:8]} TP2",
                    "ticket": 9402, "symbol": "XAUUSD", "price_open": 4061,
                    "sl": 4044, "tp": 4071, "volume": 0.01,
                },
                {
                    "magic": MT5_MAGIC_NUMBER, "comment": f"tgcp {signal.signature[:8]} TP3",
                    "ticket": 9403, "symbol": "XAUUSD", "price_open": 4061,
                    "sl": 4044, "tp": 4076, "volume": 0.01,
                },
            ),
        )

        PositionManager(
            self.database_path, self.accounts, lambda: client
        ).manage_account(self.account, self.profile())

        with connect_database(self.database_path) as connection:
            group_be = connection.execute(
                "SELECT breakeven_applied_at FROM execution_groups WHERE id=?",
                (group_id,),
            ).fetchone()[0]
            statuses = connection.execute(
                "SELECT tp_index,be_status FROM execution_orders "
                "WHERE execution_group_id=? AND tp_index IN (2,3) ORDER BY tp_index",
                (group_id,),
            ).fetchall()
        self.assertIsNone(group_be)
        self.assertEqual(statuses, [(2, "applied"), (3, "failed")])

    def test_worker_cancela_entrada_pendente_restante_apos_tp1(self) -> None:
        signal = parse_signal_text(BUY_SIGNAL).signal
        result = self.executor().execute_for_account(signal, self.account, self.profile())
        client = SimulatedMT5Client(
            tick=TickInfo(bid=Decimal("4066"), ask=Decimal("4066")),
            positions=(
                {
                    "magic": 27071301,
                    "comment": f"tgcp {signal.signature[:8]} TP2",
                    "ticket": 9032,
                    "symbol": "XAUUSD",
                    "price_open": 4061,
                    "sl": 4044,
                    "tp": 4071,
                },
            ),
            orders=(
                {
                    "magic": 27071301,
                    "comment": f"tgcp {signal.signature[:8]} TP3",
                    "ticket": 7033,
                    "symbol": "XAUUSD",
                },
            ),
        )
        manager = PositionManager(self.database_path, self.accounts, lambda: client)

        changed = manager.manage_account(self.account, self.profile())

        self.assertEqual(changed, 2)
        self.assertEqual(
            [request["action"] for request in client.order_send_requests],
            [6, 8],
        )
        self.assertEqual(client.order_send_requests[0]["position"], 9032)
        self.assertEqual(client.order_send_requests[1]["order"], 7033)
        with connect_database(self.database_path) as connection:
            status = connection.execute(
                """
                SELECT status FROM execution_orders
                WHERE execution_group_id = ? AND tp_index = 3
                """,
                (result.group_result.group.id,),
            ).fetchone()[0]
        self.assertEqual(status, "cancelled")

    def test_worker_remove_pendente_invalidada_no_broker(self) -> None:
        signal = parse_signal_text(BUY_SIGNAL).signal
        result = self.executor().execute_for_account(signal, self.account, self.profile())
        client = SimulatedMT5Client(
            tick=TickInfo(bid=Decimal("4067"), ask=Decimal("4067")),
            orders=(
                {
                    "magic": 27071301,
                    "comment": f"tgcp {signal.signature[:8]} TP1",
                    "ticket": 7001,
                    "symbol": "XAUUSD",
                },
            ),
        )
        manager = PositionManager(self.database_path, self.accounts, lambda: client)

        changed = manager.manage_account(self.account, self.profile())

        self.assertEqual(changed, 1)
        self.assertEqual(client.order_send_requests[-1]["action"], 8)
        self.assertEqual(client.order_send_requests[-1]["order"], 7001)
        with connect_database(self.database_path) as connection:
            status = connection.execute(
                "SELECT status FROM execution_orders WHERE execution_group_id = ? AND tp_index = 1",
                (result.group_result.group.id,),
            ).fetchone()[0]
        self.assertEqual(status, "cancelled")

    def test_worker_cancela_pendente_de_cliente_pausado(self) -> None:
        # Regressao: pausar um cliente so bloqueava sinais NOVOS. Uma ordem
        # pendente enviada antes da pausa continuava viva no broker e podia
        # ser preenchida depois — o cliente "desativado" recebia a operacao
        # mesmo assim.
        signal = parse_signal_text(BUY_SIGNAL).signal
        result = self.executor().execute_for_account(signal, self.account, self.profile())
        self.users.set_status(self.user.id, USER_STATUS_PAUSED)
        client = SimulatedMT5Client(
            tick=TickInfo(bid=Decimal("4059"), ask=Decimal("4061")),
            orders=(
                {
                    "magic": 27071301,
                    "comment": f"tgcp {signal.signature[:8]} TP1",
                    "ticket": 7101,
                    "symbol": "XAUUSD",
                },
            ),
        )
        manager = PositionManager(self.database_path, self.accounts, lambda: client)

        changed = manager.manage_account(self.account, self.profile())

        self.assertEqual(changed, 1)
        self.assertEqual(client.order_send_requests[-1]["action"], 8)
        self.assertEqual(client.order_send_requests[-1]["order"], 7101)
        self.assertEqual(client.order_send_requests[-1]["comment"], "tgcp user paused")
        with connect_database(self.database_path) as connection:
            status = connection.execute(
                "SELECT status FROM execution_orders WHERE execution_group_id = ? AND tp_index = 1",
                (result.group_result.group.id,),
            ).fetchone()[0]
        self.assertEqual(status, "cancelled")

    def test_worker_nao_mexe_em_pendente_de_cliente_ativo(self) -> None:
        signal = parse_signal_text(BUY_SIGNAL).signal
        self.executor().execute_for_account(signal, self.account, self.profile())
        client = SimulatedMT5Client(
            tick=TickInfo(bid=Decimal("4059"), ask=Decimal("4061")),
            orders=(
                {
                    "magic": 27071301,
                    "comment": f"tgcp {signal.signature[:8]} TP1",
                    "ticket": 7102,
                    "symbol": "XAUUSD",
                },
            ),
        )
        manager = PositionManager(self.database_path, self.accounts, lambda: client)

        changed = manager.manage_account(self.account, self.profile())

        self.assertEqual(changed, 0)
        self.assertEqual(client.order_send_requests, [])

    def test_worker_remove_pendente_gtc_ao_vencer_no_banco(self) -> None:
        signal = parse_signal_text(BUY_SIGNAL).signal
        result = self.executor().execute_for_account(signal, self.account, self.profile())
        with connect_database(self.database_path) as connection:
            connection.execute(
                "UPDATE execution_groups SET expiration_at = ? WHERE id = ?",
                (
                    (datetime.now(tz=timezone.utc) - timedelta(minutes=1)).isoformat(),
                    result.group_result.group.id,
                ),
            )
        client = SimulatedMT5Client(
            tick=TickInfo(bid=Decimal("4062"), ask=Decimal("4062")),
            orders=(
                {
                    "magic": 27071301,
                    "comment": f"tgcp {signal.signature[:8]} TP1",
                    "ticket": 7002,
                    "symbol": "XAUUSD",
                },
            ),
        )
        manager = PositionManager(self.database_path, self.accounts, lambda: client)

        changed = manager.manage_account(self.account, self.profile())

        self.assertEqual(changed, 1)
        self.assertEqual(client.order_send_requests[-1]["action"], 8)
        self.assertEqual(client.order_send_requests[-1]["order"], 7002)
        with connect_database(self.database_path) as connection:
            status = connection.execute(
                "SELECT status FROM execution_orders WHERE execution_group_id = ? AND tp_index = 1",
                (result.group_result.group.id,),
            ).fetchone()[0]
        self.assertEqual(status, "expired")

    def test_worker_fecha_exposicao_ao_atingir_limite_diario_com_flutuante(self) -> None:
        profile = self.accounts.update_execution_profile_field(
            self.user.id,
            self.account.id,
            "daily_loss_limit",
            "30",
        )

        class RecordingNotifier:
            def __init__(self) -> None:
                self.messages: list[tuple[int, str]] = []

            def send(self, telegram_user_id: int, message: str) -> bool:
                self.messages.append((telegram_user_id, message))
                return True

        notifier = RecordingNotifier()
        client = SimulatedMT5Client(
            positions=(
                {
                    "magic": MT5_MAGIC_NUMBER,
                    "ticket": 9501,
                    "symbol": "XAUUSD",
                    "type": 0,
                    "volume": 0.01,
                    "profit": -16,
                    "swap": 0,
                },
                {
                    "magic": MT5_MAGIC_NUMBER,
                    "ticket": 9502,
                    "symbol": "XAUUSD",
                    "type": 0,
                    "volume": 0.01,
                    "profit": -15,
                    "swap": 0,
                },
            ),
            orders=(
                {
                    "magic": MT5_MAGIC_NUMBER,
                    "ticket": 7503,
                    "symbol": "XAUUSD",
                },
            ),
        )

        changed = PositionManager(
            self.database_path,
            self.accounts,
            lambda: client,
            notifier=notifier,  # type: ignore[arg-type]
        ).manage_account(self.account, profile)

        self.assertEqual(changed, 3)
        self.assertEqual(
            [request["action"] for request in client.order_send_requests],
            [8, 1, 1],
        )
        self.assertEqual(
            [request.get("position") for request in client.order_send_requests[1:]],
            [9501, 9502],
        )
        with connect_database(self.database_path) as connection:
            pause_until = connection.execute(
                "SELECT daily_signal_pause_until FROM users WHERE id = ?",
                (self.user.id,),
            ).fetchone()[0]
        self.assertIsNotNone(pause_until)
        self.assertEqual(len(notifier.messages), 1)
        self.assertIn("LIMITE DE PERDA DIÁRIA ACIONADO", notifier.messages[0][1])
        self.assertIn("US$ -31.00", notifier.messages[0][1])

    def test_worker_nao_fecha_antes_do_limite_diario(self) -> None:
        profile = self.accounts.update_execution_profile_field(
            self.user.id,
            self.account.id,
            "daily_loss_limit",
            "30",
        )
        client = SimulatedMT5Client(
            positions=(
                {
                    "magic": MT5_MAGIC_NUMBER,
                    "ticket": 9601,
                    "symbol": "XAUUSD",
                    "type": 0,
                    "volume": 0.01,
                    "profit": -29,
                },
            ),
        )

        changed = PositionManager(
            self.database_path,
            self.accounts,
            lambda: client,
        ).manage_account(self.account, profile)

        self.assertEqual(changed, 0)
        self.assertEqual(client.order_send_requests, [])

    def test_worker_protege_meta_diaria_com_lucro_flutuante(self) -> None:
        profile = self.accounts.update_execution_profile_field(
            self.user.id,
            self.account.id,
            "daily_profit_target",
            "30",
        )

        class RecordingNotifier:
            def __init__(self) -> None:
                self.messages: list[tuple[int, str]] = []

            def send(self, telegram_user_id: int, message: str) -> bool:
                self.messages.append((telegram_user_id, message))
                return True

        notifier = RecordingNotifier()
        client = SimulatedMT5Client(
            positions=(
                {
                    "magic": MT5_MAGIC_NUMBER,
                    "ticket": 9651,
                    "symbol": "XAUUSD",
                    "type": 0,
                    "volume": 0.01,
                    "profit": 18,
                },
                {
                    "magic": MT5_MAGIC_NUMBER,
                    "ticket": 9652,
                    "symbol": "XAUUSD",
                    "type": 1,
                    "volume": 0.01,
                    "profit": 13,
                },
            ),
            orders=(
                {
                    "magic": MT5_MAGIC_NUMBER,
                    "ticket": 7653,
                    "symbol": "XAUUSD",
                },
            ),
        )

        changed = PositionManager(
            self.database_path,
            self.accounts,
            lambda: client,
            notifier=notifier,  # type: ignore[arg-type]
        ).manage_account(self.account, profile)

        self.assertEqual(changed, 3)
        self.assertEqual(
            [request["action"] for request in client.order_send_requests],
            [8, 1, 1],
        )
        self.assertTrue(
            all(
                request["comment"] == "tgcp daily target"
                for request in client.order_send_requests
            )
        )
        self.assertEqual(len(notifier.messages), 1)
        self.assertIn("META DIÁRIA ATINGIDA", notifier.messages[0][1])
        self.assertIn("US$ 31.00", notifier.messages[0][1])
        with connect_database(self.database_path) as connection:
            pause_until = connection.execute(
                "SELECT daily_signal_pause_until FROM users WHERE id = ?",
                (self.user.id,),
            ).fetchone()[0]
        self.assertIsNotNone(pause_until)

    def test_worker_pausa_meta_diaria_mesmo_sem_exposicao_aberta(self) -> None:
        # Regressao: o TP que fecha a ULTIMA posicao da conta nao deixa
        # posicao nem pendente aberta na proxima varredura do worker. Antes
        # do fix, a funcao retornava cedo demais e nunca detectava que a
        # meta diaria ja tinha sido atingida pelo resultado realizado —
        # nada era pausado, nada era avisado, e o proximo sinal continuava
        # sendo aceito normalmente.
        profile = self.accounts.update_execution_profile_field(
            self.user.id,
            self.account.id,
            "daily_profit_target",
            "30",
        )

        class RecordingNotifier:
            def __init__(self) -> None:
                self.messages: list[tuple[int, str]] = []

            def send(self, telegram_user_id: int, message: str) -> bool:
                self.messages.append((telegram_user_id, message))
                return True

        notifier = RecordingNotifier()
        client = SimulatedMT5Client(
            positions=(),
            orders=(),
            history_deals=({"magic": MT5_MAGIC_NUMBER, "profit": 31},),
        )

        changed = PositionManager(
            self.database_path,
            self.accounts,
            lambda: client,
            notifier=notifier,  # type: ignore[arg-type]
        ).manage_account(self.account, profile)

        self.assertEqual(changed, 0)
        self.assertEqual(client.order_send_requests, [])
        self.assertEqual(len(notifier.messages), 1)
        self.assertIn("META DIÁRIA ATINGIDA", notifier.messages[0][1])
        with connect_database(self.database_path) as connection:
            pause_until = connection.execute(
                "SELECT daily_signal_pause_until FROM users WHERE id = ?",
                (self.user.id,),
            ).fetchone()[0]
        self.assertIsNotNone(pause_until)

    def test_worker_pausa_limite_diario_mesmo_sem_exposicao_aberta(self) -> None:
        profile = self.accounts.update_execution_profile_field(
            self.user.id,
            self.account.id,
            "daily_loss_limit",
            "30",
        )
        client = SimulatedMT5Client(
            positions=(),
            orders=(),
            history_deals=({"magic": MT5_MAGIC_NUMBER, "profit": -31},),
        )

        changed = PositionManager(
            self.database_path,
            self.accounts,
            lambda: client,
        ).manage_account(self.account, profile)

        self.assertEqual(changed, 0)
        with connect_database(self.database_path) as connection:
            pause_until = connection.execute(
                "SELECT daily_signal_pause_until FROM users WHERE id = ?",
                (self.user.id,),
            ).fetchone()[0]
        self.assertIsNotNone(pause_until)

    def test_worker_para_de_reconsultar_historico_apos_pausa_ja_ativa(self) -> None:
        # Regressao: sem exposicao aberta e com a pausa diaria ja ativa, o
        # worker nao deve mais consultar o historico do MT5 nem reescrever a
        # mesma pausa a cada tick (uma vez por segundo, pelo resto do dia) —
        # so ha algo a fazer de novo se aparecer posicao/pendente nova.
        profile = self.accounts.update_execution_profile_field(
            self.user.id,
            self.account.id,
            "daily_profit_target",
            "30",
        )
        client = SimulatedMT5Client(
            positions=(),
            orders=(),
            history_deals=({"magic": MT5_MAGIC_NUMBER, "profit": 31},),
        )
        manager = PositionManager(self.database_path, self.accounts, lambda: client)

        manager.manage_account(self.account, profile)
        self.assertEqual(len(client.history_deal_queries), 1)

        manager.manage_account(self.account, profile)
        manager.manage_account(self.account, profile)

        self.assertEqual(len(client.history_deal_queries), 1)

    def test_worker_nao_fecha_antes_da_meta_diaria(self) -> None:
        profile = self.accounts.update_execution_profile_field(
            self.user.id,
            self.account.id,
            "daily_profit_target",
            "30",
        )
        client = SimulatedMT5Client(
            positions=(
                {
                    "magic": MT5_MAGIC_NUMBER,
                    "ticket": 9661,
                    "symbol": "XAUUSD",
                    "type": 0,
                    "volume": 0.01,
                    "profit": 29,
                },
            ),
        )

        changed = PositionManager(
            self.database_path,
            self.accounts,
            lambda: client,
        ).manage_account(self.account, profile)

        self.assertEqual(changed, 0)
        self.assertEqual(client.order_send_requests, [])

    def test_limite_diario_nao_fecha_operacao_manual(self) -> None:
        profile = self.accounts.update_execution_profile_field(
            self.user.id,
            self.account.id,
            "daily_loss_limit",
            "30",
        )
        client = SimulatedMT5Client(
            positions=(
                {
                    "magic": 0,
                    "ticket": 9701,
                    "symbol": "XAUUSD",
                    "type": 0,
                    "volume": 0.01,
                    "profit": -100,
                },
            ),
        )

        PositionManager(
            self.database_path,
            self.accounts,
            lambda: client,
        ).manage_account(self.account, profile)

        self.assertEqual(client.order_send_requests, [])

    def executor(
        self,
        *,
        tick: TickInfo | None = None,
        client: SimulatedMT5Client | None = None,
        execution_mode: str = "simulation",
        global_kill_switch: bool = False,
        allow_live_accounts: bool = False,
    ) -> PendingOrderExecutor:
        selected_client = client or SimulatedMT5Client(tick=tick or TickInfo(bid=Decimal("4062"), ask=Decimal("4062")))
        return PendingOrderExecutor(
            self.database_path,
            self.accounts,
            execution_mode=execution_mode,
            global_kill_switch=global_kill_switch,
            allow_live_accounts=allow_live_accounts,
            client_factory=lambda: selected_client,
        )


def count_execution_orders(database_path: Path) -> int:
    with connect_database(database_path) as connection:
        cursor = connection.execute("SELECT COUNT(*) FROM execution_orders")
        try:
            return int(cursor.fetchone()[0])
        finally:
            cursor.close()


def minutes_until_expiration(database_path: Path) -> int:
    with connect_database(database_path) as connection:
        cursor = connection.execute("SELECT pending_created_at, expiration_at FROM execution_groups LIMIT 1")
        try:
            row = cursor.fetchone()
        finally:
            cursor.close()
    return int((datetime.fromisoformat(row[1]) - datetime.fromisoformat(row[0])).total_seconds() / 60)


def total_latency_ms(database_path: Path) -> int | None:
    with connect_database(database_path) as connection:
        cursor = connection.execute("SELECT total_ms FROM execution_groups LIMIT 1")
        try:
            row = cursor.fetchone()
        finally:
            cursor.close()
    return row[0]


def group_status(database_path: Path, group_id: int) -> str:
    with connect_database(database_path) as connection:
        cursor = connection.execute("SELECT status FROM execution_groups WHERE id = ?", (group_id,))
        try:
            row = cursor.fetchone()
        finally:
            cursor.close()
    return str(row[0])


def execution_order_rows(database_path: Path, group_id: int) -> list[dict[str, str]]:
    with connect_database(database_path) as connection:
        cursor = connection.execute(
            """
            SELECT status, mt5_order_ticket
            FROM execution_orders
            WHERE execution_group_id = ?
            ORDER BY tp_index ASC
            """,
            (group_id,),
        )
        try:
            rows = cursor.fetchall()
        finally:
            cursor.close()
    return [{"status": str(row[0]), "ticket": str(row[1])} for row in rows]


if __name__ == "__main__":
    unittest.main()
