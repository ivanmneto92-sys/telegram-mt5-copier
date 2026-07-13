from __future__ import annotations

from .client import SimulatedMT5Client


class SymbolResolver:
    def __init__(self, client: object | None = None) -> None:
        self.client = client or SimulatedMT5Client()

    def resolve(self, base_symbol: str = "XAUUSD") -> str:
        candidates = (
            base_symbol,
            f"{base_symbol}.",
            f"{base_symbol}m",
            f"{base_symbol}.r",
            f"{base_symbol}_i",
        )
        for candidate in candidates:
            symbol_info = self.client.symbol_info(candidate)
            if symbol_info is not None:
                return symbol_info.name
        raise ValueError(f"Simbolo {base_symbol} indisponivel no terminal.")
