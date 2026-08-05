from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import logging
from pathlib import Path
import re
from zoneinfo import ZoneInfo

from ..database import connect_database, utc_now
from ..telegram_notifier import TelegramUserNotifier
from .daily_performance import DEFAULT_BROKER_TIMEZONE
from .models import MT5Account
from .pending_order_executor import MT5_MAGIC_NUMBER, mt5_constant


COMMENT_RE = re.compile(r"^tgcp (?P<signal>[0-9a-f]{8}) TP(?P<tp>\d+)$")
TERMINAL_ORDER_STATUSES = {
    "closed", "cancelled", "expired", "failed", "rejected", "simulated"
}


class SettlementMonitor:
    def __init__(
        self,
        database_path: Path,
        notifier: TelegramUserNotifier,
        *,
        timezone_name: str = DEFAULT_BROKER_TIMEZONE,
        logger: logging.Logger | None = None,
    ) -> None:
        self.database_path = database_path
        self.notifier = notifier
        self.timezone_name = timezone_name
        self.logger = logger or logging.getLogger(__name__)
        self._daily_totals: dict[int, tuple[Decimal, Decimal]] = {}

    def reconcile(self, client: object, account: MT5Account) -> int:
        now = datetime.now(tz=timezone.utc)
        notify_after = self._last_reconciled_at(account.id) or (now - timedelta(seconds=30))
        deals = tuple(client.history_deals_get(now - timedelta(days=2), now))
        closing_entry = {
            mt5_constant(client, "DEAL_ENTRY_OUT", 1),
            mt5_constant(client, "DEAL_ENTRY_OUT_BY", 3),
        }
        inserted = 0
        for deal in deals:
            if int(field(deal, "magic", 0) or 0) != MT5_MAGIC_NUMBER:
                continue
            if int(field(deal, "entry", -1) or -1) not in closing_entry:
                continue
            deal_ticket = str(field(deal, "ticket", "") or "")
            if not deal_ticket:
                continue
            order = self._match_order(account.id, deal, deals)
            if order is None:
                continue
            should_notify = deal_datetime(deal) >= notify_after
            if self._record_close(client, deal, order, deals, should_notify=should_notify):
                inserted += 1
        self._daily_totals[account.id] = daily_totals(
            deals, timezone_name=self.timezone_name
        )
        self._save_reconciled_at(account.id, now)
        return inserted

    def _match_order(
        self, account_id: int, deal: object, history_deals: tuple[object, ...]
    ):
        position_id = str(field(deal, "position_id", field(deal, "position", "")) or "")
        comment_match = COMMENT_RE.match(str(field(deal, "comment", "") or ""))
        if comment_match is None and position_id:
            for related in history_deals:
                related_position = str(
                    field(related, "position_id", field(related, "position", "")) or ""
                )
                if related_position != position_id:
                    continue
                comment_match = COMMENT_RE.match(
                    str(field(related, "comment", "") or "")
                )
                if comment_match is not None:
                    break
        with connect_database(self.database_path) as database:
            if position_id:
                row = database.execute(
                    """
                    SELECT o.id,g.id,g.user_id,g.symbol,g.direction,o.tp_index,
                           o.entry_price,o.take_profit
                    FROM execution_orders o
                    JOIN execution_groups g ON g.id=o.execution_group_id
                    WHERE g.mt5_account_id=? AND
                          (o.mt5_position_ticket=? OR o.mt5_order_ticket=?)
                    ORDER BY o.id DESC LIMIT 1
                    """,
                    (account_id, position_id, position_id),
                ).fetchone()
                if row is not None:
                    return row
            if comment_match:
                return database.execute(
                    """
                    SELECT o.id,g.id,g.user_id,g.symbol,g.direction,o.tp_index,
                           o.entry_price,o.take_profit
                    FROM execution_orders o
                    JOIN execution_groups g ON g.id=o.execution_group_id
                    WHERE g.mt5_account_id=? AND substr(g.signal_id,1,8)=?
                          AND o.tp_index=? ORDER BY o.id DESC LIMIT 1
                    """,
                    (account_id, comment_match.group("signal"), int(comment_match.group("tp"))),
                ).fetchone()
        return None

    def _record_close(
        self,
        client: object,
        deal: object,
        order: object,
        history_deals: tuple[object, ...],
        *,
        should_notify: bool,
    ) -> bool:
        order_id, group_id, user_id = int(order[0]), int(order[1]), int(order[2])
        price = dec(field(deal, "price", 0))
        entry_price = dec(order[6])
        reason = classify_reason(client, deal, price, entry_price)
        position_id = str(field(deal, "position_id", field(deal, "position", "")) or "")
        related = tuple(
            item for item in history_deals
            if position_id and str(field(item, "position_id", field(item, "position", "")) or "") == position_id
        ) or (deal,)
        gross = sum((dec(field(item, "profit", 0)) for item in related), Decimal("0"))
        commission = sum((dec(field(item, "commission", 0)) for item in related), Decimal("0"))
        swap = sum((dec(field(item, "swap", 0)) for item in related), Decimal("0"))
        fee = sum((dec(field(item, "fee", 0)) for item in related), Decimal("0"))
        net = gross + commission + swap + fee
        closed_at = deal_datetime(deal).isoformat()
        ticket = str(field(deal, "ticket", ""))
        now = utc_now()
        with connect_database(self.database_path) as database:
            cursor = database.execute(
                """
                INSERT OR IGNORE INTO execution_close_events (
                    user_id,mt5_account_id,execution_group_id,execution_order_id,
                    mt5_deal_ticket,close_reason,close_price,gross_profit,commission,
                    swap,fee,net_profit,closed_at,notification_status,created_at,updated_at
                )
                SELECT ?,g.mt5_account_id,?,?,?,?,?,?,?,?,?,?,?,?,?,?
                FROM execution_groups g WHERE g.id=?
                """,
                (user_id, group_id, order_id, ticket, reason, str(price), str(gross),
                 str(commission), str(swap), str(fee), str(net), closed_at,
                 "pending" if should_notify else "skipped", now, now, group_id),
            )
            created = cursor.rowcount == 1
            cursor.close()
            if not created:
                return False
            database.execute(
                """
                UPDATE execution_orders SET status='closed',closed_at=?,close_reason=?,
                    close_price=?,gross_profit=?,commission=?,swap=?,fee=?,net_profit=?,
                    mt5_close_deal_ticket=?,updated_at=? WHERE id=?
                """,
                (closed_at, reason, str(price), str(gross), str(commission), str(swap),
                 str(fee), str(net), ticket, now, order_id),
            ).close()
            database.execute(
                """
                UPDATE execution_groups SET status='closed',updated_at=? WHERE id=?
                AND NOT EXISTS (SELECT 1 FROM execution_orders WHERE execution_group_id=?
                  AND status NOT IN ('closed','cancelled','expired','failed','rejected','simulated'))
                """,
                (now, group_id, group_id),
            ).close()
        return True

    def _last_reconciled_at(self, account_id: int) -> datetime | None:
        with connect_database(self.database_path) as database:
            row = database.execute(
                "SELECT last_reconciled_at FROM mt5_settlement_state WHERE mt5_account_id=?",
                (account_id,),
            ).fetchone()
        return datetime.fromisoformat(str(row[0])) if row else None

    def _save_reconciled_at(self, account_id: int, reconciled_at: datetime) -> None:
        with connect_database(self.database_path) as database:
            database.execute(
                """INSERT INTO mt5_settlement_state(mt5_account_id,last_reconciled_at)
                   VALUES(?,?) ON CONFLICT(mt5_account_id) DO UPDATE SET
                   last_reconciled_at=excluded.last_reconciled_at""",
                (account_id, reconciled_at.isoformat()),
            ).close()

    def deliver_pending(self, account: MT5Account) -> None:
        with connect_database(self.database_path) as database:
            rows = database.execute(
                """
                SELECT e.id,e.user_id,u.telegram_user_id,e.execution_group_id,
                       g.symbol,g.direction,o.tp_index,e.close_reason,e.net_profit,
                       e.notification_attempts
                FROM execution_close_events e
                JOIN users u ON u.id=e.user_id
                JOIN execution_groups g ON g.id=e.execution_group_id
                JOIN execution_orders o ON o.id=e.execution_order_id
                WHERE e.mt5_account_id=? AND e.notification_status='pending'
                  AND e.notification_attempts < 100 ORDER BY e.id LIMIT 20
                """,
                (account.id,),
            ).fetchall()
        for row in rows:
            event_id, _user_id, telegram_id, _group_id = map(int, row[:4])
            mode = self.result_mode(_user_id)
            if mode == "off":
                self._mark_delivery(event_id, "skipped")
                continue
            robot_total, account_total = self._daily_totals.get(
                account.id, (Decimal("0"), Decimal("0"))
            )
            label = reason_label(str(row[7]), int(row[6]))
            message = "\n".join([
                f"{label}", "",
                f"{row[4]} — {row[5]}",
                f"Conta: {account.masked_login}",
                f"Resultado desta ordem: {money(dec(row[8]))}", "",
                f"Resultado do robô hoje: {money(robot_total)}",
                f"Resultado total da conta hoje: {money(account_total)}",
            ])
            if self.notifier.send(telegram_id, message):
                self._mark_delivery(event_id, "sent")
                self.logger.info("Resultado MT5 notificado. conta=%s evento=%s", account.id, event_id)
            else:
                self._mark_delivery(event_id, "pending", error="telegram_send_failed")

    def _mark_delivery(self, event_id: int, status: str, error: str | None = None) -> None:
        with connect_database(self.database_path) as database:
            database.execute(
                """UPDATE execution_close_events SET notification_status=?,
                   notification_attempts=notification_attempts+1,last_error=?,updated_at=?
                   WHERE id=?""",
                (status, error, utc_now(), event_id),
            ).close()

    def result_mode(self, user_id: int) -> str:
        with connect_database(self.database_path) as database:
            row = database.execute(
                "SELECT result_mode FROM user_notification_settings WHERE user_id=?",
                (user_id,),
            ).fetchone()
        return str(row[0]) if row else "all"


def set_result_mode(database_path: Path, user_id: int, mode: str) -> str:
    if mode not in {"all", "off"}:
        raise ValueError("Modo de notificação inválido.")
    with connect_database(database_path) as database:
        database.execute(
            """INSERT INTO user_notification_settings(user_id,result_mode,updated_at)
               VALUES(?,?,?) ON CONFLICT(user_id) DO UPDATE SET
               result_mode=excluded.result_mode,updated_at=excluded.updated_at""",
            (user_id, mode, utc_now()),
        ).close()
    return mode


def get_result_mode(database_path: Path, user_id: int) -> str:
    with connect_database(database_path) as database:
        row = database.execute(
            "SELECT result_mode FROM user_notification_settings WHERE user_id=?", (user_id,)
        ).fetchone()
    return str(row[0]) if row else "all"


def daily_totals(deals: tuple[object, ...], *, timezone_name: str) -> tuple[Decimal, Decimal]:
    local_date = datetime.now(tz=timezone.utc).astimezone(ZoneInfo(timezone_name)).date()
    robot = Decimal("0")
    account = Decimal("0")
    for deal in deals:
        if deal_datetime(deal).astimezone(ZoneInfo(timezone_name)).date() != local_date:
            continue
        deal_type = field(deal, "type", None)
        if deal_type is not None and int(deal_type) not in {0, 1}:
            continue
        value = sum((dec(field(deal, name, 0)) for name in ("profit", "commission", "swap", "fee")), Decimal("0"))
        account += value
        if int(field(deal, "magic", 0) or 0) == MT5_MAGIC_NUMBER:
            robot += value
    return robot, account


def classify_reason(client: object, deal: object, close_price: Decimal, entry_price: Decimal) -> str:
    reason = int(field(deal, "reason", -1) or -1)
    if reason == mt5_constant(client, "DEAL_REASON_TP", 5):
        return "take_profit"
    if reason == mt5_constant(client, "DEAL_REASON_SL", 4):
        tolerance = max(Decimal("0.05"), abs(entry_price) * Decimal("0.00001"))
        return "breakeven" if abs(close_price - entry_price) <= tolerance else "stop_loss"
    if reason == mt5_constant(client, "DEAL_REASON_SO", 6):
        return "stop_out"
    return "manual_or_other"


def reason_label(reason: str, tp_index: int) -> str:
    return {
        "take_profit": f"✅ TP{tp_index} ATINGIDO",
        "breakeven": "🟰 ORDEM FECHADA NO BREAKEVEN",
        "stop_loss": "🛑 STOP LOSS ATINGIDO",
        "stop_out": "🚨 ORDEM ENCERRADA POR STOP OUT",
    }.get(reason, "ℹ️ ORDEM ENCERRADA")


def money(value: Decimal) -> str:
    sign = "+" if value > 0 else ""
    return f"{sign}US$ {value.quantize(Decimal('0.01'))}"


def deal_datetime(deal: object) -> datetime:
    timestamp = field(deal, "time", None)
    if timestamp is None:
        return datetime.now(tz=timezone.utc)
    return datetime.fromtimestamp(float(timestamp), tz=timezone.utc)


def field(item: object, name: str, default: object = None) -> object:
    return item.get(name, default) if isinstance(item, dict) else getattr(item, name, default)


def dec(value: object) -> Decimal:
    return Decimal(str(value or 0))
