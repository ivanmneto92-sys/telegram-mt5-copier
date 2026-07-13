from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import time

from .bot_keyboards import (
    CONFIRM_ACTIVATE,
    CONFIRM_PAUSE,
    MAIN_MENU,
    RISK_MENU,
    extract_fixed_lot,
    is_valid_callback_data,
)
from .command_queue import CommandQueue
from .settings_service import SettingsService
from .users import USER_STATUS_ACTIVE, USER_STATUS_PAUSED, UserRepository


@dataclass(frozen=True)
class BotResponse:
    text: str
    keyboard: tuple[tuple[object, ...], ...] | None = None


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
    ) -> None:
        self.users = UserRepository(database_path)
        self.settings = SettingsService(database_path)
        self.commands = CommandQueue(database_path)
        self.admin_ids = set(admin_ids)
        self.rate_limiter = rate_limiter or RateLimiter()

    def start(self, telegram_user_id: int, telegram_username: str | None) -> BotResponse:
        user = self.users.get_or_create_user(telegram_user_id, telegram_username)
        self.settings.ensure_defaults(user.id)
        return BotResponse(
            "Bem-vindo ao bot privado de gestao. MT5 ainda esta indisponivel nesta etapa.",
            MAIN_MENU,
        )

    def menu(self, telegram_user_id: int, telegram_username: str | None) -> BotResponse:
        self._ensure_user(telegram_user_id, telegram_username)
        return BotResponse("Menu principal.", MAIN_MENU)

    def status(self, telegram_user_id: int, telegram_username: str | None) -> BotResponse:
        user = self._ensure_user(telegram_user_id, telegram_username)
        settings = self.settings.ensure_defaults(user.id)
        return BotResponse(
            "\n".join(
                [
                    f"Status: {user.status}",
                    "MT5: indisponivel nesta etapa",
                    f"Modo de lote: {settings.risk_mode}",
                    f"Lote fixo: {settings.fixed_lot}",
                ]
            ),
            MAIN_MENU,
        )

    def handle_callback(
        self,
        telegram_user_id: int,
        telegram_username: str | None,
        callback_data: str,
    ) -> BotResponse:
        if not self.rate_limiter.allow(telegram_user_id):
            return BotResponse("Muitas acoes em pouco tempo. Aguarde alguns segundos.", MAIN_MENU)

        if not is_valid_callback_data(callback_data):
            return BotResponse("Acao invalida ou expirada.", MAIN_MENU)

        user = self._ensure_user(telegram_user_id, telegram_username)

        if callback_data == "menu:main":
            return BotResponse("Menu principal.", MAIN_MENU)
        if callback_data == "menu:account":
            return self.status(telegram_user_id, telegram_username)
        if callback_data == "menu:operations":
            return BotResponse("Operacoes: MT5 indisponivel nesta etapa.", MAIN_MENU)
        if callback_data == "menu:risk":
            return BotResponse("Gestao de risco. Escolha uma opcao para configurar.", RISK_MENU)
        if callback_data == "menu:protections":
            return BotResponse("Protecoes salvas no banco. Execucao MT5 ainda indisponivel.", RISK_MENU)
        if callback_data == "menu:history":
            return BotResponse("Historico operacional ainda indisponivel nesta etapa.", MAIN_MENU)
        if callback_data == "menu:refresh":
            return self.status(telegram_user_id, telegram_username)
        if callback_data == "confirm:activate":
            return BotResponse("Confirme a ativacao. Novas entradas poderao ser liberadas no futuro.", CONFIRM_ACTIVATE)
        if callback_data == "confirm:pause":
            return BotResponse("Confirme a pausa. Novas entradas ficarao bloqueadas.", CONFIRM_PAUSE)
        if callback_data == "action:cancel":
            return BotResponse("Acao cancelada.", MAIN_MENU)
        if callback_data == "action:activate:confirm":
            self.users.set_status(user.id, USER_STATUS_ACTIVE)
            self.commands.enqueue(user.id, "set_user_status", {"status": USER_STATUS_ACTIVE})
            self._log_admin_if_needed(telegram_user_id, user.id, "activate")
            return BotResponse("Usuario ativado. Configuracoes salvas.", MAIN_MENU)
        if callback_data == "action:pause:confirm":
            self.users.set_status(user.id, USER_STATUS_PAUSED)
            self.commands.enqueue(user.id, "set_user_status", {"status": USER_STATUS_PAUSED})
            self._log_admin_if_needed(telegram_user_id, user.id, "pause")
            return BotResponse("Usuario pausado. Novas entradas estarao bloqueadas.", MAIN_MENU)

        fixed_lot = extract_fixed_lot(callback_data)
        if fixed_lot is not None:
            try:
                settings = self.settings.update_fixed_lot(user.id, fixed_lot)
            except ValueError as exc:
                return BotResponse(str(exc), RISK_MENU)
            self.commands.enqueue(user.id, "update_fixed_lot", {"fixed_lot": str(settings.fixed_lot)})
            return BotResponse(f"Lote fixo atualizado para {settings.fixed_lot}.", RISK_MENU)

        if callback_data.startswith("risk:"):
            return BotResponse("Configuracao disponivel no banco; edicao detalhada vira na proxima etapa.", RISK_MENU)

        return BotResponse("Acao invalida ou expirada.", MAIN_MENU)

    def _ensure_user(self, telegram_user_id: int, telegram_username: str | None):
        user = self.users.get_or_create_user(telegram_user_id, telegram_username)
        self.settings.ensure_defaults(user.id)
        return user

    def _log_admin_if_needed(self, telegram_user_id: int, target_user_id: int, action_type: str) -> None:
        if telegram_user_id not in self.admin_ids:
            return
        self.users.log_admin_action(
            admin_telegram_user_id=telegram_user_id,
            target_user_id=target_user_id,
            action_type=action_type,
            payload={"source": "bot_callback"},
        )
