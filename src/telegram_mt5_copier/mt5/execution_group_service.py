from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from .execution_repository import ExecutionRepository
from .models import ExecutionGroup, ExecutionOrder, GROUP_STATUS_SIMULATED, LatencyMetrics, PendingOrderPlan


@dataclass(frozen=True)
class ExecutionGroupResult:
    group: ExecutionGroup | None
    orders: tuple[ExecutionOrder, ...]
    duplicate: bool = False
    rejected_reason: str | None = None


class ExecutionGroupService:
    def __init__(self, repository: ExecutionRepository) -> None:
        self.repository = repository
        self._closed = False

    def close(self) -> None:
        if hasattr(self.repository, "close"):
            self.repository.close()
        self._closed = True

    def __enter__(self) -> "ExecutionGroupService":
        return self

    def __exit__(self, _exc_type: object, _exc: object, _traceback: object) -> None:
        self.close()

    def create_simulated_group(
        self,
        plan: PendingOrderPlan,
        *,
        metrics: LatencyMetrics | None = None,
    ) -> ExecutionGroupResult:
        if self.repository.has_execution_group(plan.signal_id, plan.user_id, plan.mt5_account_id):
            return ExecutionGroupResult(group=None, orders=(), duplicate=True)
        group, orders = self.repository.create_group_with_orders(
            plan,
            status=GROUP_STATUS_SIMULATED,
            order_status="simulated",
            metrics=metrics,
        )
        return ExecutionGroupResult(group=group, orders=orders)

    def reject_group(self, plan: PendingOrderPlan, reason: str) -> ExecutionGroupResult:
        if self.repository.has_execution_group(plan.signal_id, plan.user_id, plan.mt5_account_id):
            return ExecutionGroupResult(group=None, orders=(), duplicate=True)
        group = self.repository.mark_group_rejected(plan, error_code=reason, error_message=reason)
        return ExecutionGroupResult(group=group, orders=(), rejected_reason=reason)


def build_latency_metrics(
    *,
    telegram_message_at: datetime | None = None,
    listener_received_at: datetime | None = None,
    signal_parsed_at: datetime | None = None,
    execution_planned_at: datetime | None = None,
    validation_started_at: datetime | None = None,
    validation_finished_at: datetime | None = None,
    mt5_order_sent_at: datetime | None = None,
    broker_response_at: datetime | None = None,
) -> LatencyMetrics:
    total_start = telegram_message_at or listener_received_at
    total_end = broker_response_at or validation_finished_at
    return LatencyMetrics(
        telegram_message_at=iso_or_none(telegram_message_at),
        listener_received_at=iso_or_none(listener_received_at),
        signal_parsed_at=iso_or_none(signal_parsed_at),
        execution_planned_at=iso_or_none(execution_planned_at),
        validation_started_at=iso_or_none(validation_started_at),
        validation_finished_at=iso_or_none(validation_finished_at),
        mt5_order_sent_at=iso_or_none(mt5_order_sent_at),
        broker_response_at=iso_or_none(broker_response_at),
        telegram_to_listener_ms=duration_ms(telegram_message_at, listener_received_at),
        parsing_ms=duration_ms(listener_received_at, signal_parsed_at),
        planning_ms=duration_ms(signal_parsed_at, execution_planned_at),
        validation_ms=duration_ms(validation_started_at, validation_finished_at),
        mt5_submission_ms=duration_ms(validation_finished_at, mt5_order_sent_at),
        broker_response_ms=duration_ms(mt5_order_sent_at, broker_response_at),
        total_ms=duration_ms(total_start, total_end),
    )


def iso_or_none(value: datetime | None) -> str | None:
    return value.astimezone(timezone.utc).isoformat() if value else None


def duration_ms(start: datetime | None, end: datetime | None) -> int | None:
    if start is None or end is None:
        return None
    return max(0, int((end - start).total_seconds() * 1000))
