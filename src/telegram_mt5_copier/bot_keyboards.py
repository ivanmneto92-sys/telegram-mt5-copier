from __future__ import annotations

from dataclasses import dataclass
import re


@dataclass(frozen=True)
class Button:
    text: str
    callback_data: str | None = None
    web_app_url: str | None = None


CB_MAIN = "v1:m"
CB_ACCOUNT = "v1:a"
CB_OPERATIONS = "v1:o"
CB_ACTIVATE = "v1:act"
CB_PAUSE = "v1:pause"
CB_RISK = "v1:r"
CB_PROTECTIONS = "v1:p"
CB_HISTORY = "v1:h"
CB_CONNECTION = "v1:c"
CB_CONNECT_MT5 = "v1:mt5:connect"
CB_MT5_ACCOUNTS = "v1:mt5:accounts"
CB_MT5_SECURE = "v1:mt5:secure"
CB_MT5_VIEW = "v1:mt5:view"
CB_MT5_CONFIG = "v1:mt5:config"
CB_MT5_TEST = "v1:mt5:test"
CB_MT5_REMOVE = "v1:mt5:remove"
CB_MT5_CONFIRM_REMOVE = "v1:mt5:remove:ok"
CB_SIGNAL_EXECUTION = "v1:ex"
CB_EXEC_ENTRY_MENU = "v1:ex:entry"
CB_EXEC_ENTRY_PENDING = "v1:ex:entry:pending"
CB_EXEC_ENTRY_MARKET_ZONE = "v1:ex:entry:zone"
CB_EXEC_PRICE_MENU = "v1:ex:price"
CB_EXEC_PRICE_FIRST_TOUCH = "v1:ex:price:first"
CB_EXEC_PRICE_MIDDLE = "v1:ex:price:middle"
CB_EXEC_PRICE_DISTRIBUTED = "v1:ex:price:distributed"
CB_EXEC_EXPIRATION_MENU = "v1:ex:exp"
CB_EXEC_EXPIRATION_30 = "v1:ex:exp:30"
CB_EXEC_EXPIRATION_60 = "v1:ex:exp:60"
CB_EXEC_EXPIRATION_120 = "v1:ex:exp:120"
CB_EXEC_EXPIRATION_240 = "v1:ex:exp:240"
CB_EXEC_EXPIRATION_DAY = "v1:ex:exp:day"
CB_EXEC_SPLIT_TPS = "v1:ex:split"
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
        Button("🔗 Conectar conta MT5", CB_CONNECT_MT5),
        Button("🖥️ Minhas contas", CB_MT5_ACCOUNTS),
    ),
    (
        Button("⚙️ Execução dos sinais", CB_SIGNAL_EXECUTION),
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


def mt5_connect_menu(web_app_url: str | None = None) -> tuple[tuple[Button, ...], ...]:
    open_button = (
        Button("🔐 Abrir conexão segura", web_app_url=web_app_url)
        if web_app_url
        else Button("🔐 Abrir conexão segura", CB_MT5_SECURE)
    )
    return (
        (open_button,),
        (Button("⬅️ Voltar ao menu", CB_MAIN),),
    )


MT5_ACCOUNTS_MENU: tuple[tuple[Button, ...], ...] = (
    (Button("📊 Ver conta", CB_MT5_VIEW), Button("⚙️ Configurar", CB_MT5_CONFIG)),
    (Button("⚙️ Execução dos sinais", CB_SIGNAL_EXECUTION),),
    (Button("🔄 Testar conexão", CB_MT5_TEST),),
    (Button("🗑️ Remover conta", CB_MT5_REMOVE),),
    (Button("⬅️ Voltar ao menu", CB_MAIN),),
)


CONFIRM_REMOVE_MT5: tuple[tuple[Button, ...], ...] = (
    (Button("🗑️ Confirmar remoção", CB_MT5_CONFIRM_REMOVE),),
    (Button("❌ Cancelar", CB_CANCEL),),
)


SIGNAL_EXECUTION_MENU: tuple[tuple[Button, ...], ...] = (
    (Button("📍 Modo de entrada", CB_EXEC_ENTRY_MENU),),
    (Button("🎯 Preço da faixa", CB_EXEC_PRICE_MENU),),
    (Button("⏳ Validade", CB_EXEC_EXPIRATION_MENU),),
    (Button("🔢 Dividir entre TPs", CB_EXEC_SPLIT_TPS),),
    (Button("⬅️ Voltar", CB_MT5_ACCOUNTS),),
)


ENTRY_MODE_MENU: tuple[tuple[Button, ...], ...] = (
    (Button("Ordem pendente", CB_EXEC_ENTRY_PENDING),),
    (Button("Mercado ao entrar na zona", CB_EXEC_ENTRY_MARKET_ZONE),),
    (Button("⬅️ Voltar", CB_SIGNAL_EXECUTION),),
)


ENTRY_PRICE_MENU: tuple[tuple[Button, ...], ...] = (
    (Button("Primeiro toque", CB_EXEC_PRICE_FIRST_TOUCH),),
    (Button("Meio da faixa", CB_EXEC_PRICE_MIDDLE),),
    (Button("Distribuir na faixa", CB_EXEC_PRICE_DISTRIBUTED),),
    (Button("⬅️ Voltar", CB_SIGNAL_EXECUTION),),
)


EXPIRATION_MENU: tuple[tuple[Button, ...], ...] = (
    (Button("30 minutos", CB_EXEC_EXPIRATION_30), Button("1 hora", CB_EXEC_EXPIRATION_60)),
    (Button("2 horas", CB_EXEC_EXPIRATION_120), Button("4 horas", CB_EXEC_EXPIRATION_240)),
    (Button("Até o fim do dia", CB_EXEC_EXPIRATION_DAY),),
    (Button("⬅️ Voltar", CB_SIGNAL_EXECUTION),),
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
        MT5_ACCOUNTS_MENU,
        CONFIRM_REMOVE_MT5,
        SIGNAL_EXECUTION_MENU,
        ENTRY_MODE_MENU,
        ENTRY_PRICE_MENU,
        EXPIRATION_MENU,
    )
    for row in keyboard
    for button in row
    if button.callback_data is not None
}
STATIC_CALLBACKS.update({CB_CONNECT_MT5, CB_MT5_SECURE, CB_MT5_ACCOUNTS, CB_SIGNAL_EXECUTION})

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
REFRESH_CALLBACK_RE = re.compile(
    r"^v1:ref:(?P<screen>main|account|operations|risk|protections|history|connection|mt5_accounts)$"
)


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
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
    except ImportError as exc:
        raise RuntimeError("python-telegram-bot nao instalado.") from exc

    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(button.text, web_app=WebAppInfo(button.web_app_url))
                if button.web_app_url
                else InlineKeyboardButton(button.text, callback_data=button.callback_data)
                for button in row
            ]
            for row in rows
        ]
    )
