from __future__ import annotations

from decimal import Decimal
from datetime import datetime
from pathlib import Path
from typing import Any

from .models import ACCOUNT_MODE_HEDGING, ACCOUNT_MODE_NETTING, AccountConnectionInfo, SymbolInfo, TerminalInfo, TickInfo

try:  # pragma: no cover - covered by import tests on systems without MetaTrader5.
    import MetaTrader5 as _mt5
except ImportError:  # pragma: no cover
    _mt5 = None


class MT5Client:
    DEFAULT_TIMEOUT_MS = 60000

    def __init__(self, mt5_module: Any | None = None) -> None:
        self._mt5 = mt5_module if mt5_module is not None else _mt5
        self._initialized = False
        self._last_error: object | None = None

    def initialize(
        self,
        terminal_path: Path,
        login: int,
        password: str,
        server: str,
        *,
        timeout_ms: int = DEFAULT_TIMEOUT_MS,
    ) -> bool:
        if self._mt5 is None:
            raise RuntimeError("Pacote MetaTrader5 indisponivel neste ambiente.")
        self._last_error = None
        terminal_initialized = bool(
            self._mt5.initialize(
                path=str(terminal_path),
                portable=True,
                timeout=timeout_ms,
            )
        )
        self._initialized = terminal_initialized
        if not terminal_initialized:
            self._last_error = self.last_error()
            return False

        logged_in = bool(
            self._mt5.login(
                int(login),
                password=password,
                server=server,
                timeout=timeout_ms,
            )
        )
        if not logged_in:
            self._last_error = self.last_error()
            return False
        return True

    def last_error(self) -> object | None:
        if self._mt5 is None:
            return self._last_error
        try:
            return self._mt5.last_error()
        except AttributeError:
            return self._last_error

    def shutdown(self) -> None:
        if self._mt5 is not None and self._initialized:
            self._mt5.shutdown()
        self._initialized = False

    def account_info(self) -> AccountConnectionInfo | None:
        info = self._mt5.account_info() if self._mt5 is not None else None
        if info is None:
            return None
        trade_mode = getattr(info, "trade_mode", 0)
        account_type = "real" if trade_mode == 2 else "demo"
        margin_mode = getattr(info, "margin_mode", 0)
        account_mode = ACCOUNT_MODE_NETTING if margin_mode == 0 else ACCOUNT_MODE_HEDGING
        return AccountConnectionInfo(
            login=int(getattr(info, "login")),
            trade_allowed=bool(getattr(info, "trade_allowed", False)),
            server=str(getattr(info, "server", "")),
            account_type=account_type,
            account_mode=account_mode,
            balance=Decimal(str(getattr(info, "balance"))) if hasattr(info, "balance") else None,
            equity=Decimal(str(getattr(info, "equity"))) if hasattr(info, "equity") else None,
        )

    def terminal_info(self) -> TerminalInfo | None:
        info = self._mt5.terminal_info() if self._mt5 is not None else None
        if info is None:
            return None
        return TerminalInfo(
            path=Path(str(getattr(info, "path", ""))) if getattr(info, "path", None) else None,
            connected=bool(getattr(info, "connected", False)),
            trade_allowed=bool(getattr(info, "trade_allowed", False)),
            tradeapi_disabled=bool(getattr(info, "tradeapi_disabled", False)),
        )

    def symbol_info(self, symbol: str) -> SymbolInfo | None:
        info = self._mt5.symbol_info(symbol) if self._mt5 is not None else None
        if info is None:
            return None
        return SymbolInfo(
            name=str(getattr(info, "name", symbol)),
            volume_min=Decimal(str(getattr(info, "volume_min", "0.01"))),
            volume_max=Decimal(str(getattr(info, "volume_max", "100"))),
            volume_step=Decimal(str(getattr(info, "volume_step", "0.01"))),
            trade_tick_size=Decimal(str(getattr(info, "trade_tick_size", "0.01"))),
            trade_tick_value=Decimal(str(getattr(info, "trade_tick_value", "0"))),
            trade_tick_value_loss=Decimal(str(getattr(info, "trade_tick_value_loss", "0"))),
            point=Decimal(str(getattr(info, "point", "0.01"))),
            digits=int(getattr(info, "digits", 2)),
            stops_level_points=int(getattr(info, "trade_stops_level", 0)),
            filling_mode=int(getattr(info, "filling_mode")) if hasattr(info, "filling_mode") else None,
            trade_allowed=bool(getattr(info, "trade_mode", 0) != 0),
        )

    def symbol_info_tick(self, symbol: str) -> TickInfo | None:
        tick = self._mt5.symbol_info_tick(symbol) if self._mt5 is not None else None
        if tick is None:
            return None
        return TickInfo(
            bid=Decimal(str(getattr(tick, "bid"))),
            ask=Decimal(str(getattr(tick, "ask"))),
        )

    def positions_get(self) -> tuple[object, ...]:
        positions = self._mt5.positions_get() if self._mt5 is not None else None
        return tuple(positions or ())

    def orders_get(self) -> tuple[object, ...]:
        orders = self._mt5.orders_get() if self._mt5 is not None else None
        return tuple(orders or ())

    def history_deals_get(self, date_from: datetime, date_to: datetime) -> tuple[object, ...]:
        deals = self._mt5.history_deals_get(date_from, date_to) if self._mt5 is not None else None
        return tuple(deals or ())

    def symbol_select(self, symbol: str, enable: bool = True) -> bool:
        if self._mt5 is None:
            return False
        return bool(self._mt5.symbol_select(symbol, enable))

    def order_check(self, request: dict[str, object]) -> object | None:
        if self._mt5 is None:
            return None
        return self._mt5.order_check(request)

    def order_send(self, request: dict[str, object]) -> object | None:
        if self._mt5 is None:
            return None
        return self._mt5.order_send(request)

    def constant(self, name: str, default: int) -> int:
        return int(getattr(self._mt5, name, default)) if self._mt5 is not None else default

    def __enter__(self) -> "MT5Client":
        return self

    def __exit__(self, _exc_type: object, _exc: object, _traceback: object) -> None:
        self.shutdown()


class SimulatedMT5Client:
    def __init__(
        self,
        *,
        account_type: str = "demo",
        account_mode: str = ACCOUNT_MODE_HEDGING,
        trade_allowed: bool = True,
        symbol_info: SymbolInfo | None = None,
        tick: TickInfo | None = None,
        order_check_results: list[object] | None = None,
        order_send_results: list[object] | None = None,
        positions: tuple[object, ...] = (),
        orders: tuple[object, ...] = (),
        history_deals: tuple[object, ...] = (),
    ) -> None:
        self.account_type = account_type
        self.account_mode = account_mode
        self.trade_allowed = trade_allowed
        self._symbol_info = symbol_info or SymbolInfo(name="XAUUSD")
        self._tick = tick or TickInfo(bid=Decimal("4059"), ask=Decimal("4061"))
        self.initialized = False
        self.shutdown_count = 0
        self.order_send_called = False
        self.order_check_requests: list[dict[str, object]] = []
        self.order_send_requests: list[dict[str, object]] = []
        self.order_check_results = list(order_check_results or [])
        self.order_send_results = list(order_send_results or [])
        self._positions = positions
        self._orders = orders
        self._history_deals = history_deals
        self._login: int | None = None
        self._server = ""
        self._last_error: object | None = None

    def initialize(
        self,
        terminal_path: Path,
        login: int,
        password: str,
        server: str,
        *,
        timeout_ms: int = MT5Client.DEFAULT_TIMEOUT_MS,
    ) -> bool:
        _ = terminal_path
        _ = password
        _ = timeout_ms
        self.initialized = True
        self._login = int(login)
        self._server = server
        return True

    def last_error(self) -> object | None:
        return self._last_error

    def shutdown(self) -> None:
        self.initialized = False
        self.shutdown_count += 1

    def account_info(self) -> AccountConnectionInfo | None:
        if self._login is None:
            return None
        return AccountConnectionInfo(
            login=self._login,
            trade_allowed=self.trade_allowed,
            server=self._server,
            account_type=self.account_type,
            account_mode=self.account_mode,
            balance=Decimal("10000"),
            equity=Decimal("10000"),
        )

    def terminal_info(self) -> TerminalInfo:
        return TerminalInfo(path=None, connected=self.initialized)

    def symbol_info(self, symbol: str) -> SymbolInfo | None:
        if symbol.upper().startswith("XAUUSD"):
            return self._symbol_info
        return None

    def symbol_info_tick(self, symbol: str) -> TickInfo:
        _ = symbol
        return self._tick

    def positions_get(self) -> tuple[object, ...]:
        return self._positions

    def orders_get(self) -> tuple[object, ...]:
        return self._orders

    def history_deals_get(self, date_from: datetime, date_to: datetime) -> tuple[object, ...]:
        _ = date_from
        _ = date_to
        return self._history_deals

    def symbol_select(self, symbol: str, enable: bool = True) -> bool:
        _ = enable
        return self.symbol_info(symbol) is not None

    def order_check(self, request: dict[str, object]) -> dict[str, object]:
        self.order_check_requests.append(dict(request))
        if self.order_check_results:
            return self.order_check_results.pop(0)
        return {"retcode": 0, "request": request}

    def order_send(self, request: dict[str, object]) -> dict[str, object]:
        self.order_send_called = True
        self.order_send_requests.append(dict(request))
        if self.order_send_results:
            return self.order_send_results.pop(0)
        return {"retcode": 10009, "order": 100000 + len(self.order_send_requests), "comment": "simulated"}

    def constant(self, name: str, default: int) -> int:
        constants = {
            "TRADE_ACTION_PENDING": 5,
            "ORDER_TYPE_BUY_LIMIT": 2,
            "ORDER_TYPE_SELL_LIMIT": 3,
            "ORDER_TYPE_BUY_STOP": 4,
            "ORDER_TYPE_SELL_STOP": 5,
            "ORDER_TIME_SPECIFIED": 2,
            "ORDER_FILLING_RETURN": 2,
            "TRADE_RETCODE_PLACED": 10008,
            "TRADE_RETCODE_DONE": 10009,
        }
        return constants.get(name, default)
