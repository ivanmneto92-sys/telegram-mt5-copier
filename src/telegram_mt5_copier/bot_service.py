from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
import time

from .account_service import AccountService
from .bot_keyboards import (
    ACCOUNT_MENU,
    ACTIVATED_MENU,
    CB_ACCOUNT,
    CB_ACTIVATE,
    CB_CANCEL,
    CB_CONFIRM_ACTIVATE,
    CB_CONFIRM_PAUSE,
    CB_CONNECTION,
    CB_HISTORY,
    CB_MAIN,
    CB_OPERATIONS,
    CB_PAUSE,
    CB_PROTECTIONS,
    CB_RISK,
    CONFIRM_ACTIVATE,
    CONFIRM_PAUSE,
    CONNECTION_MENU,
    HISTORY_MENU,
    MAIN_MENU,
    OPERATIONS_MENU,
    PAUSED_MENU,
    PROTECTIONS_MENU,
    RISK_MENU,
    extract_fixed_lot,
    extract_refresh_screen,
    is_valid_callback_data,
    normalize_callback_data,
)
from .command_queue import CommandQueue
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
    ) -> None:
        self.users = UserRepository(database_path)
        self.settings = SettingsService(database_path)
        self.commands = CommandQueue(database_path)
        self.account_service = account_service or AccountService()
        self.admin_ids = set(admin_ids)
        self.rate_limiter = rate_limiter or RateLimiter()

    def start(
        self,
        telegram_user_id: int,
        telegram_username: str | None,
        first_name: str | None = None,
    ) -> BotResponse:
        user = self._ensure_user(telegram_user_id, telegram_username)
        return self._main_panel(user, first_name=first_name)

    def menu(
        self,
        telegram_user_id: int,
        telegram_username: str | None,
        first_name: str | None = None,
    ) -> BotResponse:
        user = self._ensure_user(telegram_user_id, telegram_username)
        return self._main_panel(user, first_name=first_name)

    def status(
        self,
        telegram_user_id: int,
        telegram_username: str | None,
        first_name: str | None = None,
    ) -> BotResponse:
        user = self._ensure_user(telegram_user_id, telegram_username)
        return self._connection_screen(user)

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

        refresh_screen = extract_refresh_screen(callback)
        if refresh_screen is not None:
            return self._screen_for_name(refresh_screen, user, first_name=first_name)

        if callback == CB_MAIN:
            return self._main_panel(user, first_name=first_name)
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
        if callback == CB_ACTIVATE:
            return BotResponse(
                "\n".join(
                    [
                        "▶️ ATIVAR COPIADOR",
                        "",
                        "Ao confirmar, o sistema ficará autorizado a receber novos sinais e criar novas operações quando o MT5 estiver conectado.",
                        "",
                        "Confirme para continuar.",
                        "",
                        "Deseja continuar?",
                    ]
                ),
                CONFIRM_ACTIVATE,
                screen="confirm_activate",
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
        if callback == CB_CONFIRM_ACTIVATE:
            self.users.set_status(user.id, USER_STATUS_ACTIVE)
            self.commands.enqueue(user.id, "set_user_status", {"status": USER_STATUS_ACTIVE})
            self._log_admin_if_needed(telegram_user_id, user.id, "activate")
            return BotResponse(
                "\n".join(
                    [
                        "✅ COPIADOR ATIVADO",
                        "",
                        "Copiador ativado com sucesso.",
                        "",
                        "Novos sinais estão liberados.",
                        "",
                        "MetaTrader 5: ⚪ Não conectado",
                        "",
                        "A configuração foi salva. A execução automática ficará disponível após a integração com o MT5.",
                    ]
                ),
                ACTIVATED_MENU,
                screen="activated",
            )
        if callback == CB_CONFIRM_PAUSE:
            self.users.set_status(user.id, USER_STATUS_PAUSED)
            self.commands.enqueue(user.id, "set_user_status", {"status": USER_STATUS_PAUSED})
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

        fixed_lot = extract_fixed_lot(callback)
        if fixed_lot is not None:
            try:
                settings = self.settings.update_fixed_lot(user.id, fixed_lot)
            except ValueError as exc:
                return BotResponse(str(exc), RISK_MENU, screen="risk")
            self.commands.enqueue(user.id, "update_fixed_lot", {"fixed_lot": str(settings.fixed_lot)})
            return BotResponse(f"Lote fixo atualizado para {settings.fixed_lot}.", RISK_MENU, screen="risk")

        if callback == "v1:p:be":
            settings = self.settings.ensure_defaults(user.id)
            updated = self.settings.update_breakeven_enabled(user.id, not settings.breakeven_enabled)
            self.commands.enqueue(user.id, "update_breakeven", {"enabled": updated.breakeven_enabled})
            return self._protections_screen(user)
        if callback == "v1:p:tr":
            settings = self.settings.ensure_defaults(user.id)
            updated = self.settings.update_trailing_enabled(user.id, not settings.trailing_enabled)
            self.commands.enqueue(user.id, "update_trailing", {"enabled": updated.trailing_enabled})
            return self._protections_screen(user)
        if callback == "v1:p:loss":
            return BotResponse(
                "🛑 LIMITE DIÁRIO\n\nUse o menu de gestão de risco para ajustar o limite de perda diária.",
                PROTECTIONS_MENU,
                screen="protections",
            )
        if callback.startswith("v1:r:"):
            return BotResponse(
                "Configuração disponível no banco. A edição detalhada será conectada na próxima etapa.",
                RISK_MENU,
                screen="risk",
            )

        return BotResponse("Ação invalida ou expirada.", MAIN_MENU)

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
        return self._main_panel(user, first_name=first_name)

    def _main_panel(self, user: User, first_name: str | None = None) -> BotResponse:
        name = first_name or user.telegram_username or "trader"
        status_label, operations_label = status_labels(user.status)
        return BotResponse(
            "\n".join(
                [
                    "🤖 INSTITUTO TRADER",
                    "",
                    f"Olá, {name}! 👋",
                    "",
                    f"Status do copiador: {status_label}",
                    "MetaTrader 5: ⚪ Não conectado",
                    f"Novas operações: {operations_label}",
                    "",
                    "Gerencie sua conta utilizando o menu abaixo.",
                    "bot privado de gestão.",
                ]
            ),
            MAIN_MENU,
            screen="main",
        )

    def _account_screen(self, user: User) -> BotResponse:
        status_label, _operations_label = status_labels(user.status)
        summary = self.account_service.get_account_summary(user.id)
        return BotResponse(
            "\n".join(
                [
                    "💼 MINHA CONTA",
                    "",
                    f"Status do copiador: {status_label}",
                    f"MetaTrader 5: {connection_label(summary.connection_status)}",
                    "",
                    f"Saldo: {financial_value(summary.balance)}",
                    f"Equity: {financial_value(summary.equity)}",
                    f"Resultado do dia: {financial_value(summary.daily_result)}",
                    "",
                    "As informações financeiras serão exibidas após a integração com o MetaTrader 5.",
                ]
            ),
            ACCOUNT_MENU,
            screen="account",
        )

    def _operations_screen(self, user: User) -> BotResponse:
        _positions = self.account_service.get_open_positions(user.id)
        return BotResponse(
            "\n".join(
                [
                    "📈 OPERAÇÕES",
                    "",
                    "MetaTrader 5: ⚪ Não conectado",
                    "",
                    "Ainda não é possível consultar operações abertas.",
                    "",
                    "Após a integração com o MT5, esta área exibirá:",
                    "",
                    "• Ativo",
                    "• Compra ou venda",
                    "• Lote",
                    "• Preço de entrada",
                    "• Stop Loss",
                    "• Take Profits",
                    "• Resultado atual",
                ]
            ),
            OPERATIONS_MENU,
            screen="operations",
        )

    def _risk_screen(self, user: User) -> BotResponse:
        settings = self.settings.ensure_defaults(user.id)
        return BotResponse(
            "\n".join(
                [
                    "⚙️ GESTÃO DE RISCO",
                    "",
                    "Modo de gestão:",
                    risk_mode_label(settings.risk_mode),
                    "",
                    "Lote fixo:",
                    setting_value(settings.fixed_lot),
                    "",
                    "Risco por operação:",
                    percent_value(settings.risk_percent),
                    "",
                    "Meta diária:",
                    money_value(settings.daily_profit_target),
                    "",
                    "Limite de perda diária:",
                    money_value(settings.daily_loss_limit),
                    "",
                    "Máximo de operações:",
                    str(settings.max_open_trades) if settings.max_open_trades else "Não configurado",
                    "",
                    "Selecione uma configuração:",
                ]
            ),
            RISK_MENU,
            screen="risk",
        )

    def _protections_screen(self, user: User) -> BotResponse:
        settings = self.settings.ensure_defaults(user.id)
        return BotResponse(
            "\n".join(
                [
                    "🛡️ PROTEÇÕES",
                    "",
                    "Breakeven:",
                    enabled_label(settings.breakeven_enabled),
                    "",
                    "Trailing Stop:",
                    enabled_label(settings.trailing_enabled),
                    "",
                    "Limite diário:",
                    configured_label(settings.daily_loss_limit > 0),
                    "",
                    "As proteções serão aplicadas automaticamente após a integração com o MetaTrader 5.",
                ]
            ),
            PROTECTIONS_MENU,
            screen="protections",
        )

    def _history_screen(self, user: User) -> BotResponse:
        recent_commands = self.commands.recent_for_user(user.id, limit=10)
        if recent_commands:
            lines = [
                f"• {item['created_at']} - {item['command_type']} - {item['status']}"
                for item in recent_commands
            ]
        else:
            lines = ["Nenhum comando criado até agora."]

        return BotResponse(
            "\n".join(
                [
                    "📋 HISTÓRICO",
                    "",
                    "Nenhuma operação sincronizada com o MT5.",
                    "",
                    "Enquanto o MT5 não estiver conectado, este painel mostra alterações de status, gestão e comandos criados.",
                    "",
                    *lines,
                ]
            ),
            HISTORY_MENU,
            screen="history",
        )

    def _connection_screen(self, user: User) -> BotResponse:
        return BotResponse(
            "\n".join(
                [
                    "📡 STATUS DA CONEXÃO",
                    "",
                    "Bot de gestão:",
                    "🟢 Online",
                    "",
                    "Monitor de sinais:",
                    "⚪ Aguardando integração",
                    "",
                    "MetaTrader 5:",
                    "⚪ Não conectado",
                    "",
                    "Última atualização:",
                    time.strftime("%Y-%m-%d %H:%M:%S"),
                ]
            ),
            CONNECTION_MENU,
            screen="connection",
        )

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


def connection_label(status: str) -> str:
    if status == "connected":
        return "🟢 Conectado"
    return "⚪ Não conectado"


def financial_value(value: str | None) -> str:
    return value if value else "Aguardando conexão"


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
    return decimal_to_storage(value)


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
