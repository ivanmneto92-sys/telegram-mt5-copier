from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
import time
from urllib.parse import urlsplit, urlunsplit

from .account_service import AccountService
from .access_control import ACCESS_EXPIRED, paid_access_decision
from .admin_auth import AdminBrowserAuthService
from .channel_catalog import (
    ChannelCatalogService,
    REQUEST_APPROVED,
    REQUEST_AWAITING_MEMBERSHIP,
    REQUEST_PENDING_ACCESS,
    REQUEST_READY_REVIEW,
    extract_channel_toggle,
)
from .bot_keyboards import (
    ACCOUNT_MENU,
    CB_ACCOUNT,
    CB_ADMIN_BROWSER_ACCESS,
    CB_ACTIVATE,
    CB_CANCEL,
    CB_CHANNELS,
    CB_CHANNEL_ADD,
    CB_CHANNEL_INFO,
    CB_CHANNEL_MODE_ALL,
    CB_CHANNEL_MODE_CUSTOM,
    CB_CONFIRM_ACTIVATE,
    CB_CONFIRM_PAUSE,
    CB_CONNECTION,
    CB_CONNECT_MT5,
    CB_EXEC_ENTRY_MENU,
    CB_EXEC_ENTRY_MARKET_ZONE,
    CB_EXEC_ENTRY_MARKET_NOW,
    CB_EXEC_ENTRY_PENDING,
    CB_EXEC_EXPIRATION_MENU,
    CB_EXEC_EXPIRATION_120,
    CB_EXEC_EXPIRATION_240,
    CB_EXEC_EXPIRATION_30,
    CB_EXEC_EXPIRATION_60,
    CB_EXEC_EXPIRATION_DAY,
    CB_EXEC_PRICE_MENU,
    CB_EXEC_PRICE_DISTRIBUTED,
    CB_EXEC_PRICE_FIRST_TOUCH,
    CB_EXEC_PRICE_MIDDLE,
    CB_EXEC_SPLIT_TPS,
    CB_EXEC_TP_MENU,
    CB_EXEC_TP_ALL,
    CB_EXEC_TP_1,
    CB_EXEC_TP_2,
    CB_EXEC_TP_3,
    CB_EXEC_TP_4,
    CB_HISTORY,
    CB_MAIN,
    CB_MT5_ACCOUNTS,
    CB_MT5_CONFIG,
    CB_MT5_CONFIRM_REMOVE,
    CB_MT5_REMOVE,
    CB_MT5_SECURE,
    CB_MT5_TEST,
    CB_MT5_VIEW,
    CB_OPERATIONS,
    CB_PAUSE,
    CB_PROTECTIONS,
    CB_RISK,
    CB_SIGNAL_EXECUTION,
    CONFIRM_PAUSE,
    CONFIRM_REMOVE_MT5,
    CHANNEL_MENU,
    CHANNEL_TEXT_INPUT_MENU,
    CONNECTION_MENU,
    CUSTOM_VALUE_MENU,
    DAILY_LOSS_MENU,
    DAILY_TARGET_MENU,
    FIXED_LOT_MENU,
    HISTORY_MENU,
    MAIN_MENU,
    MT5_ACCOUNTS_MENU,
    OPERATIONS_MENU,
    PAUSED_MENU,
    PROTECTIONS_MENU,
    RISK_MENU,
    RISK_MODE_MENU,
    RISK_PERCENT_MENU,
    MAX_OPEN_MENU,
    SPREAD_MENU,
    SLIPPAGE_MENU,
    ENTRY_MODE_MENU,
    ENTRY_PRICE_MENU,
    EXPIRATION_MENU,
    SIGNAL_EXECUTION_MENU,
    TAKE_PROFIT_COUNT_MENU,
    Button,
    extract_fixed_lot,
    extract_custom_value_field,
    extract_risk_value,
    extract_refresh_screen,
    is_valid_callback_data,
    main_menu,
    mt5_connect_menu,
    normalize_callback_data,
)
from .command_queue import CommandQueue
from .database import SIGNAL_MONITOR_SERVICE_NAME, connect_database, get_service_heartbeat
from .mt5.account_service import MT5AccountService
from .mt5.models import (
    CONNECTION_STATUS_CONNECTED,
    ENTRY_EXECUTION_MARKET_IMMEDIATE,
    ENTRY_EXECUTION_MARKET_ON_ZONE,
    ENTRY_EXECUTION_PENDING_ORDER,
    ENTRY_PRICE_DISTRIBUTED,
    ENTRY_PRICE_FIRST_TOUCH,
    ENTRY_PRICE_MIDDLE,
    MT5Account,
)
from .settings_service import SettingsService, UserSettings, decimal_to_storage
from .users import USER_STATUS_ACTIVE, USER_STATUS_PAUSED, User, UserRepository


@dataclass(frozen=True)
class BotResponse:
    text: str
    keyboard: tuple[tuple[object, ...], ...] | None = None
    screen: str = "main"


class RateLimiter:
    def __init__(self, limit: int = 12, window_seconds: int = 60) -> None:
        self.limit = limit
        self.window_seconds = window_seconds
        self._events: dict[int, list[float]] = {}

    def allow(self, telegram_user_id: int) -> bool:
        now = time.monotonic()
        window_start = now - self.window_seconds
        events = [event for event in self._events.get(telegram_user_id, []) if event >= window_start]
        if len(events) >= self.limit:
            self._events[telegram_user_id] = events
            return False
        events.append(now)
        self._events[telegram_user_id] = events
        return True


class BotService:
    def __init__(
        self,
        database_path: Path,
        *,
        admin_ids: tuple[int, ...] = (),
        rate_limiter: RateLimiter | None = None,
        account_service: AccountService | None = None,
        mt5_account_service: MT5AccountService | None = None,
        mt5_onboarding_url: str | None = None,
    ) -> None:
        self.users = UserRepository(database_path)
        self.settings = SettingsService(database_path)
        self.commands = CommandQueue(database_path)
        self.account_service = account_service or AccountService()
        self.mt5_accounts = mt5_account_service or MT5AccountService(database_path)
        self.channels = ChannelCatalogService(database_path)
        self.database_path = database_path
        self.mt5_onboarding_url = mt5_onboarding_url
        self.admin_ids = set(admin_ids)
        self.admin_browser_auth = AdminBrowserAuthService(
            database_path,
            admin_ids=admin_ids,
        )
        self.rate_limiter = rate_limiter or RateLimiter()
        self._pending_custom_values: dict[int, str] = {}
        self._closed = False

    def close(self) -> None:
        if self._closed:
            return
        for service in (self.users, self.settings, self.commands, self.mt5_accounts):
            service.close()
        self._closed = True

    def __enter__(self) -> "BotService":
        return self

    def __exit__(self, _exc_type: object, _exc: object, _traceback: object) -> None:
        self.close()

    def start(
        self,
        telegram_user_id: int,
        telegram_username: str | None,
        first_name: str | None = None,
    ) -> BotResponse:
        self._pending_custom_values.pop(telegram_user_id, None)
        user = self._ensure_user(telegram_user_id, telegram_username)
        return self._main_panel(user, first_name=first_name)

    def menu(
        self,
        telegram_user_id: int,
        telegram_username: str | None,
        first_name: str | None = None,
    ) -> BotResponse:
        self._pending_custom_values.pop(telegram_user_id, None)
        user = self._ensure_user(telegram_user_id, telegram_username)
        return self._main_panel(user, first_name=first_name)

    def status(
        self,
        telegram_user_id: int,
        telegram_username: str | None,
        first_name: str | None = None,
    ) -> BotResponse:
        self._pending_custom_values.pop(telegram_user_id, None)
        user = self._ensure_user(telegram_user_id, telegram_username)
        return self._connection_screen(user)

    def handle_text(
        self,
        telegram_user_id: int,
        telegram_username: str | None,
        text: str,
        first_name: str | None = None,
    ) -> BotResponse:
        user = self._ensure_user(telegram_user_id, telegram_username)
        field = self._pending_custom_values.get(telegram_user_id)
        if field is None:
            return BotResponse(
                "Use os botões do menu para escolher o que deseja configurar.",
                MAIN_MENU,
            )
        if field == "channel_link":
            try:
                result = self.channels.submit_request(user.id, text)
            except ValueError as exc:
                return BotResponse(
                    f"Não consegui cadastrar esse canal: {exc}\n\nEnvie outro link ou toque em Cancelar.",
                    CHANNEL_TEXT_INPUT_MENU,
                    screen="channel_add",
                )
            self._pending_custom_values.pop(telegram_user_id, None)
            if result.status == REQUEST_APPROVED:
                message = (
                    f"✅ {result.title} já faz parte do catálogo e foi habilitado para você."
                )
            else:
                message = "\n".join(
                    [
                        "✅ SUGESTÃO RECEBIDA",
                        "",
                        f"Canal: {result.canonical_link}",
                        "",
                        "Agora o administrador vai acessar o canal, analisar o formato dos sinais e confirmar o acesso da conta principal de monitoramento.",
                        "O canal só será liberado após essa aprovação.",
                    ]
                )
            screen = self._channels_screen(user)
            return BotResponse(
                f"{message}\n\n{screen.text}",
                screen.keyboard,
                screen="channels",
            )
        try:
            normalized_value = normalize_custom_numeric_value(text, integer=field in {"max", "spread", "slippage"})
            self._apply_risk_value(user, field, normalized_value)
        except ValueError as exc:
            return BotResponse(
                f"Valor inválido: {exc}\n\nEnvie outro valor ou toque em Cancelar.",
                CUSTOM_VALUE_MENU,
                screen="custom_value",
            )

        self._pending_custom_values.pop(telegram_user_id, None)
        updated_screen = self._risk_screen(user)
        return BotResponse(
            f"✅ Configuração atualizada: {custom_field_label(field)} = {custom_value_label(field, normalized_value)}.\n\n{updated_screen.text}",
            updated_screen.keyboard,
            screen="risk",
        )

    def handle_callback(
        self,
        telegram_user_id: int,
        telegram_username: str | None,
        callback_data: str,
        first_name: str | None = None,
    ) -> BotResponse:
        if not self.rate_limiter.allow(telegram_user_id):
            return BotResponse("Muitas ações em pouco tempo. Aguarde alguns segundos.", MAIN_MENU)

        if not is_valid_callback_data(callback_data):
            return BotResponse("Ação invalida ou expirada.", MAIN_MENU)

        callback = normalize_callback_data(callback_data)
        user = self._ensure_user(telegram_user_id, telegram_username)

        custom_field = extract_custom_value_field(callback)
        if custom_field is not None:
            self._pending_custom_values.pop(telegram_user_id, None)
            if self.mt5_accounts.first_account(user.id) is None:
                return self._connect_mt5_screen()
            self._pending_custom_values[telegram_user_id] = custom_field
            return BotResponse(
                custom_value_prompt(custom_field),
                CUSTOM_VALUE_MENU,
                screen="custom_value",
            )
        self._pending_custom_values.pop(telegram_user_id, None)

        refresh_screen = extract_refresh_screen(callback)
        if refresh_screen is not None:
            return self._screen_for_name(refresh_screen, user, first_name=first_name)

        if callback == CB_MAIN:
            return self._main_panel(user, first_name=first_name)
        if callback == CB_CHANNELS:
            return self._channels_screen(user)
        if callback == CB_CHANNEL_ADD:
            self._pending_custom_values[telegram_user_id] = "channel_link"
            return BotResponse(
                "\n".join(
                    [
                        "➕ SUGERIR CANAL",
                        "",
                        "Envie o link público ou @username do canal.",
                        "",
                        "Exemplos:",
                        "https://t.me/NomeDoCanal",
                        "https://web.telegram.org/k/#@NomeDoCanal",
                        "@NomeDoCanal",
                        "",
                        "⚠️ A conta principal de monitoramento precisa participar do canal. Você apenas sugere; o administrador faz a análise e a aprovação.",
                        "",
                        "Links privados devem ser enviados diretamente ao administrador e não ficam armazenados aqui.",
                    ]
                ),
                CHANNEL_TEXT_INPUT_MENU,
                screen="channel_add",
            )
        if callback == CB_CHANNEL_INFO:
            return BotResponse(
                "\n".join(
                    [
                        "ℹ️ COMO FUNCIONAM OS CANAIS",
                        "",
                        "1. Você sugere um canal público.",
                        "2. O administrador acessa e analisa o formato dos sinais.",
                        "3. A conta principal do sistema confirma que consegue monitorá-lo.",
                        "4. Após aprovação, o canal aparece na sua lista.",
                        "5. Você escolhe seguir todos ou somente canais específicos.",
                        "",
                        "A aprovação evita que mensagens incompatíveis gerem operações incorretas.",
                    ]
                ),
                CHANNEL_MENU,
                screen="channels",
            )
        if callback == CB_CHANNEL_MODE_ALL:
            self.channels.set_selection_mode(user.id, "all")
            return self._channels_screen(user)
        if callback == CB_CHANNEL_MODE_CUSTOM:
            self.channels.set_selection_mode(user.id, "custom")
            return self._channels_screen(user)
        channel_id = extract_channel_toggle(callback)
        if channel_id is not None:
            try:
                self.channels.toggle_subscription(user.id, channel_id)
            except ValueError as exc:
                return BotResponse(str(exc), CHANNEL_MENU, screen="channels")
            return self._channels_screen(user)
        if callback == CB_ADMIN_BROWSER_ACCESS:
            if telegram_user_id not in self.admin_ids:
                return BotResponse("Acesso administrativo não autorizado.", MAIN_MENU)
            panel_url = admin_panel_url(self.mt5_onboarding_url)
            if not panel_url:
                return BotResponse(
                    "URL HTTPS do painel administrativo não configurada.",
                    main_menu(None),
                )
            access_url = self.admin_browser_auth.create_login_url(
                telegram_user_id,
                panel_url,
            )
            return BotResponse(
                "\n".join(
                    [
                        "🖥️ ACESSO AO PAINEL PELO PC",
                        "",
                        "Abra o link abaixo no navegador do seu computador:",
                        "",
                        access_url,
                        "",
                        "🔐 O link funciona uma única vez e expira em 5 minutos.",
                        "Depois de entrar, sua sessão permanece ativa por até 12 horas.",
                    ]
                ),
                main_menu(panel_url),
                screen="admin_access",
            )
        if callback == CB_ACCOUNT:
            return self._account_screen(user)
        if callback == CB_OPERATIONS:
            return self._operations_screen(user)
        if callback == CB_RISK:
            return self._risk_screen(user)
        if callback == CB_PROTECTIONS:
            return self._protections_screen(user)
        if callback == CB_HISTORY:
            return self._history_screen(user)
        if callback == CB_CONNECTION:
            return self._connection_screen(user)
        if callback == CB_CONNECT_MT5:
            return self._connect_mt5_screen()
        if callback == CB_MT5_SECURE:
            return BotResponse(
                "Link seguro ainda nao configurado. Defina MT5_ONBOARDING_URL com uma URL HTTPS da Mini App.",
                mt5_connect_menu(self.mt5_onboarding_url),
                screen="mt5_connect",
            )
        if callback == CB_MT5_ACCOUNTS:
            return self._mt5_accounts_screen(user)
        if callback == CB_MT5_VIEW:
            return self._mt5_accounts_screen(user)
        if callback == CB_MT5_CONFIG:
            return self._signal_execution_screen(user)
        if callback == CB_SIGNAL_EXECUTION:
            return self._signal_execution_screen(user)
        if callback == CB_EXEC_ENTRY_MENU:
            return BotResponse(
                "\n".join(
                    [
                        "📍 MODO DE ENTRADA",
                        "",
                        "🚀 Entrada imediata:",
                        "Executa ao preço disponível quando o sinal chegar, mesmo fora da zona.",
                        "",
                        "📍 Posicionar na entrada:",
                        "Cria a ordem no preço indicado pelo canal e aguarda o mercado.",
                        "",
                        "⚖️ Mercado somente na zona:",
                        "Entra imediatamente se já estiver na zona; fora dela, posiciona a ordem.",
                        "",
                        "SL, TP, spread, margem e limites de risco continuam sendo validados em todos os modos.",
                    ]
                ),
                ENTRY_MODE_MENU,
                screen="signal_execution",
            )
        if callback == CB_EXEC_PRICE_MENU:
            return BotResponse("🎯 PREÇO DA FAIXA", ENTRY_PRICE_MENU, screen="signal_execution")
        if callback == CB_EXEC_EXPIRATION_MENU:
            return BotResponse("⏳ VALIDADE DAS ORDENS", EXPIRATION_MENU, screen="signal_execution")
        if callback == CB_EXEC_TP_MENU:
            return BotResponse(
                "\n".join(
                    [
                        "🎯 QUANTIDADE DE TAKE PROFITS",
                        "",
                        "Escolha quantos alvos serão usados em cada sinal.",
                        "Se o canal enviar menos TPs, serão usados somente os disponíveis.",
                        "",
                        "O lote total será dividido entre os alvos escolhidos.",
                        "Quando TP1 for atingido, as posições restantes serão protegidas no breakeven.",
                    ]
                ),
                TAKE_PROFIT_COUNT_MENU,
                screen="signal_execution",
            )
        if callback in {
            CB_EXEC_ENTRY_PENDING,
            CB_EXEC_ENTRY_MARKET_ZONE,
            CB_EXEC_ENTRY_MARKET_NOW,
        }:
            account = self.mt5_accounts.first_account(user.id)
            if account is None:
                return self._connect_mt5_screen()
            value = {
                CB_EXEC_ENTRY_PENDING: ENTRY_EXECUTION_PENDING_ORDER,
                CB_EXEC_ENTRY_MARKET_ZONE: ENTRY_EXECUTION_MARKET_ON_ZONE,
                CB_EXEC_ENTRY_MARKET_NOW: ENTRY_EXECUTION_MARKET_IMMEDIATE,
            }[callback]
            self.mt5_accounts.update_execution_profile_field(user.id, account.id, "entry_execution_mode", value)
            return self._signal_execution_screen(user)
        if callback in {CB_EXEC_PRICE_FIRST_TOUCH, CB_EXEC_PRICE_MIDDLE, CB_EXEC_PRICE_DISTRIBUTED}:
            account = self.mt5_accounts.first_account(user.id)
            if account is None:
                return self._connect_mt5_screen()
            value = {
                CB_EXEC_PRICE_FIRST_TOUCH: ENTRY_PRICE_FIRST_TOUCH,
                CB_EXEC_PRICE_MIDDLE: ENTRY_PRICE_MIDDLE,
                CB_EXEC_PRICE_DISTRIBUTED: ENTRY_PRICE_DISTRIBUTED,
            }[callback]
            self.mt5_accounts.update_execution_profile_field(user.id, account.id, "entry_price_mode", value)
            return self._signal_execution_screen(user)
        if callback in {
            CB_EXEC_EXPIRATION_30,
            CB_EXEC_EXPIRATION_60,
            CB_EXEC_EXPIRATION_120,
            CB_EXEC_EXPIRATION_240,
            CB_EXEC_EXPIRATION_DAY,
        }:
            account = self.mt5_accounts.first_account(user.id)
            if account is None:
                return self._connect_mt5_screen()
            minutes = {
                CB_EXEC_EXPIRATION_30: 30,
                CB_EXEC_EXPIRATION_60: 60,
                CB_EXEC_EXPIRATION_120: 120,
                CB_EXEC_EXPIRATION_240: 240,
                CB_EXEC_EXPIRATION_DAY: 1440,
            }[callback]
            self.mt5_accounts.update_execution_profile_field(
                user.id,
                account.id,
                "pending_expiration_minutes",
                minutes,
            )
            return self._signal_execution_screen(user)
        if callback == CB_EXEC_SPLIT_TPS:
            account = self.mt5_accounts.first_account(user.id)
            if account is None:
                return self._connect_mt5_screen()
            profile = self.mt5_accounts.ensure_execution_profile(user.id, account.id)
            self.mt5_accounts.update_execution_profile_field(user.id, account.id, "split_tps", int(not profile.split_tps))
            return self._signal_execution_screen(user)
        if callback in {
            CB_EXEC_TP_ALL,
            CB_EXEC_TP_1,
            CB_EXEC_TP_2,
            CB_EXEC_TP_3,
            CB_EXEC_TP_4,
        }:
            account = self.mt5_accounts.first_account(user.id)
            if account is None:
                return self._connect_mt5_screen()
            limit = {
                CB_EXEC_TP_ALL: 0,
                CB_EXEC_TP_1: 1,
                CB_EXEC_TP_2: 2,
                CB_EXEC_TP_3: 3,
                CB_EXEC_TP_4: 4,
            }[callback]
            self.mt5_accounts.update_execution_profile_field(
                user.id,
                account.id,
                "take_profit_limit",
                limit,
            )
            self.mt5_accounts.update_execution_profile_field(
                user.id,
                account.id,
                "split_tps",
                1,
            )
            return self._signal_execution_screen(user)
        if callback == CB_MT5_TEST:
            account = self.mt5_accounts.first_account(user.id)
            if account is None:
                return self._connect_mt5_screen()
            try:
                updated = self.mt5_accounts.test_connection(
                    user.id,
                    account.id,
                    startup_retry=True,
                )
            except Exception as exc:
                return BotResponse(f"Teste de conexao falhou: {exc}", MT5_ACCOUNTS_MENU, screen="mt5_accounts")
            return BotResponse(
                f"Conexao testada com sucesso para {updated.masked_login}.",
                MT5_ACCOUNTS_MENU,
                screen="mt5_accounts",
            )
        if callback == CB_MT5_REMOVE:
            account = self.mt5_accounts.first_account(user.id)
            if account is None:
                return self._connect_mt5_screen()
            return BotResponse(
                "\n".join(
                    [
                        "🗑️ REMOVER CONTA MT5",
                        "",
                        f"Conta: {account.masked_login}",
                        "",
                        "Ao confirmar, a senha criptografada e o perfil de execucao serao removidos.",
                        "",
                        "Deseja continuar?",
                    ]
                ),
                CONFIRM_REMOVE_MT5,
                screen="mt5_remove",
            )
        if callback == CB_ACTIVATE:
            return BotResponse(
                "\n".join(
                    [
                        "🔐 ATIVAÇÃO DO COPIADOR",
                        "",
                        "Sua conta MT5 pode ser conectada e configurada normalmente.",
                        "",
                        "A liberação dos sinais é feita pelo administrador após a confirmação do pagamento e da data de validade.",
                        "",
                        "Quando o acesso for aprovado, o status aparecerá como ativo automaticamente.",
                    ]
                ),
                MAIN_MENU,
                screen="activation_pending",
            )
        if callback == CB_PAUSE:
            return BotResponse(
                "\n".join(
                    [
                        "⏸️ PAUSAR NOVAS ENTRADAS",
                        "",
                        "Ao confirmar, novos sinais não poderão criar operações.",
                        "",
                        "Operações já abertas não serão fechadas automaticamente.",
                        "",
                        "Confirme para continuar.",
                        "",
                        "Deseja continuar?",
                    ]
                ),
                CONFIRM_PAUSE,
                screen="confirm_pause",
            )
        if callback == CB_CANCEL:
            return BotResponse("Ação cancelada.", MAIN_MENU)
        if callback == CB_MT5_CONFIRM_REMOVE:
            account = self.mt5_accounts.first_account(user.id)
            if account is not None:
                self.mt5_accounts.remove_account(user.id, account.id)
            return BotResponse(
                "Conta MT5 removida com segurança.",
                mt5_connect_menu(self.mt5_onboarding_url),
                screen="mt5_connect",
            )
        if callback == CB_CONFIRM_ACTIVATE:
            return BotResponse(
                "\n".join(
                    [
                        "🔐 APROVAÇÃO NECESSÁRIA",
                        "",
                        "A ativação direta pelo cliente foi desabilitada.",
                        "",
                        "O administrador precisa registrar o pagamento, definir a validade e aprovar seu acesso.",
                    ]
                ),
                MAIN_MENU,
                screen="activation_pending",
            )
        if callback == CB_CONFIRM_PAUSE:
            self.users.set_status(user.id, USER_STATUS_PAUSED)
            self._log_admin_if_needed(telegram_user_id, user.id, "pause")
            return BotResponse(
                "\n".join(
                    [
                        "⏸️ COPIADOR PAUSADO",
                        "",
                        "Novas entradas estão bloqueadas.",
                        "",
                        "As operações que já estiverem abertas não serão alteradas.",
                    ]
                ),
                PAUSED_MENU,
                screen="paused",
            )

        if callback == "v1:r:mode":
            return BotResponse("📦 MODO DE LOTE", RISK_MODE_MENU, screen="risk")
        if callback == "v1:r:lot":
            return BotResponse("✏️ LOTE FIXO\n\nSelecione o lote total por sinal.", FIXED_LOT_MENU, screen="risk")
        if callback == "v1:r:risk":
            return BotResponse("📊 RISCO PERCENTUAL\n\nSelecione o risco máximo por sinal.", RISK_PERCENT_MENU, screen="risk")
        if callback == "v1:r:target":
            return BotResponse("🎯 META DIÁRIA\n\nSelecione o lucro diário em moeda da conta.", DAILY_TARGET_MENU, screen="risk")
        if callback == "v1:r:loss":
            return BotResponse("🛑 LIMITE DE PERDA\n\nSelecione a perda diária em moeda da conta.", DAILY_LOSS_MENU, screen="risk")
        if callback == "v1:r:max":
            return BotResponse("🔢 MÁXIMO DE OPERAÇÕES\n\nSelecione o máximo de grupos ativos.", MAX_OPEN_MENU, screen="risk")
        if callback == "v1:r:spread":
            return BotResponse("📏 SPREAD MÁXIMO\n\nSelecione o limite em pontos do símbolo.", SPREAD_MENU, screen="risk")
        if callback == "v1:r:slippage":
            return BotResponse("🎚️ SLIPPAGE\n\nSelecione o desvio máximo em pontos.", SLIPPAGE_MENU, screen="risk")
        if callback in {"v1:r:mode:fixed_lot", "v1:r:mode:risk_percent"}:
            account = self.mt5_accounts.first_account(user.id)
            if account is None:
                return self._connect_mt5_screen()
            risk_mode = callback.rsplit(":", 1)[-1]
            self.mt5_accounts.update_execution_profile_field(user.id, account.id, "risk_mode", risk_mode)
            self.settings.update_risk_mode(user.id, risk_mode)
            return self._risk_screen(user)

        fixed_lot = extract_fixed_lot(callback)
        if fixed_lot is not None:
            try:
                self._apply_risk_value(user, "lot", fixed_lot)
            except ValueError as exc:
                return BotResponse(str(exc), RISK_MENU, screen="risk")
            return BotResponse(f"Lote fixo atualizado para {fixed_lot}.", RISK_MENU, screen="risk")

        risk_value = extract_risk_value(callback)
        if risk_value is not None:
            field, raw_value = risk_value
            try:
                self._apply_risk_value(user, field, raw_value)
            except ValueError as exc:
                return BotResponse(str(exc), RISK_MENU, screen="risk")
            return self._risk_screen(user)

        if callback == "v1:p:be":
            settings = self.settings.ensure_defaults(user.id)
            account = self.mt5_accounts.first_account(user.id)
            current = settings.breakeven_enabled
            if account is not None:
                current = self.mt5_accounts.ensure_execution_profile(user.id, account.id).breakeven_enabled
            updated = self.settings.update_breakeven_enabled(user.id, not current)
            if account is not None:
                self.mt5_accounts.update_execution_profile_field(
                    user.id, account.id, "breakeven_enabled", int(updated.breakeven_enabled)
                )
            return self._protections_screen(user)
        if callback == "v1:p:tr":
            settings = self.settings.ensure_defaults(user.id)
            account = self.mt5_accounts.first_account(user.id)
            current = settings.trailing_enabled
            if account is not None:
                current = self.mt5_accounts.ensure_execution_profile(user.id, account.id).trailing_enabled
            updated = self.settings.update_trailing_enabled(user.id, not current)
            if account is not None:
                self.mt5_accounts.update_execution_profile_field(
                    user.id, account.id, "trailing_enabled", int(updated.trailing_enabled)
                )
            return self._protections_screen(user)
        if callback == "v1:p:loss":
            return BotResponse(
                "🛑 LIMITE DIÁRIO\n\nUse o menu de gestão de risco para ajustar o limite de perda diária.",
                PROTECTIONS_MENU,
                screen="protections",
            )
        return BotResponse("Ação invalida ou expirada.", MAIN_MENU)

    def _apply_risk_value(self, user: User, field: str, raw_value: str) -> None:
        account = self.mt5_accounts.first_account(user.id)
        if field == "lot":
            settings = self.settings.update_fixed_lot(user.id, raw_value)
            if account is not None:
                self.mt5_accounts.update_execution_profile_fixed_lot(
                    user.id, account.id, settings.fixed_lot
                )
            return

        if account is None:
            raise ValueError("Conecte uma conta MT5 antes de alterar esta configuração.")

        profile_field = {
            "risk": "risk_percent",
            "target": "daily_profit_target",
            "loss": "daily_loss_limit",
            "max": "max_open_signals",
            "spread": "max_spread_points",
            "slippage": "max_slippage_points",
        }.get(field)
        if profile_field is None:
            raise ValueError("Campo de configuração inválido.")

        if field == "risk":
            self.settings.update_risk_percent(user.id, raw_value)
        elif field == "target":
            self.settings.update_daily_profit_target(user.id, raw_value)
        elif field == "loss":
            self.settings.update_daily_loss_limit(user.id, raw_value)
        elif field == "max":
            self.settings.update_max_open_trades(user.id, int(Decimal(raw_value)))
        elif Decimal(raw_value) > Decimal("100000"):
            raise ValueError("O limite máximo permitido é 100000 pontos.")

        self.mt5_accounts.update_execution_profile_risk_value(
            user.id, account.id, profile_field, raw_value
        )

    def _ensure_user(self, telegram_user_id: int, telegram_username: str | None) -> User:
        user = self.users.get_or_create_user(telegram_user_id, telegram_username)
        self.settings.ensure_defaults(user.id)
        return user

    def _screen_for_name(self, screen: str, user: User, first_name: str | None = None) -> BotResponse:
        if screen == "main":
            return self._main_panel(user, first_name=first_name)
        if screen == "account":
            return self._account_screen(user)
        if screen == "operations":
            return self._operations_screen(user)
        if screen == "risk":
            return self._risk_screen(user)
        if screen == "protections":
            return self._protections_screen(user)
        if screen == "history":
            return self._history_screen(user)
        if screen == "connection":
            return self._connection_screen(user)
        if screen == "mt5_accounts":
            return self._mt5_accounts_screen(user)
        if screen == "channels":
            return self._channels_screen(user)
        return self._main_panel(user, first_name=first_name)

    def _channels_screen(self, user: User) -> BotResponse:
        overview = self.channels.user_overview(user.id)
        mode = str(overview["selection_mode"])
        all_channels = list(overview["channels"])
        channels = all_channels[:30]
        requests = list(overview["requests"])
        channel_lines = ["Nenhum canal aprovado no catálogo."]
        toggle_rows: list[tuple[Button, ...]] = []
        if channels:
            channel_lines = []
            for channel in channels:
                enabled = mode == "all" or bool(channel["enabled"])
                marker = "✅" if enabled else "⚪"
                channel_lines.append(f"{marker} {channel['title']}")
                if mode == "custom":
                    toggle_rows.append(
                        (
                            Button(
                                f"{marker} {channel['title']}"[:55],
                                f"v1:ch:t:{channel['id']}",
                            ),
                        )
                    )
        request_lines: list[str] = []
        if requests:
            request_lines = ["", "Sugestões em análise:"]
            request_lines.extend(
                f"• @{request['username']} — {channel_request_status_label(str(request['status']))}"
                for request in requests
            )
        if len(all_channels) > len(channels):
            channel_lines.append(
                f"… e mais {len(all_channels) - len(channels)} canal(is) ativos."
            )
        mode_label = "todos os canais aprovados" if mode == "all" else "seleção personalizada"
        keyboard = tuple(toggle_rows) + CHANNEL_MENU
        return BotResponse(
            "\n".join(
                [
                    "📻 CANAIS DE SINAIS",
                    "",
                    f"Modo atual: {mode_label}",
                    "",
                    *channel_lines,
                    *request_lines,
                    "",
                    "Somente canais analisados e aprovados pelo administrador podem gerar operações.",
                ]
            ),
            keyboard,
            screen="channels",
        )

    def _main_panel(self, user: User, first_name: str | None = None) -> BotResponse:
        name = first_name or user.telegram_username or "trader"
        status_label, operations_label = self._status_labels(user)
        mt5_account = self.mt5_accounts.first_account(user.id)
        mt5_status = "⚪ Não conectado"
        if mt5_account is not None:
            mt5_status = mt5_account_connection_label(mt5_account.connection_status)
        admin_url = (
            admin_panel_url(self.mt5_onboarding_url)
            if user.telegram_user_id in self.admin_ids
            else None
        )
        return BotResponse(
            "\n".join(
                [
                    "🤖 INSTITUTO TRADER",
                    "",
                    f"Olá, {name}! 👋",
                    "",
                    f"Status do copiador: {status_label}",
                    f"MetaTrader 5: {mt5_status}",
                    f"Novas operações: {operations_label}",
                    "",
                    "Gerencie sua conta utilizando o menu abaixo.",
                    "bot privado de gestão.",
                ]
            ),
            main_menu(admin_url),
            screen="main",
        )

    def _account_screen(self, user: User) -> BotResponse:
        status_label, _operations_label = self._status_labels(user)
        account = self.mt5_accounts.first_account(user.id)
        connection_status = "⚪ Não conectado"
        balance = "Aguardando conexão"
        equity = "Aguardando conexão"
        daily_result = "Aguardando atualização do Worker MT5"
        integration_message = "As informações financeiras serão exibidas após a integração com o MetaTrader 5."
        if account is not None:
            connection_status = mt5_account_connection_label(account.connection_status)
            balance = money_label(account.balance)
            equity = money_label(account.equity)
            daily_result = daily_performance_label(
                self.mt5_accounts.daily_performance(account.id)
            )
            integration_message = "Informações atualizadas pela conexão com o MetaTrader 5."
        return BotResponse(
            "\n".join(
                [
                    "💼 MINHA CONTA",
                    "",
                    f"Status do copiador: {status_label}",
                    f"MetaTrader 5: {connection_status}",
                    "",
                    f"Saldo: {balance}",
                    f"Equity: {equity}",
                    f"Resultado do dia: {daily_result}",
                    "",
                    integration_message,
                ]
            ),
            ACCOUNT_MENU,
            screen="account",
        )

    def _operations_screen(self, user: User) -> BotResponse:
        account = self.mt5_accounts.first_account(user.id)
        connection = mt5_account_connection_label(account.connection_status) if account else "⚪ Não conectado"
        with connect_database(self.mt5_accounts.database_path) as database:
            cursor = database.execute(
                """
                SELECT g.symbol, g.direction, g.total_volume, g.selected_entry_price,
                       g.stop_loss, g.status, COUNT(o.id)
                FROM execution_groups g
                LEFT JOIN execution_orders o ON o.execution_group_id = g.id
                WHERE g.user_id = ?
                  AND g.status IN ('pending_submission', 'pending_active')
                GROUP BY g.id
                ORDER BY g.id DESC LIMIT 10
                """,
                (user.id,),
            )
            try:
                rows = cursor.fetchall()
            finally:
                cursor.close()
        operation_lines = ["Nenhuma operação do copiador registrada como ativa."]
        if rows:
            operation_lines = [
                f"• {row[0]} {row[1]} | lote {row[2]} | entrada {row[3]} | SL {row[4]} | {row[6]} TP(s) | {row[5]}"
                for row in rows
            ]
        return BotResponse(
            "\n".join(
                [
                    "📈 OPERAÇÕES",
                    "",
                    f"MetaTrader 5: {connection}",
                    "",
                    *operation_lines,
                    "",
                    "A tela mostra operações registradas pelo copiador; a reconciliação de lucro flutuante é feita separadamente.",
                ]
            ),
            OPERATIONS_MENU,
            screen="operations",
        )

    def _risk_screen(self, user: User) -> BotResponse:
        settings = self.settings.ensure_defaults(user.id)
        account = self.mt5_accounts.first_account(user.id)
        profile = self.mt5_accounts.ensure_execution_profile(user.id, account.id) if account else None
        risk_mode = profile.risk_mode if profile else settings.risk_mode
        fixed_lot = profile.fixed_lot if profile else settings.fixed_lot
        risk_percent = profile.risk_percent if profile else settings.risk_percent
        daily_profit_target = profile.daily_profit_target if profile else settings.daily_profit_target
        daily_loss_limit = profile.daily_loss_limit if profile else settings.daily_loss_limit
        max_open_trades = profile.max_open_signals if profile else settings.max_open_trades
        max_spread_points = profile.max_spread_points if profile else None
        max_slippage_points = profile.max_slippage_points if profile else None
        return BotResponse(
            "\n".join(
                [
                    "⚙️ GESTÃO DE RISCO",
                    "",
                    "Modo de gestão:",
                    risk_mode_label(risk_mode),
                    "",
                    "Lote fixo:",
                    setting_value(fixed_lot),
                    "",
                    "Risco por operação:",
                    percent_value(risk_percent),
                    "",
                    "Meta diária:",
                    money_value(daily_profit_target),
                    "",
                    "Limite de perda diária:",
                    money_value(daily_loss_limit),
                    "",
                    "Máximo de operações:",
                    str(max_open_trades) if max_open_trades else "Não configurado",
                    "",
                    f"Spread máximo: {max_spread_points if max_spread_points is not None else 'Não configurado'} pontos",
                    f"Slippage máximo: {max_slippage_points if max_slippage_points is not None else 'Não configurado'} pontos",
                    "",
                    "Selecione uma configuração:",
                ]
            ),
            RISK_MENU,
            screen="risk",
        )

    def _protections_screen(self, user: User) -> BotResponse:
        settings = self.settings.ensure_defaults(user.id)
        account = self.mt5_accounts.first_account(user.id)
        profile = self.mt5_accounts.ensure_execution_profile(user.id, account.id) if account else None
        breakeven_enabled = profile.breakeven_enabled if profile else settings.breakeven_enabled
        trailing_enabled = profile.trailing_enabled if profile else settings.trailing_enabled
        daily_loss_limit = profile.daily_loss_limit if profile else settings.daily_loss_limit
        return BotResponse(
            "\n".join(
                [
                    "🛡️ PROTEÇÕES",
                    "",
                    "Breakeven: obrigatório após TP1",
                    "🟢 Ativado",
                    "",
                    "BE antecipado ao avançar 1R:",
                    enabled_label(breakeven_enabled),
                    "",
                    "Trailing Stop:",
                    enabled_label(trailing_enabled),
                    "",
                    "Limite diário:",
                    configured_label(daily_loss_limit > 0),
                    "",
                    "Ao atingir TP1, o Worker move as posições restantes do mesmo sinal para a entrada. O BE antecipado e o trailing são avaliados após avanço de 1R; o trailing nunca afasta o stop.",
                ]
            ),
            PROTECTIONS_MENU,
            screen="protections",
        )

    def _history_screen(self, user: User) -> BotResponse:
        with connect_database(self.mt5_accounts.database_path) as database:
            cursor = database.execute(
                """
                SELECT created_at, symbol, direction, total_volume, status,
                       COALESCE(error_message, '')
                FROM execution_groups WHERE user_id = ?
                ORDER BY id DESC LIMIT 10
                """,
                (user.id,),
            )
            try:
                rows = cursor.fetchall()
            finally:
                cursor.close()
        lines = ["Nenhuma execução registrada até agora."]
        if rows:
            lines = [
                f"• {row[0]} | {row[1]} {row[2]} | lote {row[3]} | {row[4]}"
                + (f" | {row[5]}" if row[5] else "")
                for row in rows
            ]
        else:
            recent_commands = self.commands.recent_for_user(user.id, limit=10)
            if recent_commands:
                lines = [
                    f"• {item['created_at']} | {item['command_type']} | {item['status']}"
                    for item in recent_commands
                ]

        return BotResponse(
            "\n".join(
                [
                    "📋 HISTÓRICO",
                    "",
                    *lines,
                ]
            ),
            HISTORY_MENU,
            screen="history",
        )

    def _connection_screen(self, user: User) -> BotResponse:
        account = self.mt5_accounts.first_account(user.id)
        mt5_status = "⚪ Não conectado"
        if account is not None:
            mt5_status = mt5_account_connection_label(account.connection_status)
        monitor_status = signal_monitor_connection_label(
            get_service_heartbeat(self.database_path, SIGNAL_MONITOR_SERVICE_NAME)
        )
        return BotResponse(
            "\n".join(
                [
                    "📡 STATUS DA CONEXÃO",
                    "",
                    "Bot de gestão:",
                    "🟢 Online",
                    "",
                    "Monitor de sinais:",
                    monitor_status,
                    "",
                    "MetaTrader 5:",
                    mt5_status,
                    "",
                    "Última atualização:",
                    time.strftime("%Y-%m-%d %H:%M:%S"),
                ]
            ),
            CONNECTION_MENU,
            screen="connection",
        )

    def _connect_mt5_screen(self) -> BotResponse:
        return BotResponse(
            "\n".join(
                [
                    "🔗 CONECTAR CONTA MT5",
                    "",
                    "Nenhuma conta conectada.",
                    "",
                    "Cadastre uma conta MT5 para validar a conexão.",
                ]
            ),
            mt5_connect_menu(self.mt5_onboarding_url),
            screen="mt5_connect",
        )

    def _mt5_accounts_screen(self, user: User) -> BotResponse:
        account = self.mt5_accounts.first_account(user.id)
        if account is None:
            return self._connect_mt5_screen()

        status_label, operations_label = self._status_labels(user)
        error_lines = ["", f"Erro: {account.last_error}"] if account.last_error else []
        return BotResponse(
            "\n".join(
                [
                    "🖥️ MINHAS CONTAS",
                    "",
                    f"Conta: {account.masked_login}",
                    f"Servidor: {account.server_name}",
                    f"Tipo: {account_type_label(account)}",
                    f"Modo: {account_mode_label(account)}",
                    f"Saldo: {money_label(account.balance)}",
                    f"Equity: {money_label(account.equity)}",
                    f"Conexão: {mt5_account_connection_label(account.connection_status)}",
                    f"Heartbeat: {account.worker_heartbeat_at or 'Aguardando worker'}",
                    f"Copiador: {status_label} / {operations_label}",
                    *error_lines,
                ]
            ),
            MT5_ACCOUNTS_MENU,
            screen="mt5_accounts",
        )

    def _signal_execution_screen(self, user: User) -> BotResponse:
        account = self.mt5_accounts.first_account(user.id)
        if account is None:
            return self._connect_mt5_screen()
        profile = self.mt5_accounts.ensure_execution_profile(user.id, account.id)
        return BotResponse(
            "\n".join(
                [
                    "⚙️ EXECUÇÃO DOS SINAIS",
                    "",
                    "Modo de entrada:",
                    entry_execution_label(profile.entry_execution_mode),
                    "",
                    "Preço dentro da faixa:",
                    (
                        "Não se aplica à entrada imediata"
                        if profile.entry_execution_mode
                        == ENTRY_EXECUTION_MARKET_IMMEDIATE
                        else entry_price_label(profile.entry_price_mode)
                    ),
                    "",
                    "Validade:",
                    expiration_minutes_label(profile.pending_expiration_minutes),
                    "",
                    "Take Profits utilizados:",
                    take_profit_limit_label(profile.take_profit_limit),
                    "",
                    "Gestão após TP1:",
                    "✅ Demais posições protegidas no breakeven",
                ]
            ),
            SIGNAL_EXECUTION_MENU,
            screen="signal_execution",
        )

    def _status_labels(self, user: User) -> tuple[str, str]:
        if user.status != USER_STATUS_ACTIVE:
            return "🟡 Aguardando aprovação", "⏸️ Bloqueadas"
        access = paid_access_decision(self.database_path, user.id)
        if access.allowed:
            return "🟢 Ativo", "▶️ Liberadas"
        if access.reason == ACCESS_EXPIRED:
            return "🔴 Acesso expirado", "⏸️ Bloqueadas"
        return "🟡 Aguardando aprovação", "⏸️ Bloqueadas"

    def _log_admin_if_needed(self, telegram_user_id: int, target_user_id: int, action_type: str) -> None:
        if telegram_user_id not in self.admin_ids:
            return
        self.users.log_admin_action(
            admin_telegram_user_id=telegram_user_id,
            target_user_id=target_user_id,
            action_type=action_type,
            payload={"source": "bot_callback"},
        )


def status_labels(status: str) -> tuple[str, str]:
    if status == USER_STATUS_ACTIVE:
        return "🟢 Ativo", "▶️ Liberadas"
    return "🟡 Pausado", "⏸️ Bloqueadas"


def admin_panel_url(onboarding_url: str | None) -> str | None:
    if not onboarding_url:
        return None
    parsed = urlsplit(onboarding_url)
    if parsed.scheme != "https" or not parsed.netloc:
        return None
    path = parsed.path.rstrip("/")
    path = f"{path}/admin" if path else "/admin"
    return urlunsplit((parsed.scheme, parsed.netloc, path, parsed.query, ""))


def connection_label(status: str) -> str:
    if status == CONNECTION_STATUS_CONNECTED:
        return "🟢 Conectado"
    return "⚪ Não conectado"


def mt5_account_connection_label(status: str) -> str:
    if status == CONNECTION_STATUS_CONNECTED:
        return "🟢 Conectada"
    return "🔴 Desconectada"


def signal_monitor_connection_label(
    heartbeat_at: str | None,
    *,
    now: datetime | None = None,
    stale_after_seconds: int = 45,
) -> str:
    if not heartbeat_at:
        return "⚪ Aguardando inicialização"
    try:
        heartbeat = datetime.fromisoformat(heartbeat_at)
        if heartbeat.tzinfo is None:
            heartbeat = heartbeat.replace(tzinfo=timezone.utc)
    except ValueError:
        return "🔴 Offline"

    current_time = now or datetime.now(tz=timezone.utc)
    if current_time - heartbeat <= timedelta(seconds=stale_after_seconds):
        return "🟢 Online"
    return "🔴 Offline"


def account_type_label(account: MT5Account) -> str:
    if account.account_type == "real":
        return "Real"
    return "Demonstração"


def account_mode_label(account: MT5Account) -> str:
    if account.account_mode == "netting":
        return "Netting"
    return "Hedging"


def money_label(value: Decimal | None) -> str:
    if value is None:
        return "Indisponível"
    return f"$ {format(value, '.2f')}"


def daily_performance_label(performance: object | None) -> str:
    if performance is None:
        return "Aguardando atualização do Worker MT5"
    profit = Decimal(str(getattr(performance, "realized_profit")))
    percentage_value = getattr(performance, "return_percent")
    percentage = Decimal(str(percentage_value)) if percentage_value is not None else None
    marker = "🟢" if profit > 0 else "🔴" if profit < 0 else "⚪"
    signed_profit = f"{profit:+.2f}"
    signed_percentage = f"{percentage:+.2f}%" if percentage is not None else "percentual indisponível"
    return f"{marker} $ {signed_profit} ({signed_percentage})"


def entry_execution_label(value: str) -> str:
    if value == ENTRY_EXECUTION_MARKET_IMMEDIATE:
        return "🚀 Entrada imediata ao preço atual"
    if value == ENTRY_EXECUTION_MARKET_ON_ZONE:
        return "⚖️ Mercado se estiver na zona; caso contrário, ordem posicionada"
    return "📍 Ordem posicionada na entrada indicada"


def entry_price_label(value: str) -> str:
    labels = {
        ENTRY_PRICE_FIRST_TOUCH: "Primeiro toque",
        ENTRY_PRICE_MIDDLE: "Meio da faixa",
        ENTRY_PRICE_DISTRIBUTED: "Distribuir na faixa",
    }
    return labels.get(value, "Primeiro toque")


def expiration_minutes_label(value: int) -> str:
    if value == 30:
        return "30 minutos"
    if value == 60:
        return "1 hora"
    if value == 120:
        return "2 horas"
    if value == 240:
        return "4 horas"
    if value >= 1440:
        return "Até o fim do dia"
    return f"{value} minutos"


def take_profit_limit_label(value: int) -> str:
    if value <= 0:
        return "Todos os TPs enviados pelo canal"
    if value == 1:
        return "Somente TP1"
    return f"TP1 até TP{value}"


def financial_value(value: str | None) -> str:
    return f"$ {value}" if value else "Aguardando conexão"


def setting_value(value: Decimal | str | None) -> str:
    if value is None:
        return "Não configurado"
    return decimal_to_storage(value if isinstance(value, Decimal) else Decimal(str(value)))


def percent_value(value: Decimal | None) -> str:
    if value is None:
        return "Não configurado"
    return f"{decimal_to_storage(value)}%"


def money_value(value: Decimal | None) -> str:
    if value is None or value == 0:
        return "Não configurado"
    return f"$ {decimal_to_storage(value)}"


def normalize_custom_numeric_value(raw_value: str, *, integer: bool = False) -> str:
    value = raw_value.strip().replace("$", "").replace("%", "").replace(" ", "")
    if not value:
        raise ValueError("envie um número.")
    if "," in value and "." in value:
        if value.rfind(",") > value.rfind("."):
            value = value.replace(".", "").replace(",", ".")
        else:
            value = value.replace(",", "")
    else:
        value = value.replace(",", ".")
    try:
        number = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError("use apenas números, vírgula ou ponto decimal.") from exc
    if not number.is_finite():
        raise ValueError("o número precisa ser finito.")
    if integer and number != number.to_integral_value():
        raise ValueError("este campo aceita somente números inteiros.")
    return decimal_to_storage(number)


def custom_value_prompt(field: str) -> str:
    instructions = {
        "lot": "Envie o lote entre 0,01 e 100. Exemplo: 0,07",
        "risk": "Envie o risco percentual entre 0,1% e 10%. Exemplo: 0,75",
        "target": "Envie a meta diária em dólar, ou 0 para desativar. Exemplo: $ 150",
        "loss": "Envie o limite diário em dólar, ou 0 para desativar. Exemplo: $ 75",
        "max": "Envie um número inteiro entre 1 e 100.",
        "spread": "Envie o spread máximo em pontos, usando um número inteiro.",
        "slippage": "Envie o slippage máximo em pontos, usando um número inteiro.",
    }
    return f"✍️ VALOR PERSONALIZADO\n\n{instructions[field]}\n\nEnvie somente o valor na próxima mensagem."


def custom_field_label(field: str) -> str:
    return {
        "lot": "Lote fixo",
        "risk": "Risco percentual",
        "target": "Meta diária",
        "loss": "Limite de perda diária",
        "max": "Máximo de operações",
        "spread": "Spread máximo",
        "slippage": "Slippage máximo",
    }[field]


def custom_value_label(field: str, value: str) -> str:
    if field in {"target", "loss"}:
        return f"$ {value}"
    if field == "risk":
        return f"{value}%"
    if field in {"spread", "slippage"}:
        return f"{value} pontos"
    return value


def channel_request_status_label(status: str) -> str:
    return {
        REQUEST_PENDING_ACCESS: "aguardando verificação",
        REQUEST_AWAITING_MEMBERSHIP: "aguardando o administrador entrar no canal",
        REQUEST_READY_REVIEW: "aguardando aprovação do administrador",
        REQUEST_APPROVED: "aprovado",
    }.get(status, status)


def risk_mode_label(value: str) -> str:
    labels = {
        "fixed_lot": "Lote fixo",
        "risk_percent": "Risco percentual",
    }
    return labels.get(value, "Não configurado")


def enabled_label(value: bool) -> str:
    return "🟢 Ativado" if value else "⚪ Desativado"


def configured_label(value: bool) -> str:
    return "🟢 Configurado" if value else "⚪ Não configurado"
