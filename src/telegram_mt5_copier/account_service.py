from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AccountSummary:
    connection_status: str
    balance: str | None
    equity: str | None
    daily_result: str | None


class AccountService:
    def get_connection_status(self, user_id: int) -> str:
        return "not_connected"

    def get_account_summary(self, user_id: int) -> AccountSummary:
        return AccountSummary(
            connection_status=self.get_connection_status(user_id),
            balance=None,
            equity=None,
            daily_result=None,
        )

    def get_open_positions(self, user_id: int) -> tuple[object, ...]:
        return ()

    def get_daily_result(self, user_id: int) -> str:
        return "not_connected"
