from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from html import escape
from pathlib import Path

from .database import connect_database, initialize_database
from .mt5.models import mask_login
from .users import USER_STATUS_ACTIVE, USER_STATUS_PAUSED, UserRepository
from .web_app import TelegramWebAppInitData, WebAppValidationError, validate_telegram_web_app_init_data

BILLING_STATUSES = {"pending", "paid", "overdue", "exempt", "cancelled"}


@dataclass(frozen=True)
class AdminIdentity:
    telegram_user_id: int
    username: str | None


class AdminPanelService:
    def __init__(
        self,
        database_path: Path,
        *,
        bot_token: str,
        admin_ids: tuple[int, ...],
    ) -> None:
        self.database_path = database_path
        self.bot_token = bot_token
        self.admin_ids = frozenset(admin_ids)
        initialize_database(database_path)

    def authenticate(self, init_data: str) -> AdminIdentity:
        parsed = validate_telegram_web_app_init_data(init_data, self.bot_token)
        if parsed.user.id not in self.admin_ids:
            raise WebAppValidationError("Administrador não autorizado.")
        return AdminIdentity(
            telegram_user_id=parsed.user.id,
            username=parsed.user.username,
        )

    def dashboard(self) -> dict[str, object]:
        with connect_database(self.database_path) as connection:
            summary = connection.execute(
                """
                SELECT
                    COUNT(*),
                    SUM(CASE WHEN status = 'active' THEN 1 ELSE 0 END),
                    SUM(CASE WHEN status = 'paused' THEN 1 ELSE 0 END)
                FROM users
                """
            ).fetchone()
            connected_accounts = connection.execute(
                "SELECT COUNT(*) FROM mt5_accounts WHERE connection_status = 'connected'"
            ).fetchone()[0]
            attention_accounts = connection.execute(
                """
                SELECT COUNT(*) FROM mt5_accounts
                WHERE connection_status != 'connected' OR last_error IS NOT NULL
                """
            ).fetchone()[0]
            active_groups = connection.execute(
                """
                SELECT COUNT(*) FROM execution_groups
                WHERE status IN ('pending_submission', 'pending_active')
                """
            ).fetchone()[0]
            rows = connection.execute(
                """
                SELECT
                    u.id, u.telegram_user_id, u.telegram_username, u.status,
                    u.created_at, u.updated_at,
                    a.id, a.account_alias, a.login, a.server_name, a.account_type,
                    a.connection_status, a.balance, a.equity, a.worker_heartbeat_at,
                    a.last_error,
                    p.risk_mode, p.fixed_lot, p.risk_percent,
                    p.daily_profit_target, p.daily_loss_limit, p.max_open_signals,
                    (SELECT COUNT(*) FROM execution_groups g WHERE g.user_id = u.id),
                    (
                        SELECT COUNT(*) FROM execution_groups g
                        WHERE g.user_id = u.id
                          AND g.status IN ('pending_submission', 'pending_active')
                    ),
                    (
                        SELECT MAX(g.created_at) FROM execution_groups g
                        WHERE g.user_id = u.id
                    ),
                    b.customer_name, b.email, b.phone, b.plan_name,
                    b.monthly_amount, b.due_date, b.billing_status,
                    b.last_paid_at, b.notes,
                    (
                        SELECT cp.amount FROM customer_payments cp
                        WHERE cp.user_id = u.id AND cp.status = 'paid'
                        ORDER BY cp.paid_at DESC, cp.id DESC LIMIT 1
                    )
                FROM users u
                LEFT JOIN mt5_accounts a ON a.id = (
                    SELECT a2.id FROM mt5_accounts a2
                    WHERE a2.user_id = u.id ORDER BY a2.id DESC LIMIT 1
                )
                LEFT JOIN execution_profiles p
                    ON p.user_id = u.id AND p.mt5_account_id = a.id
                LEFT JOIN customer_billing b ON b.user_id = u.id
                ORDER BY u.id DESC
                """
            ).fetchall()
            payment_rows = connection.execute(
                """
                SELECT id, user_id, amount, paid_at, method, reference, status
                FROM customer_payments
                ORDER BY paid_at DESC, id DESC
                """
            ).fetchall()

        users = [admin_user_payload(row) for row in rows]
        payments_by_user: dict[int, list[dict[str, object]]] = {}
        for payment in payment_rows:
            bucket = payments_by_user.setdefault(int(payment[1]), [])
            if len(bucket) >= 12:
                continue
            bucket.append(
                {
                    "id": int(payment[0]),
                    "amount": decimal_text(payment[2]),
                    "paid_at": str(payment[3]),
                    "method": str(payment[4]) if payment[4] else None,
                    "reference": str(payment[5]) if payment[5] else None,
                    "status": str(payment[6]),
                }
            )
        for user in users:
            user["billing"]["payments"] = payments_by_user.get(int(user["id"]), [])
        today = date.today()
        billing_summary = {
            "paid": 0,
            "pending": 0,
            "overdue": 0,
            "due_soon": 0,
            "unconfigured": 0,
            "monthly_total": Decimal("0"),
        }
        for user in users:
            billing = user["billing"]
            status = effective_billing_status(
                str(billing["status"]),
                str(billing["due_date"]) if billing["due_date"] else None,
                today=today,
            )
            billing["effective_status"] = status
            if status == "paid":
                billing_summary["paid"] += 1
            elif status == "overdue":
                billing_summary["overdue"] += 1
            unconfigured = not billing["due_date"] and Decimal(str(billing["monthly_amount"])) <= 0
            if unconfigured:
                billing_summary["unconfigured"] += 1
            elif status == "pending":
                billing_summary["pending"] += 1
            if billing["due_date"]:
                due = date.fromisoformat(str(billing["due_date"]))
                if today <= due <= today + timedelta(days=7):
                    billing_summary["due_soon"] += 1
            if status not in {"exempt", "cancelled"}:
                billing_summary["monthly_total"] += Decimal(str(billing["monthly_amount"]))

        return {
            "summary": {
                "users": int(summary[0] or 0),
                "active": int(summary[1] or 0),
                "paused": int(summary[2] or 0),
                "connected_accounts": int(connected_accounts or 0),
                "attention_accounts": int(attention_accounts or 0),
                "active_groups": int(active_groups or 0),
                "billing_paid": int(billing_summary["paid"]),
                "billing_pending": int(billing_summary["pending"]),
                "billing_overdue": int(billing_summary["overdue"]),
                "billing_due_soon": int(billing_summary["due_soon"]),
                "billing_unconfigured": int(billing_summary["unconfigured"]),
                "monthly_total": decimal_text(billing_summary["monthly_total"]),
            },
            "users": users,
        }

    def set_user_status(
        self,
        *,
        admin_telegram_user_id: int,
        target_user_id: int,
        status: str,
    ) -> dict[str, object]:
        if status not in {USER_STATUS_ACTIVE, USER_STATUS_PAUSED}:
            raise ValueError("Status de usuário inválido.")
        users = UserRepository(self.database_path)
        try:
            target = users.get_by_id(target_user_id)
            updated = users.set_status(target.id, status)
            users.log_admin_action(
                admin_telegram_user_id=admin_telegram_user_id,
                target_user_id=target.id,
                action_type="admin_panel_set_user_status",
                payload={
                    "previous_status": target.status,
                    "status": updated.status,
                    "source": "admin_panel",
                },
            )
        finally:
            users.close()
        return {
            "id": updated.id,
            "status": updated.status,
        }

    def update_billing(
        self,
        *,
        admin_telegram_user_id: int,
        target_user_id: int,
        customer_name: str,
        email: str,
        phone: str,
        plan_name: str,
        monthly_amount: str,
        due_date: str,
        billing_status: str,
        notes: str,
    ) -> dict[str, object]:
        self._require_user(target_user_id)
        status = billing_status.strip().lower()
        if status not in BILLING_STATUSES:
            raise ValueError("Situação financeira inválida.")
        amount = validated_amount(monthly_amount, allow_zero=True)
        normalized_due_date = validated_date(due_date, required=False)
        now = datetime.now(tz=timezone.utc).isoformat()
        values = {
            "customer_name": limited_text(customer_name, 120),
            "email": limited_text(email, 180),
            "phone": limited_text(phone, 40),
            "plan_name": limited_text(plan_name, 80) or "Mensal",
            "monthly_amount": decimal_text(amount),
            "due_date": normalized_due_date,
            "billing_status": status,
            "notes": limited_text(notes, 1000),
        }
        with connect_database(self.database_path) as connection:
            connection.execute(
                """
                INSERT INTO customer_billing (
                    user_id, customer_name, email, phone, plan_name,
                    monthly_amount, due_date, billing_status, last_paid_at,
                    notes, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    customer_name = excluded.customer_name,
                    email = excluded.email,
                    phone = excluded.phone,
                    plan_name = excluded.plan_name,
                    monthly_amount = excluded.monthly_amount,
                    due_date = excluded.due_date,
                    billing_status = excluded.billing_status,
                    notes = excluded.notes,
                    updated_at = excluded.updated_at
                """,
                (
                    target_user_id,
                    values["customer_name"],
                    values["email"],
                    values["phone"],
                    values["plan_name"],
                    values["monthly_amount"],
                    values["due_date"],
                    values["billing_status"],
                    values["notes"],
                    now,
                    now,
                ),
            ).close()
        self._log_admin_action(
            admin_telegram_user_id,
            target_user_id,
            "admin_panel_update_billing",
            {
                "plan_name": values["plan_name"],
                "monthly_amount": values["monthly_amount"],
                "due_date": values["due_date"],
                "billing_status": values["billing_status"],
            },
        )
        return {"user_id": target_user_id, **values}

    def record_payment(
        self,
        *,
        admin_telegram_user_id: int,
        target_user_id: int,
        amount: str,
        paid_at: str,
        method: str,
        reference: str,
        next_due_date: str,
    ) -> dict[str, object]:
        self._require_user(target_user_id)
        normalized_paid_at = validated_date(paid_at, required=True)
        with connect_database(self.database_path) as connection:
            billing = connection.execute(
                """
                SELECT monthly_amount, due_date FROM customer_billing
                WHERE user_id = ?
                """,
                (target_user_id,),
            ).fetchone()
        fallback_amount = str(billing[0]) if billing else "0"
        payment_amount = validated_amount(amount or fallback_amount, allow_zero=False)
        normalized_next_due = (
            validated_date(next_due_date, required=True)
            if next_due_date.strip()
            else add_one_month(str(billing[1]) if billing and billing[1] else normalized_paid_at)
        )
        now = datetime.now(tz=timezone.utc).isoformat()
        with connect_database(self.database_path) as connection:
            connection.execute(
                """
                INSERT INTO customer_payments (
                    user_id, amount, paid_at, period_start, period_end,
                    method, reference, status, admin_telegram_user_id, created_at
                )
                VALUES (?, ?, ?, NULL, ?, ?, ?, 'paid', ?, ?)
                """,
                (
                    target_user_id,
                    decimal_text(payment_amount),
                    normalized_paid_at,
                    normalized_next_due,
                    limited_text(method, 80),
                    limited_text(reference, 180),
                    admin_telegram_user_id,
                    now,
                ),
            ).close()
            connection.execute(
                """
                INSERT INTO customer_billing (
                    user_id, plan_name, monthly_amount, due_date,
                    billing_status, last_paid_at, created_at, updated_at
                )
                VALUES (?, 'Mensal', ?, ?, 'paid', ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    monthly_amount = CASE
                        WHEN customer_billing.monthly_amount = '0'
                        THEN excluded.monthly_amount
                        ELSE customer_billing.monthly_amount
                    END,
                    due_date = excluded.due_date,
                    billing_status = 'paid',
                    last_paid_at = excluded.last_paid_at,
                    updated_at = excluded.updated_at
                """,
                (
                    target_user_id,
                    decimal_text(payment_amount),
                    normalized_next_due,
                    normalized_paid_at,
                    now,
                    now,
                ),
            ).close()
        self._log_admin_action(
            admin_telegram_user_id,
            target_user_id,
            "admin_panel_record_payment",
            {
                "amount": decimal_text(payment_amount),
                "paid_at": normalized_paid_at,
                "next_due_date": normalized_next_due,
            },
        )
        return {
            "user_id": target_user_id,
            "amount": decimal_text(payment_amount),
            "paid_at": normalized_paid_at,
            "next_due_date": normalized_next_due,
        }

    def _require_user(self, user_id: int) -> None:
        users = UserRepository(self.database_path)
        try:
            users.get_by_id(user_id)
        finally:
            users.close()

    def _log_admin_action(
        self,
        admin_telegram_user_id: int,
        target_user_id: int,
        action_type: str,
        payload: dict[str, object],
    ) -> None:
        users = UserRepository(self.database_path)
        try:
            users.log_admin_action(
                admin_telegram_user_id=admin_telegram_user_id,
                target_user_id=target_user_id,
                action_type=action_type,
                payload={**payload, "source": "admin_panel"},
            )
        finally:
            users.close()


def admin_user_payload(row: tuple[object, ...]) -> dict[str, object]:
    account_id = int(row[6]) if row[6] is not None else None
    return {
        "id": int(row[0]),
        "telegram_user_id": int(row[1]),
        "username": str(row[2]) if row[2] else None,
        "status": str(row[3]),
        "created_at": str(row[4]),
        "updated_at": str(row[5]),
        "account": None
        if account_id is None
        else {
            "id": account_id,
            "alias": str(row[7]),
            "masked_login": mask_login(str(row[8])),
            "server": str(row[9]),
            "type": str(row[10]),
            "connection_status": str(row[11]),
            "balance": decimal_text(row[12]),
            "equity": decimal_text(row[13]),
            "worker_heartbeat_at": str(row[14]) if row[14] else None,
            "last_error": str(row[15]) if row[15] else None,
        },
        "profile": None
        if row[16] is None
        else {
            "risk_mode": str(row[16]),
            "fixed_lot": decimal_text(row[17]),
            "risk_percent": decimal_text(row[18]),
            "daily_profit_target": decimal_text(row[19]),
            "daily_loss_limit": decimal_text(row[20]),
            "max_open_signals": int(row[21]),
        },
        "execution_count": int(row[22] or 0),
        "active_group_count": int(row[23] or 0),
        "last_execution_at": str(row[24]) if row[24] else None,
        "billing": {
            "customer_name": str(row[25]) if row[25] else None,
            "email": str(row[26]) if row[26] else None,
            "phone": str(row[27]) if row[27] else None,
            "plan_name": str(row[28]) if row[28] else "Mensal",
            "monthly_amount": decimal_text(row[29]) or "0",
            "due_date": str(row[30]) if row[30] else None,
            "status": str(row[31]) if row[31] else "pending",
            "last_paid_at": str(row[32]) if row[32] else None,
            "notes": str(row[33]) if row[33] else None,
            "last_payment_amount": decimal_text(row[34]),
        },
    }


def decimal_text(value: object) -> str | None:
    if value is None:
        return None
    return format(Decimal(str(value)), "f")


def validated_amount(value: str, *, allow_zero: bool) -> Decimal:
    try:
        amount = Decimal(value.replace(",", ".").strip())
    except Exception as exc:
        raise ValueError("Valor da mensalidade inválido.") from exc
    minimum = Decimal("0") if allow_zero else Decimal("0.01")
    if amount < minimum or amount > Decimal("1000000"):
        raise ValueError("Valor da mensalidade fora do intervalo permitido.")
    return amount.quantize(Decimal("0.01"))


def validated_date(value: str, *, required: bool) -> str | None:
    normalized = value.strip()
    if not normalized:
        if required:
            raise ValueError("Data obrigatória.")
        return None
    try:
        return date.fromisoformat(normalized).isoformat()
    except ValueError as exc:
        raise ValueError("Data inválida.") from exc


def limited_text(value: str, maximum: int) -> str | None:
    normalized = value.strip()
    if not normalized:
        return None
    if len(normalized) > maximum:
        raise ValueError("Texto acima do tamanho permitido.")
    return normalized


def effective_billing_status(
    status: str,
    due_date: str | None,
    *,
    today: date | None = None,
) -> str:
    current = today or date.today()
    if status in {"exempt", "cancelled"}:
        return status
    if due_date and date.fromisoformat(due_date) < current:
        return "overdue"
    return status


def add_one_month(value: str) -> str:
    current = date.fromisoformat(value)
    year = current.year + (1 if current.month == 12 else 0)
    month = 1 if current.month == 12 else current.month + 1
    day = min(current.day, calendar.monthrange(year, month)[1])
    return date(year, month, day).isoformat()


def render_admin_panel(script_nonce: str = "") -> str:
    nonce_attribute = f' nonce="{escape(script_nonce, quote=True)}"' if script_nonce else ""
    return f"""<!doctype html>
<html lang="pt-BR">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="theme-color" content="#07111f">
  <title>Painel Admin — Instituto Trader</title>
  <style>
    :root {{
      color-scheme: dark;
      --bg: #07111f;
      --panel: #0d1b2e;
      --panel-soft: #11243b;
      --line: #213954;
      --text: #eef6ff;
      --muted: #8fa8c2;
      --cyan: #39d7c4;
      --blue: #4b8fff;
      --green: #45d483;
      --yellow: #f2bd4f;
      --red: #ff6b78;
      --shadow: 0 18px 55px rgba(0, 0, 0, .28);
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      min-height: 100vh;
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background:
        radial-gradient(circle at 15% -10%, rgba(57, 215, 196, .16), transparent 34rem),
        radial-gradient(circle at 100% 10%, rgba(75, 143, 255, .14), transparent 30rem),
        var(--bg);
      color: var(--text);
    }}
    button, input, select, textarea {{ font: inherit; }}
    .shell {{ max-width: 1280px; margin: 0 auto; padding: 26px 18px 60px; }}
    header {{ display: flex; justify-content: space-between; align-items: flex-start; gap: 18px; margin-bottom: 28px; }}
    .eyebrow {{ color: var(--cyan); font-size: 12px; font-weight: 800; letter-spacing: .16em; text-transform: uppercase; }}
    h1 {{ margin: 6px 0 4px; font-size: clamp(28px, 5vw, 44px); line-height: 1; letter-spacing: -.035em; }}
    .subtitle {{ color: var(--muted); margin: 0; }}
    .header-actions {{ display: flex; gap: 8px; }}
    .refresh, .logout {{
      border: 1px solid var(--line); border-radius: 12px; padding: 11px 15px;
      color: var(--text); background: rgba(17, 36, 59, .82); cursor: pointer;
    }}
    .refresh:hover, .logout:hover {{ border-color: var(--cyan); }}
    .notice {{ padding: 14px 16px; border: 1px solid var(--line); background: var(--panel); border-radius: 14px; color: var(--muted); }}
    .notice.error {{ color: #ffd9dd; border-color: rgba(255,107,120,.55); background: rgba(92, 25, 38, .45); }}
    .summary {{ display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); gap: 12px; margin: 20px 0 26px; }}
    .metric {{ padding: 17px; border: 1px solid var(--line); border-radius: 16px; background: linear-gradient(145deg, rgba(17,36,59,.96), rgba(10,25,43,.96)); box-shadow: var(--shadow); }}
    .metric span {{ display: block; color: var(--muted); font-size: 12px; min-height: 30px; }}
    .metric strong {{ display: block; margin-top: 8px; font-size: 27px; letter-spacing: -.03em; }}
    .toolbar {{ display: flex; align-items: center; gap: 10px; flex-wrap: wrap; margin-bottom: 15px; }}
    .search {{ flex: 1 1 280px; min-width: 0; padding: 12px 14px; border: 1px solid var(--line); border-radius: 12px; background: #091727; color: var(--text); outline: none; }}
    .search:focus {{ border-color: var(--cyan); box-shadow: 0 0 0 3px rgba(57,215,196,.1); }}
    .filters {{ display: flex; gap: 7px; overflow-x: auto; }}
    .filter {{ border: 1px solid var(--line); border-radius: 999px; padding: 9px 12px; color: var(--muted); background: transparent; cursor: pointer; white-space: nowrap; }}
    .filter.active {{ color: #06151a; background: var(--cyan); border-color: var(--cyan); font-weight: 800; }}
    .list {{ display: grid; gap: 12px; }}
    .user-card {{ border: 1px solid var(--line); border-radius: 18px; background: rgba(13, 27, 46, .94); box-shadow: var(--shadow); overflow: hidden; }}
    .user-main {{ display: grid; grid-template-columns: minmax(190px, 1.1fr) minmax(190px, 1fr) minmax(180px, .9fr) minmax(180px, .9fr) auto; gap: 18px; align-items: center; padding: 18px; }}
    .identity {{ min-width: 0; }}
    .identity h2 {{ margin: 0 0 5px; font-size: 18px; overflow-wrap: anywhere; }}
    .meta, .detail {{ color: var(--muted); font-size: 13px; line-height: 1.65; }}
    .status {{ display: inline-flex; align-items: center; gap: 7px; padding: 6px 9px; border-radius: 999px; font-size: 12px; font-weight: 800; }}
    .status::before {{ content: ""; width: 7px; height: 7px; border-radius: 50%; background: currentColor; }}
    .status.active, .status.connected {{ color: var(--green); background: rgba(69,212,131,.1); }}
    .status.paused {{ color: var(--yellow); background: rgba(242,189,79,.1); }}
    .status.failed, .status.disconnected {{ color: var(--red); background: rgba(255,107,120,.1); }}
    .status.paid {{ color: var(--green); background: rgba(69,212,131,.1); }}
    .status.overdue {{ color: var(--red); background: rgba(255,107,120,.1); }}
    .status.pending {{ color: var(--yellow); background: rgba(242,189,79,.1); }}
    .status.exempt, .status.cancelled {{ color: var(--muted); background: rgba(143,168,194,.1); }}
    .account-name {{ color: var(--text); font-weight: 750; }}
    .money {{ color: var(--text); }}
    .actions {{ display: flex; flex-direction: column; gap: 8px; min-width: 112px; }}
    .action {{ border: 0; border-radius: 11px; padding: 10px 12px; cursor: pointer; font-weight: 800; }}
    .action.activate {{ color: #06170e; background: var(--green); }}
    .action.pause {{ color: #251a00; background: var(--yellow); }}
    .action.finance {{ color: var(--text); background: var(--blue); }}
    .action:disabled {{ opacity: .55; cursor: wait; }}
    .empty {{ padding: 35px; text-align: center; color: var(--muted); border: 1px dashed var(--line); border-radius: 16px; }}
    dialog {{ width: min(760px, calc(100vw - 24px)); max-height: calc(100vh - 24px); overflow: auto; color: var(--text); background: var(--panel); border: 1px solid var(--line); border-radius: 18px; padding: 0; box-shadow: var(--shadow); }}
    dialog::backdrop {{ background: rgba(2, 8, 17, .78); backdrop-filter: blur(4px); }}
    .dialog-head {{ display: flex; justify-content: space-between; align-items: center; padding: 18px 20px; border-bottom: 1px solid var(--line); }}
    .dialog-head h2 {{ margin: 0; font-size: 21px; }}
    .close {{ border: 0; background: transparent; color: var(--muted); font-size: 24px; cursor: pointer; }}
    .dialog-body {{ padding: 20px; display: grid; gap: 22px; }}
    .form-section {{ display: grid; gap: 12px; }}
    .form-section h3 {{ margin: 0; font-size: 15px; color: var(--cyan); }}
    .form-grid {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; }}
    label {{ display: grid; gap: 6px; color: var(--muted); font-size: 12px; }}
    label.wide {{ grid-column: 1 / -1; }}
    label input, label select, label textarea {{ width: 100%; color: var(--text); background: #091727; border: 1px solid var(--line); border-radius: 10px; padding: 10px 11px; outline: none; }}
    label textarea {{ min-height: 72px; resize: vertical; }}
    .form-actions {{ display: flex; justify-content: flex-end; gap: 9px; }}
    .save {{ border: 0; border-radius: 10px; padding: 11px 15px; color: #06151a; background: var(--cyan); font-weight: 800; cursor: pointer; }}
    .payment {{ background: var(--green); }}
    .payment-history {{ display: grid; gap: 7px; color: var(--muted); font-size: 13px; }}
    .payment-row {{ display: flex; justify-content: space-between; gap: 12px; padding: 9px 11px; background: #091727; border: 1px solid var(--line); border-radius: 10px; }}
    @media (max-width: 980px) {{
      .summary {{ grid-template-columns: repeat(3, 1fr); }}
      .user-main {{ grid-template-columns: 1fr 1fr; }}
      .actions {{ flex-direction: row; }}
    }}
    @media (max-width: 620px) {{
      .shell {{ padding: 19px 12px 45px; }}
      header {{ align-items: center; }}
      .subtitle {{ font-size: 13px; }}
      .summary {{ grid-template-columns: repeat(2, 1fr); }}
      .metric {{ padding: 14px; }}
      .user-main {{ grid-template-columns: 1fr; gap: 12px; padding: 15px; }}
      .actions {{ width: 100%; }}
      .action {{ flex: 1; }}
      .form-grid {{ grid-template-columns: 1fr; }}
      label.wide {{ grid-column: auto; }}
    }}
  </style>
  <script defer src="https://telegram.org/js/telegram-web-app.js"></script>
  <script{nonce_attribute} defer src="/admin.js"></script>
</head>
<body>
  <main class="shell">
    <header>
      <div>
        <div class="eyebrow">Instituto Trader · Master</div>
        <h1>Central de clientes</h1>
        <p class="subtitle">Acesso, conexão MT5 e atividade operacional em um único lugar.</p>
      </div>
      <div class="header-actions">
        <button class="refresh" id="refresh" type="button">Atualizar</button>
        <button class="logout" id="logout" type="button" hidden>Sair</button>
      </div>
    </header>
    <div class="notice" id="notice">Validando seu acesso administrativo pelo Telegram…</div>
    <section class="summary" id="summary" aria-label="Resumo operacional" hidden></section>
    <section id="workspace" hidden>
      <div class="toolbar">
        <input class="search" id="search" type="search" placeholder="Buscar nome, ID, conta ou servidor" aria-label="Buscar clientes">
        <div class="filters" id="filters" aria-label="Filtrar clientes">
          <button class="filter active" type="button" data-filter="all">Todos</button>
          <button class="filter" type="button" data-filter="active">Ativos</button>
          <button class="filter" type="button" data-filter="paused">Pausados</button>
          <button class="filter" type="button" data-filter="attention">Atenção</button>
          <button class="filter" type="button" data-filter="overdue">Em atraso</button>
          <button class="filter" type="button" data-filter="pending">Pendentes</button>
          <button class="filter" type="button" data-filter="due_soon">Vence em 7 dias</button>
        </div>
      </div>
      <div class="list" id="user-list"></div>
    </section>
    <dialog id="finance-dialog">
      <div class="dialog-head">
        <h2 id="finance-title">Financeiro do cliente</h2>
        <button class="close" id="finance-close" type="button" aria-label="Fechar">×</button>
      </div>
      <div class="dialog-body">
        <form class="form-section" id="billing-form">
          <h3>Cadastro e mensalidade</h3>
          <input id="billing-user-id" type="hidden">
          <div class="form-grid">
            <label>Nome do cliente<input id="customer-name" maxlength="120"></label>
            <label>Plano<input id="plan-name" maxlength="80" value="Mensal"></label>
            <label>E-mail<input id="customer-email" type="email" maxlength="180"></label>
            <label>Telefone<input id="customer-phone" maxlength="40"></label>
            <label>Mensalidade ($)<input id="monthly-amount" inputmode="decimal" required></label>
            <label>Próximo vencimento<input id="due-date" type="date"></label>
            <label>Situação<select id="billing-status">
              <option value="pending">Pendente</option>
              <option value="paid">Pago</option>
              <option value="overdue">Em atraso</option>
              <option value="exempt">Isento</option>
              <option value="cancelled">Cancelado</option>
            </select></label>
            <label class="wide">Observações<textarea id="billing-notes" maxlength="1000"></textarea></label>
          </div>
          <div class="form-actions"><button class="save" type="submit">Salvar cadastro</button></div>
        </form>
        <form class="form-section" id="payment-form">
          <h3>Registrar pagamento</h3>
          <div class="form-grid">
            <label>Valor pago ($)<input id="payment-amount" inputmode="decimal" required></label>
            <label>Data do pagamento<input id="paid-at" type="date" required></label>
            <label>Forma de pagamento<input id="payment-method" maxlength="80" placeholder="PIX, cartão, transferência"></label>
            <label>Referência<input id="payment-reference" maxlength="180" placeholder="Comprovante ou observação"></label>
            <label>Próximo vencimento (opcional)<input id="next-due-date" type="date"></label>
          </div>
          <div class="form-actions"><button class="save payment" type="submit">Confirmar pagamento</button></div>
        </form>
        <section class="form-section">
          <h3>Histórico de pagamentos</h3>
          <div class="payment-history" id="payment-history"></div>
        </section>
      </div>
    </dialog>
  </main>
</body>
</html>"""


def render_admin_script() -> str:
    return r"""
(function () {
  "use strict";
  var tg = window.Telegram && window.Telegram.WebApp ? window.Telegram.WebApp : null;
  var state = { users: [], summary: {}, csrf: "", filter: "all", query: "", busy: false, browser: false };
  var notice = document.getElementById("notice");
  var summary = document.getElementById("summary");
  var workspace = document.getElementById("workspace");
  var list = document.getElementById("user-list");
  var search = document.getElementById("search");
  var filters = document.getElementById("filters");
  var refresh = document.getElementById("refresh");
  var logout = document.getElementById("logout");
  var dialog = document.getElementById("finance-dialog");
  var billingForm = document.getElementById("billing-form");
  var paymentForm = document.getElementById("payment-form");

  function encode(fields) {
    return Object.keys(fields).map(function (key) {
      return encodeURIComponent(key) + "=" + encodeURIComponent(fields[key] == null ? "" : fields[key]);
    }).join("&");
  }

  function post(path, fields) {
    return fetch(path, {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: encode(fields)
    }).then(function (response) {
      return response.json().catch(function () { return {}; }).then(function (data) {
        if (!response.ok) { throw new Error(data.error || "Falha na solicitação."); }
        return data;
      });
    });
  }

  function esc(value) {
    return String(value == null ? "" : value)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;")
      .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  }

  function money(value) {
    if (value == null || value === "") { return "—"; }
    var number = Number(value);
    return isFinite(number) ? "$ " + number.toFixed(2) : "$ " + esc(value);
  }

  function dateTime(value) {
    if (!value) { return "—"; }
    var parsed = new Date(value);
    return isNaN(parsed.getTime()) ? esc(value) : parsed.toLocaleString("pt-BR");
  }

  function dateOnly(value) {
    if (!value) { return "—"; }
    var parts = String(value).slice(0, 10).split("-");
    return parts.length === 3 ? parts[2] + "/" + parts[1] + "/" + parts[0] : esc(value);
  }

  function todayIso() {
    var now = new Date();
    var local = new Date(now.getTime() - now.getTimezoneOffset() * 60000);
    return local.toISOString().slice(0, 10);
  }

  function statusLabel(value) {
    var labels = {
      active: "Ativo", paused: "Pausado", connected: "Conectada",
      failed: "Falha", disconnected: "Desconectada", paid: "Pago",
      overdue: "Em atraso", pending: "Pendente", exempt: "Isento",
      cancelled: "Cancelado"
    };
    return labels[value] || value || "Sem status";
  }

  function showError(message) {
    notice.className = "notice error";
    notice.textContent = message;
    notice.hidden = false;
  }

  function renderSummary() {
    var items = [
      ["Clientes", state.summary.users],
      ["Ativos", state.summary.active],
      ["MT5 conectadas", state.summary.connected_accounts],
      ["Precisam atenção", state.summary.attention_accounts],
      ["Sinais ativos", state.summary.active_groups],
      ["Pagos", state.summary.billing_paid],
      ["Pendentes", state.summary.billing_pending],
      ["Em atraso", state.summary.billing_overdue],
      ["Vencem em 7 dias", state.summary.billing_due_soon],
      ["Sem financeiro", state.summary.billing_unconfigured],
      ["Receita mensal", money(state.summary.monthly_total)]
    ];
    summary.innerHTML = items.map(function (item) {
      return '<article class="metric"><span>' + esc(item[0]) + '</span><strong>' + esc(item[1] || 0) + '</strong></article>';
    }).join("");
  }

  function matches(user) {
    if (state.filter === "active" && user.status !== "active") { return false; }
    if (state.filter === "paused" && user.status !== "paused") { return false; }
    if (state.filter === "attention" && user.account && user.account.connection_status === "connected" && !user.account.last_error) { return false; }
    if (state.filter === "overdue" && user.billing.effective_status !== "overdue") { return false; }
    if (state.filter === "pending" && user.billing.effective_status !== "pending") { return false; }
    if (state.filter === "due_soon") {
      if (!user.billing.due_date) { return false; }
      var due = new Date(user.billing.due_date + "T12:00:00");
      var today = new Date(todayIso() + "T12:00:00");
      var days = Math.round((due - today) / 86400000);
      if (days < 0 || days > 7) { return false; }
    }
    var account = user.account || {};
    var billing = user.billing || {};
    var haystack = [
      user.username, user.telegram_user_id, account.alias, account.masked_login,
      account.server, billing.customer_name, billing.email, billing.phone,
      billing.plan_name
    ].join(" ").toLowerCase();
    return !state.query || haystack.indexOf(state.query) !== -1;
  }

  function userCard(user) {
    var account = user.account;
    var profile = user.profile;
    var billing = user.billing || {};
    var nextStatus = user.status === "active" ? "paused" : "active";
    var actionLabel = nextStatus === "active" ? "Ativar" : "Pausar";
    var accountHtml = account
      ? '<div><div class="account-name">' + esc(account.alias) + ' · ' + esc(account.masked_login) + '</div>' +
        '<div class="detail">' + esc(account.server) + '<br><span class="status ' + esc(account.connection_status) + '">' + esc(statusLabel(account.connection_status)) + '</span>' +
        '<br>Saldo <span class="money">' + money(account.balance) + '</span> · Equity <span class="money">' + money(account.equity) + '</span></div></div>'
      : '<div><div class="account-name">Sem conta MT5</div><div class="detail">Aguardando cadastro do cliente.</div></div>';
    var profileHtml = profile
      ? '<div class="detail">Gestão: ' + esc(profile.risk_mode) + '<br>Lote: ' + esc(profile.fixed_lot || "—") +
        ' · Risco: ' + esc(profile.risk_percent || "—") + '%<br>Máx. sinais: ' + esc(profile.max_open_signals) +
        ' · Ativos: ' + esc(user.active_group_count) + '<br>Última execução: ' + dateTime(user.last_execution_at) + '</div>'
      : '<div class="detail">Perfil operacional ainda não criado.<br>Execuções registradas: ' + esc(user.execution_count) + '</div>';
    var errorHtml = account && account.last_error ? '<br><span style="color:var(--red)">' + esc(account.last_error) + '</span>' : "";
    var billingHtml =
      '<div class="detail"><span class="status ' + esc(billing.effective_status) + '">' +
      esc(statusLabel(billing.effective_status)) + '</span><br>' +
      '<span class="account-name">' + esc(billing.customer_name || "Financeiro não cadastrado") + '</span><br>' +
      esc(billing.plan_name || "Mensal") + ' · ' + money(billing.monthly_amount || "0") +
      '<br>Vencimento: ' + dateOnly(billing.due_date) +
      '<br>Último pagamento: ' + dateOnly(billing.last_paid_at) + '</div>';
    return '<article class="user-card" data-user-id="' + esc(user.id) + '">' +
      '<div class="user-main">' +
        '<div class="identity"><h2>' + esc(user.username ? "@" + user.username : "Cliente #" + user.id) + '</h2>' +
          '<div class="meta">Telegram ' + esc(user.telegram_user_id) + ' · Cadastro ' + dateTime(user.created_at) + '</div>' +
          '<span class="status ' + esc(user.status) + '">' + esc(statusLabel(user.status)) + '</span>' + errorHtml +
        '</div>' +
        accountHtml + profileHtml + billingHtml +
        '<div class="actions"><button class="action ' + (nextStatus === "active" ? "activate" : "pause") +
          '" type="button" data-action-status="' + nextStatus + '" data-user-id="' + esc(user.id) + '">' + actionLabel + '</button>' +
          '<button class="action finance" type="button" data-finance-user="' + esc(user.id) + '">Financeiro</button></div>' +
      '</div></article>';
  }

  function renderUsers() {
    var visible = state.users.filter(matches);
    list.innerHTML = visible.length ? visible.map(userCard).join("") : '<div class="empty">Nenhum cliente encontrado neste filtro.</div>';
  }

  function applyDashboard(data) {
    state.users = data.users || [];
    state.summary = data.summary || {};
    state.csrf = data.csrf_token || state.csrf;
    state.browser = !tg || !tg.initData;
    logout.hidden = !state.browser;
    notice.hidden = true;
    summary.hidden = false;
    workspace.hidden = false;
    renderSummary();
    renderUsers();
  }

  function load() {
    refresh.disabled = true;
    var token = "";
    if (window.location.hash.indexOf("#token=") === 0) {
      token = decodeURIComponent(window.location.hash.slice(7));
      window.history.replaceState(null, "", window.location.pathname + window.location.search);
    }
    var request = token
      ? post("/api/admin/browser-login", { token: token })
      : post("/api/admin/session", authFields());
    request
      .then(applyDashboard)
      .catch(function (error) {
        showError(error.message || "Abra o Painel Admin pelo botão do bot de gestão ou gere um novo link para o PC.");
      })
      .finally(function () { refresh.disabled = false; });
  }

  function authFields(extra) {
    var fields = extra || {};
    fields.init_data = tg && tg.initData ? tg.initData : "";
    return fields;
  }

  function reloadDashboard() {
    return post("/api/admin/session", authFields()).then(applyDashboard);
  }

  function changeStatus(userId, status, button) {
    if (state.busy) { return; }
    var verb = status === "active" ? "ativar" : "pausar";
    if (!window.confirm("Deseja " + verb + " este cliente?")) { return; }
    state.busy = true;
    button.disabled = true;
    post("/api/admin/user-status", authFields({
      csrf_token: state.csrf,
      user_id: userId,
      status: status
    })).then(reloadDashboard)
      .catch(function (error) { showError(error.message || "Não foi possível alterar o cliente."); })
      .finally(function () { state.busy = false; button.disabled = false; });
  }

  function userById(userId) {
    return state.users.find(function (user) { return String(user.id) === String(userId); });
  }

  function field(id, value) {
    document.getElementById(id).value = value == null ? "" : value;
  }

  function openFinance(userId) {
    var user = userById(userId);
    if (!user) { return; }
    var billing = user.billing || {};
    document.getElementById("finance-title").textContent =
      "Financeiro — " + (billing.customer_name || user.username || "Cliente #" + user.id);
    field("billing-user-id", user.id);
    field("customer-name", billing.customer_name);
    field("plan-name", billing.plan_name || "Mensal");
    field("customer-email", billing.email);
    field("customer-phone", billing.phone);
    field("monthly-amount", billing.monthly_amount || "0.00");
    field("due-date", billing.due_date);
    field("billing-status", billing.status || "pending");
    field("billing-notes", billing.notes);
    field("payment-amount", billing.monthly_amount !== "0" ? billing.monthly_amount : "");
    field("paid-at", todayIso());
    field("payment-method", "");
    field("payment-reference", "");
    field("next-due-date", "");
    var payments = billing.payments || [];
    document.getElementById("payment-history").innerHTML = payments.length
      ? payments.map(function (payment) {
          return '<div class="payment-row"><span>' + dateOnly(payment.paid_at) +
            ' · ' + esc(payment.method || "Forma não informada") + '</span><strong>' +
            money(payment.amount) + '</strong></div>';
        }).join("")
      : '<div class="empty">Nenhum pagamento registrado.</div>';
    dialog.showModal();
  }

  function billingFields() {
    return authFields({
      csrf_token: state.csrf,
      user_id: document.getElementById("billing-user-id").value,
      customer_name: document.getElementById("customer-name").value,
      email: document.getElementById("customer-email").value,
      phone: document.getElementById("customer-phone").value,
      plan_name: document.getElementById("plan-name").value,
      monthly_amount: document.getElementById("monthly-amount").value,
      due_date: document.getElementById("due-date").value,
      billing_status: document.getElementById("billing-status").value,
      notes: document.getElementById("billing-notes").value
    });
  }

  function paymentFields() {
    return authFields({
      csrf_token: state.csrf,
      user_id: document.getElementById("billing-user-id").value,
      amount: document.getElementById("payment-amount").value,
      paid_at: document.getElementById("paid-at").value,
      method: document.getElementById("payment-method").value,
      reference: document.getElementById("payment-reference").value,
      next_due_date: document.getElementById("next-due-date").value
    });
  }

  function submitFinance(path, fields, form) {
    if (state.busy) { return; }
    state.busy = true;
    var button = form.querySelector("button[type=submit]");
    button.disabled = true;
    post(path, fields).then(reloadDashboard).then(function () {
      dialog.close();
    }).catch(function (error) {
      window.alert(error.message || "Não foi possível salvar.");
    }).finally(function () {
      state.busy = false;
      button.disabled = false;
    });
  }

  search.addEventListener("input", function () {
    state.query = search.value.trim().toLowerCase();
    renderUsers();
  });
  filters.addEventListener("click", function (event) {
    var button = event.target.closest("[data-filter]");
    if (!button) { return; }
    state.filter = button.getAttribute("data-filter");
    Array.prototype.forEach.call(filters.querySelectorAll(".filter"), function (item) {
      item.classList.toggle("active", item === button);
    });
    renderUsers();
  });
  list.addEventListener("click", function (event) {
    var button = event.target.closest("[data-action-status]");
    if (button) {
      changeStatus(button.getAttribute("data-user-id"), button.getAttribute("data-action-status"), button);
      return;
    }
    var finance = event.target.closest("[data-finance-user]");
    if (finance) { openFinance(finance.getAttribute("data-finance-user")); }
  });
  refresh.addEventListener("click", load);
  document.getElementById("finance-close").addEventListener("click", function () { dialog.close(); });
  billingForm.addEventListener("submit", function (event) {
    event.preventDefault();
    submitFinance("/api/admin/billing-update", billingFields(), billingForm);
  });
  paymentForm.addEventListener("submit", function (event) {
    event.preventDefault();
    submitFinance("/api/admin/payment", paymentFields(), paymentForm);
  });
  logout.addEventListener("click", function () {
    post("/api/admin/logout", authFields({ csrf_token: state.csrf }))
      .then(function () {
        state.users = [];
        summary.hidden = true;
        workspace.hidden = true;
        logout.hidden = true;
        showError("Sessão encerrada. Gere um novo link pelo bot para entrar novamente.");
      })
      .catch(function (error) { showError(error.message || "Não foi possível encerrar a sessão."); });
  });

  if (tg) {
    try { tg.ready(); tg.expand(); } catch (_error) {}
  }
  load();
}());
""".lstrip()
