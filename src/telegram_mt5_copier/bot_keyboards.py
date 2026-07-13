from __future__ import annotations

from dataclasses import dataclass
import re


@dataclass(frozen=True)
class Button:
    text: str
    callback_data: str


MAIN_MENU: tuple[tuple[Button, ...], ...] = (
    (
        Button("Minha conta", "menu:account"),
        Button("Operacoes", "menu:operations"),
    ),
    (
        Button("Ativar", "confirm:activate"),
        Button("Pausar", "confirm:pause"),
    ),
    (
        Button("Gestao de risco", "menu:risk"),
        Button("Protecoes", "menu:protections"),
    ),
    (
        Button("Historico", "menu:history"),
        Button("Atualizar", "menu:refresh"),
    ),
)

RISK_MENU: tuple[tuple[Button, ...], ...] = (
    (
        Button("Modo de lote", "risk:mode"),
        Button("Lote fixo", "risk:fixed_lot"),
    ),
    (
        Button("Risco percentual", "risk:risk_percent"),
        Button("Meta diaria", "risk:daily_profit_target"),
    ),
    (
        Button("Perda maxima diaria", "risk:daily_loss_limit"),
        Button("Maximo de operacoes", "risk:max_open_trades"),
    ),
    (
        Button("Quantidade de TPs", "risk:tp_distribution"),
        Button("Breakeven", "risk:breakeven"),
    ),
    (
        Button("Trailing stop", "risk:trailing"),
        Button("Voltar", "menu:main"),
    ),
)

CONFIRM_ACTIVATE: tuple[tuple[Button, ...], ...] = (
    (
        Button("Confirmar ativacao", "action:activate:confirm"),
        Button("Cancelar", "action:cancel"),
    ),
)

CONFIRM_PAUSE: tuple[tuple[Button, ...], ...] = (
    (
        Button("Confirmar pausa", "action:pause:confirm"),
        Button("Cancelar", "action:cancel"),
    ),
)

STATIC_CALLBACKS = {
    button.callback_data
    for keyboard in (MAIN_MENU, RISK_MENU, CONFIRM_ACTIVATE, CONFIRM_PAUSE)
    for row in keyboard
    for button in row
}

FIXED_LOT_CALLBACK_RE = re.compile(r"^set:fixed_lot:(?P<value>\d+(?:[\.,]\d{1,2})?)$")


def is_valid_callback_data(callback_data: str) -> bool:
    return callback_data in STATIC_CALLBACKS or bool(FIXED_LOT_CALLBACK_RE.fullmatch(callback_data))


def extract_fixed_lot(callback_data: str) -> str | None:
    match = FIXED_LOT_CALLBACK_RE.fullmatch(callback_data)
    if not match:
        return None
    return match.group("value")


def build_inline_keyboard(rows: tuple[tuple[Button, ...], ...]):
    try:
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    except ImportError as exc:
        raise RuntimeError("python-telegram-bot nao instalado.") from exc

    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(button.text, callback_data=button.callback_data) for button in row]
            for row in rows
        ]
    )
