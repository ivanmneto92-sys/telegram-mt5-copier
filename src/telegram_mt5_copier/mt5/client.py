from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

from .models import ACCOUNT_MODE_HEDGING, ACCOUNT_MODE_NETTING, AccountConnectionInfo, SymbolInfo, TerminalInfo, TickInfo

try:  # pragma: no cover - covered by import tests on systems without MetaTrader5.
    import MetaTrader5 as _mt5
except ImportError:  # pragma: no cover
    _mt5 = None


class MT5Client:
    def __init__(self, mt5_module: Any | None = None) -> None:
        self._mt5 = mt5_module if mt5_module is not None else _mt5
        self._initialized = False

    def initialize(self, terminal_path: Path, login: int, password: str, server: str) -> bool:
        if self._mt5 is None:
            raise RuntimeError("Pacote MetaTrader5 indisponivel neste ambiente.")
        initialized = bool(
            self._mt5.initialize(
                path=str(terminal_path),
                login=int(login),
                password=password,
                server=server,
            )
        )
        self._initialized = initialized
        return initialized

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
        )

    def terminal_info(self) -> TerminalInfo | None:
        info = self._mt5.terminal_info() if self._mt5 is not None else None
        if info is None:
            return None
        return TerminalInfo(
            path=Path(str(getattr(info, "path", ""))) if getattr(info, "path", None) else None,
            connected=bool(getattr(info, "connected", False)),
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
            digits=int(getattr(info, "digits", 2)),
            stops_level_points=int(getattr(info, "trade_stops_level", 0)),
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

    def order_check(self, request: dict[str, object]) -> object | None:
        if self._mt5 is None:
            return None
        return self._mt5.order_check(request)

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
    ) -> None:
        self.account_type = account_type
        self.account_mode = account_mode
        self.trade_allowed = trade_allowed
        self._symbol_info = symbol_info or SymbolInfo(name="XAUUSD")
        self._tick = tick or TickInfo(bid=Decimal("4059"), ask=Decimal("4061"))
        self.initialized = False
        self.shutdown_count = 0
        self.order_send_called = False
        self._login: int | None = None
        self._server = ""

    def initialize(self, terminal_path: Path, login: int, password: str, server: str) -> bool:
        _ = terminal_path
        _ = password
        self.initialized = True
        self._login = int(login)
        self._server = server
        return True

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
        return ()

    def orders_get(self) -> tuple[object, ...]:
        return ()

    def order_check(self, request: dict[str, object]) -> dict[str, object]:
        return {"retcode": 0, "request": request}
