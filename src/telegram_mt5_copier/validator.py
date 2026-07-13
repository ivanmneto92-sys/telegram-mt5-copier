from __future__ import annotations

from .models import DecisionStatus, Direction, ProcessingDecision, TradeSignal


def validate_signal(signal: TradeSignal) -> ProcessingDecision:
    if signal.entry_low > signal.entry_high:
        return ProcessingDecision(DecisionStatus.REJECTED, "invalid_entry_range", signal=signal)

    if signal.direction == Direction.BUY:
        if signal.stop_loss >= signal.entry_low:
            return ProcessingDecision(DecisionStatus.REJECTED, "buy_stop_loss_not_below_entry", signal=signal)
        invalid_targets = [tp for tp in signal.take_profits if tp <= signal.entry_high]
        if invalid_targets:
            return ProcessingDecision(DecisionStatus.REJECTED, "buy_take_profit_not_above_entry", signal=signal)
        return ProcessingDecision(DecisionStatus.ACCEPTED, "valid", signal=signal)

    if signal.stop_loss <= signal.entry_high:
        return ProcessingDecision(DecisionStatus.REJECTED, "sell_stop_loss_not_above_entry", signal=signal)
    invalid_targets = [tp for tp in signal.take_profits if tp >= signal.entry_low]
    if invalid_targets:
        return ProcessingDecision(DecisionStatus.REJECTED, "sell_take_profit_not_below_entry", signal=signal)

    return ProcessingDecision(DecisionStatus.ACCEPTED, "valid", signal=signal)
