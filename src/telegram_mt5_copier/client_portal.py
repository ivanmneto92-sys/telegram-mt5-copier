from __future__ import annotations

from pathlib import Path

from .database import connect_database, initialize_database


class ClientPortalService:
    """Consultas somente-leitura do portal, sempre limitadas ao user_id autenticado."""

    def __init__(self, database_path: Path, *, brand_name: str) -> None:
        self.database_path = database_path
        self.brand_name = brand_name
        initialize_database(database_path)

    def dashboard(self, user_id: int) -> dict[str, object]:
        with connect_database(self.database_path) as db:
            user = db.execute(
                "SELECT telegram_username, status, daily_signal_pause_until FROM users WHERE id = ?",
                (user_id,),
            ).fetchone()
            if user is None:
                raise ValueError("Cliente nao encontrado.")
            account = db.execute(
                """
                SELECT id, account_alias, broker_name, server_name, login, account_type,
                       connection_status, balance, equity, worker_heartbeat_at, last_error
                FROM mt5_accounts WHERE user_id = ?
                ORDER BY CASE connection_status WHEN 'connected' THEN 0 ELSE 1 END, id DESC LIMIT 1
                """,
                (user_id,),
            ).fetchone()
            performance = None
            if account is not None:
                performance = db.execute(
                    """
                    SELECT performance_date, realized_profit, gross_profit, trading_costs,
                           starting_balance, return_percent, updated_at
                    FROM account_daily_performance WHERE mt5_account_id = ?
                    ORDER BY performance_date DESC LIMIT 1
                    """,
                    (int(account[0]),),
                ).fetchone()
            active_count = db.execute(
                """
                SELECT COUNT(*) FROM execution_groups
                WHERE user_id = ? AND status IN ('pending_active', 'filled', 'open')
                """,
                (user_id,),
            ).fetchone()[0]
        return {
            "brand": self.brand_name,
            "user": {
                "id": user_id,
                "username": user[0],
                "status": user[1],
                "daily_signal_pause_until": user[2],
            },
            "account": self._account(account),
            "daily_performance": self._performance(performance),
            "active_operations": int(active_count),
        }

    def channels(self, user_id: int) -> dict[str, object]:
        with connect_database(self.database_path) as db:
            setting = db.execute(
                "SELECT selection_mode FROM user_channel_settings WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            mode = str(setting[0]) if setting else "custom"
            rows = db.execute(
                """
                SELECT c.id, c.title, c.status, COALESCE(s.enabled, 0) AS enabled
                FROM source_channels c
                LEFT JOIN user_channel_subscriptions s
                  ON s.source_channel_id = c.id AND s.user_id = ?
                WHERE c.status = 'active'
                ORDER BY c.id
                """,
                (user_id,),
            ).fetchall()
        return {
            "selection_mode": mode,
            "channels": [
                {"id": int(row[0]), "name": str(row[1]), "status": str(row[2]), "enabled": bool(row[3])}
                for row in rows
            ],
        }

    def operations(self, user_id: int, *, limit: int = 100) -> dict[str, object]:
        safe_limit = max(1, min(limit, 200))
        with connect_database(self.database_path) as db:
            rows = db.execute(
                """
                SELECT g.id, g.status, g.symbol, g.direction, g.order_type,
                       g.selected_entry_price, g.stop_loss, g.total_volume,
                       g.created_at, g.error_code, c.title,
                       COUNT(o.id), COALESCE(SUM(CAST(o.net_profit AS REAL)), 0)
                FROM execution_groups g
                LEFT JOIN signals sig ON sig.signature = g.signal_id
                LEFT JOIN source_channels c ON c.telegram_chat_id = sig.source_chat_id
                LEFT JOIN execution_orders o ON o.execution_group_id = g.id
                WHERE g.user_id = ?
                GROUP BY g.id
                ORDER BY g.id DESC LIMIT ?
                """,
                (user_id, safe_limit),
            ).fetchall()
        return {
            "operations": [
                {
                    "id": int(r[0]), "status": r[1], "symbol": r[2], "direction": r[3],
                    "order_type": r[4], "entry_price": r[5], "stop_loss": r[6],
                    "total_volume": r[7], "created_at": r[8], "error_code": r[9],
                    "channel_name": r[10] or "Canal nao identificado", "order_count": int(r[11]),
                    "net_profit": str(r[12]),
                }
                for r in rows
            ]
        }

    @staticmethod
    def _account(row: object) -> dict[str, object] | None:
        if row is None:
            return None
        return {
            "id": int(row[0]), "alias": row[1], "broker": row[2], "server": row[3],
            "masked_login": f"••••{str(row[4])[-4:]}", "account_type": row[5],
            "connection_status": row[6], "balance": row[7], "equity": row[8],
            "worker_heartbeat_at": row[9], "last_error": row[10], "currency": "USD",
        }

    @staticmethod
    def _performance(row: object) -> dict[str, object] | None:
        if row is None:
            return None
        return {
            "date": row[0], "net_profit": row[1], "gross_profit": row[2],
            "trading_costs": row[3], "starting_balance": row[4],
            "return_percent": row[5], "updated_at": row[6],
        }
