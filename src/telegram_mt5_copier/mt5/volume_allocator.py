from __future__ import annotations

from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR

from .models import SymbolInfo


class VolumeAllocationError(ValueError):
    pass


def allocate_volume(total_volume: Decimal, parts: int, symbol_info: SymbolInfo) -> tuple[Decimal, ...]:
    if parts < 1:
        raise VolumeAllocationError("Quantidade de TPs invalida.")
    if total_volume <= 0:
        raise VolumeAllocationError("Lote total invalido.")
    validate_step(total_volume, symbol_info.volume_step)

    total_units = units_for_volume(total_volume, symbol_info.volume_step)
    min_units = units_for_volume(symbol_info.volume_min, symbol_info.volume_step, round_up=True)
    max_units = units_for_volume(symbol_info.volume_max, symbol_info.volume_step)

    if total_units < min_units * parts:
        raise VolumeAllocationError("Lote total insuficiente para dividir entre todos os TPs.")

    base_units = total_units // parts
    remainder = total_units % parts
    if base_units < min_units:
        raise VolumeAllocationError("Divisao geraria ordem abaixo do lote minimo.")

    allocations: list[Decimal] = []
    for index in range(parts):
        order_units = base_units + (1 if index < remainder else 0)
        if order_units < min_units or order_units > max_units:
            raise VolumeAllocationError("Lote por ordem fora dos limites do simbolo.")
        allocations.append(order_units * symbol_info.volume_step)

    if sum(allocations, Decimal("0")) != total_volume:
        raise VolumeAllocationError("A divisao de lotes perderia volume.")

    return tuple(allocations)


def validate_volume(volume: Decimal, symbol_info: SymbolInfo) -> None:
    if volume < symbol_info.volume_min or volume > symbol_info.volume_max:
        raise VolumeAllocationError("Lote fora dos limites do simbolo.")
    validate_step(volume, symbol_info.volume_step)


def volume_for_risk(
    *,
    equity: Decimal | None,
    risk_percent: Decimal,
    entry_price: Decimal,
    stop_loss: Decimal,
    symbol_info: SymbolInfo,
) -> Decimal:
    if equity is None or equity <= 0:
        raise VolumeAllocationError("Equity indisponivel para calcular risco percentual.")
    if risk_percent <= 0 or risk_percent > Decimal("100"):
        raise VolumeAllocationError("Percentual de risco invalido.")
    tick_value = (
        symbol_info.trade_tick_value_loss
        if symbol_info.trade_tick_value_loss > 0
        else symbol_info.trade_tick_value
    )
    if symbol_info.trade_tick_size <= 0 or tick_value <= 0:
        raise VolumeAllocationError("Tick size/tick value indisponivel para calcular o lote.")

    price_risk = abs(entry_price - stop_loss)
    if price_risk <= 0:
        raise VolumeAllocationError("Distancia ate o Stop Loss invalida.")
    loss_per_lot = (price_risk / symbol_info.trade_tick_size) * tick_value
    allowed_loss = equity * risk_percent / Decimal("100")
    raw_volume = allowed_loss / loss_per_lot
    units = (raw_volume / symbol_info.volume_step).to_integral_value(rounding=ROUND_FLOOR)
    volume = units * symbol_info.volume_step
    if volume > symbol_info.volume_max:
        max_units = (symbol_info.volume_max / symbol_info.volume_step).to_integral_value(rounding=ROUND_FLOOR)
        volume = max_units * symbol_info.volume_step
    if volume < symbol_info.volume_min:
        raise VolumeAllocationError("Risco calculado gera lote abaixo do minimo do simbolo.")
    validate_volume(volume, symbol_info)
    return volume


def validate_step(volume: Decimal, step: Decimal) -> None:
    units = volume / step
    if units != units.to_integral_value():
        raise VolumeAllocationError("Lote nao respeita o volume_step do simbolo.")


def units_for_volume(volume: Decimal, step: Decimal, *, round_up: bool = False) -> int:
    units = volume / step
    if round_up:
        units = units.to_integral_value(rounding=ROUND_CEILING)
    if units != units.to_integral_value():
        raise VolumeAllocationError("Volume nao e multiplo do volume_step.")
    return int(units)
