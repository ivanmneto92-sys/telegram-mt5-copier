from __future__ import annotations

from .client import SimulatedMT5Client


class SymbolResolver:
    def __init__(self, client: object | None = None) -> None:
        self.client = client or SimulatedMT5Client()

    def resolve(self, base_symbol: str = "XAUUSD") -> str:
        aliases = (base_symbol, "GOLD") if base_symbol.upper() == "XAUUSD" else (base_symbol,)
        suffixes = ("", ".", "m", "b", ".r", "_i")
        candidates = tuple(
            f"{alias}{suffix}"
            for alias in aliases
            for suffix in suffixes
        )
        disabled_fallback: str | None = None
        for candidate in candidates:
            symbol_info = self.client.symbol_info(candidate)
            if symbol_info is not None:
                if getattr(symbol_info, "trade_allowed", True):
                    return symbol_info.name
                disabled_fallback = disabled_fallback or symbol_info.name

        if hasattr(self.client, "symbol_names"):
            discovered = sorted(
                {
                    name
                    for alias in aliases
                    for name in self.client.symbol_names(f"*{alias}*")
                    if name.upper().startswith(alias.upper())
                },
                key=lambda name: (len(name), name.upper()),
            )
            for candidate in discovered:
                symbol_info = self.client.symbol_info(candidate)
                if symbol_info is not None:
                    if getattr(symbol_info, "trade_allowed", True):
                        return symbol_info.name
                    disabled_fallback = disabled_fallback or symbol_info.name

        if disabled_fallback is not None:
            return disabled_fallback

        raise ValueError(f"Simbolo {base_symbol} indisponivel no terminal.")
