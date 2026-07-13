from __future__ import annotations

from dataclasses import dataclass
import re


@dataclass(frozen=True)
class Button:
    text: str
    callback_data: str


CB_MAIN = "v1:m"
CB_ACCOUNT = "v1:a"
CB_OPERATIONS = "v1:o"
CB_ACTIVATE = "v1:act"
CB_PAUSE = "v1:pause"
CB_RISK = "v1:r"
CB_PROTECTIONS = "v1:p"
CB_HISTORY = "v1:h"
CB_CONNECTION = "v1:c"
CB_CANCEL = "v1:x"
CB_CONFIRM_ACTIVATE = "v1:act:ok"
CB_CONFIRM_PAUSE = "v1:pause:ok"


def refresh_callback(screen: str) -> str:
    return f"v1:ref:{screen}"


MAIN_MENU: tuple[tuple[Button, ...], ...] = (
    (
        Button("💼 Minha conta", CB_ACCOUNT),
        Button("📈 Operações", CB_OPERATIONS),
    ),
    (
        Button("▶️ Ativar", CB_ACTIVATE),
        Button("⏸️ Pausar", CB_PAUSE),
    ),
    (
        Button("⚙️ Gestão de risco", CB_RISK),
        Button("🛡️ Proteções", CB_PROTECTIONS),
    ),
    (
        Button("📋 Histórico", CB_HISTORY),
        Button("🔄 Atualizar", refresh_callback("main")),
    ),
    (
        Button("📡 Status da conexão", CB_CONNECTION),
    ),
)

ACCOUNT_MENU: tuple[tuple[Button, ...], ...] = (
    (Button("🔄 Atualizar", refresh_callback("account")),),
    (Button("⬅️ Voltar ao menu", CB_MAIN),),
)

OPERATIONS_MENU: tuple[tuple[Button, ...], ...] = (
    (Button("🔄 Atualizar", refresh_callback("operations")),),
    (Button("⬅️ Voltar ao menu", CB_MAIN),),
)

RISK_MENU: tuple[tuple[Button, ...], ...] = (
    (Button("📦 Modo de lote", "v1:r:mode"),),
    (Button("✏️ Lote fixo", "v1:r:lot"),),
    (Button("📊 Risco percentual", "v1:r:risk"),),
    (Button("🎯 Meta diária", "v1:r:target"),),
    (Button("🛑 Limite de perda", "v1:r:loss"),),
    (Button("🔢 Máximo de operações", "v1:r:max"),),
    (Button("⬅️ Voltar ao menu", CB_MAIN),),
)

PROTECTIONS_MENU: tuple[tuple[Button, ...], ...] = (
    (Button("🛡️ Ativar/desativar Breakeven", "v1:p:be"),),
    (Button("📈 Ativar/desativar Trailing Stop", "v1:p:tr"),),
    (Button("🛑 Configurar limite diário", "v1:p:loss"),),
    (Button("⬅️ Voltar ao menu", CB_MAIN),),
)

HISTORY_MENU: tuple[tuple[Button, ...], ...] = (
    (Button("🔄 Atualizar", refresh_callback("history")),),
    (Button("⬅️ Voltar ao menu", CB_MAIN),),
)

CONNECTION_MENU: tuple[tuple[Button, ...], ...] = (
    (Button("🔄 Atualizar", refresh_callback("connection")),),
    (Button("⬅️ Voltar ao menu", CB_MAIN),),
)

CONFIRM_ACTIVATE: tuple[tuple[Button, ...], ...] = (
    (Button("✅ Confirmar ativação", CB_CONFIRM_ACTIVATE),),
    (Button("❌ Cancelar", CB_CANCEL),),
)

CONFIRM_PAUSE: tuple[tuple[Button, ...], ...] = (
    (Button("⏸️ Confirmar pausa", CB_CONFIRM_PAUSE),),
    (Button("❌ Cancelar", CB_CANCEL),),
)

PAUSED_MENU: tuple[tuple[Button, ...], ...] = (
    (Button("▶️ Reativar", CB_ACTIVATE),),
    (Button("⬅️ Voltar ao menu", CB_MAIN),),
)

ACTIVATED_MENU: tuple[tuple[Button, ...], ...] = (
    (Button("⬅️ Voltar ao menu", CB_MAIN),),
)

STATIC_CALLBACKS = {
    button.callback_data
    for keyboard in (
        MAIN_MENU,
        ACCOUNT_MENU,
        OPERATIONS_MENU,
        RISK_MENU,
        PROTECTIONS_MENU,
        HISTORY_MENU,
        CONNECTION_MENU,
        CONFIRM_ACTIVATE,
        CONFIRM_PAUSE,
        PAUSED_MENU,
        ACTIVATED_MENU,
    )
    for row in keyboard
    for button in row
}

LEGACY_CALLBACK_ALIASES = {
    "menu:main": CB_MAIN,
    "menu:account": CB_ACCOUNT,
    "menu:operations": CB_OPERATIONS,
    "menu:risk": CB_RISK,
    "menu:protections": CB_PROTECTIONS,
    "menu:history": CB_HISTORY,
    "menu:refresh": refresh_callback("main"),
    "confirm:activate": CB_ACTIVATE,
    "confirm:pause": CB_PAUSE,
    "action:activate:confirm": CB_CONFIRM_ACTIVATE,
    "action:pause:confirm": CB_CONFIRM_PAUSE,
    "action:cancel": CB_CANCEL,
}

FIXED_LOT_CALLBACK_RE = re.compile(r"^(?:v1:r:lot:|set:fixed_lot:)(?P<value>\d+(?:[\.,]\d{1,2})?)$")
REFRESH_CALLBACK_RE = re.compile(r"^v1:ref:(?P<screen>main|account|operations|risk|protections|history|connection)$")


def normalize_callback_data(callback_data: str) -> str:
    return LEGACY_CALLBACK_ALIASES.get(callback_data, callback_data)


def is_valid_callback_data(callback_data: str) -> bool:
    normalized = normalize_callback_data(callback_data)
    return (
        normalized in STATIC_CALLBACKS
        or bool(FIXED_LOT_CALLBACK_RE.fullmatch(normalized))
        or bool(REFRESH_CALLBACK_RE.fullmatch(normalized))
    )


def extract_fixed_lot(callback_data: str) -> str | None:
    match = FIXED_LOT_CALLBACK_RE.fullmatch(normalize_callback_data(callback_data))
    if not match:
        return None
    return match.group("value")


def extract_refresh_screen(callback_data: str) -> str | None:
    match = REFRESH_CALLBACK_RE.fullmatch(normalize_callback_data(callback_data))
    if not match:
        return None
    return match.group("screen")


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
