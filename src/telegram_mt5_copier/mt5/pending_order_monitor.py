from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from ..database import connect_database, initialize_database


class PendingOrderMonitor:
    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path
        self._closed = False
        initialize_database(database_path)

    def close(self) -> None:
        self._closed = True

    def __enter__(self) -> "PendingOrderMonitor":
        return self

    def __exit__(self, _exc_type: object, _exc: object, _traceback: object) -> None:
        self.close()

    def expire_orders(self, now: datetime | None = None) -> int:
        now_iso = (now or datetime.now(tz=timezone.utc)).isoformat()
        with connect_database(self.database_path) as connection:
            cursor = connection.execute(
                """
                UPDATE execution_groups
                SET status = 'expired', updated_at = ?
                WHERE expiration_at <= ?
                  AND status IN ('planned', 'simulated', 'pending_submission', 'pending_active')
                """,
                (now_iso, now_iso),
            )
            try:
                changed = int(cursor.rowcount)
            finally:
                cursor.close()
            cursor = connection.execute(
                """
                UPDATE execution_orders
                SET status = 'expired', updated_at = ?
                WHERE execution_group_id IN (
                    SELECT id FROM execution_groups WHERE status = 'expired'
                )
                  AND status IN ('planned', 'simulated', 'pending_submission', 'pending_active')
                """,
                (now_iso,),
            )
            cursor.close()
        return changed

    def cancel_if_price_invalidates(self, *, group_id: int, current_price: Decimal) -> bool:
        with connect_database(self.database_path) as connection:
            cursor = connection.execute(
                """
                SELECT direction, stop_loss
                FROM execution_groups
                WHERE id = ?
                """,
                (group_id,),
            )
            try:
                group = cursor.fetchone()
            finally:
                cursor.close()
            if group is None:
                return False
            cursor = connection.execute(
                """
                SELECT take_profit
                FROM execution_orders
                WHERE execution_group_id = ?
                ORDER BY tp_index ASC
                LIMIT 1
                """,
                (group_id,),
            )
            try:
                first_tp = cursor.fetchone()
            finally:
                cursor.close()
            if first_tp is None:
                return False

            direction = str(group[0])
            stop_loss = Decimal(str(group[1]))
            take_profit = Decimal(str(first_tp[0]))
            invalidated = (
                (direction == "BUY" and (current_price <= stop_loss or current_price >= take_profit))
                or (direction == "SELL" and (current_price >= stop_loss or current_price <= take_profit))
            )
            if not invalidated:
                return False

            now_iso = datetime.now(tz=timezone.utc).isoformat()
            cursor = connection.execute(
                "UPDATE execution_groups SET status = 'cancelled', updated_at = ? WHERE id = ?",
                (now_iso, group_id),
            )
            cursor.close()
            cursor = connection.execute(
                "UPDATE execution_orders SET status = 'cancelled', updated_at = ? WHERE execution_group_id = ?",
                (now_iso, group_id),
            )
            cursor.close()
            return True
