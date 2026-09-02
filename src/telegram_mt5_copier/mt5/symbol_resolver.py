from __future__ import annotations

from .client import SimulatedMT5Client


class SymbolResolver:
    def __init__(self, client: object | None = None) -> None:
        self.client = client or SimulatedMT5Client()

    def resolve(self, base_symbol: str = "XAUUSD") -> str:
        # "GOLD" catches most brokers whose gold symbol has no "XAUUSD" in it
        # at all, via the discovery fallback below matching anything that
        # starts with it. FXGlobe's exact symbol name, "Gold_Spot", is
        # listed directly instead of relying only on that fallback, since
        # a symbol lookup for a specific real broker is worth guaranteeing
        # rather than leaving to a wildcard search over the terminal's
        # current symbol list.
        aliases = (
            (base_symbol, "GOLD", "Gold_Spot")
            if base_symbol.upper() == "XAUUSD"
            else (base_symbol,)
        )
        # "-STDc" is VT Markets' suffix for its standard account group
        # (XAUUSD-STDc, EURUSD-STDc, ...) -- listed directly rather than
        # relying only on the wildcard discovery fallback below, for the
        # same reason as Gold_Spot: a fresh terminal's symbol list isn't
        # guaranteed to be fully synced with the server yet right after
        # provisioning.
        suffixes = ("", ".", "m", "b", ".r", "_i", "-STDc")
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
