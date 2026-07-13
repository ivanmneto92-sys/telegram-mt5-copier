from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .database import connect_database, initialize_database, utc_now

RISK_MODES = {"fixed_lot", "risk_percent"}
TP_DISTRIBUTION_MODES = {"all", "split", "first"}
MIN_FIXED_LOT = Decimal("0.01")
MAX_FIXED_LOT = Decimal("100")
MIN_RISK_PERCENT = Decimal("0.1")
MAX_RISK_PERCENT = Decimal("10")
MAX_MONEY_LIMIT = Decimal("1000000")
MIN_OPEN_TRADES = 1
MAX_OPEN_TRADES = 100


@dataclass(frozen=True)
class UserSettings:
    user_id: int
    risk_mode: str
    fixed_lot: Decimal
    risk_percent: Decimal
    daily_profit_target: Decimal
    daily_loss_limit: Decimal
    max_open_trades: int
    tp_distribution_mode: str
    breakeven_enabled: bool
    trailing_enabled: bool


class SettingsService:
    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path
        self._closed = False
        initialize_database(database_path)

    def close(self) -> None:
        self._closed = True

    def __enter__(self) -> "SettingsService":
        return self

    def __exit__(self, _exc_type: object, _exc: object, _traceback: object) -> None:
        self.close()

    def ensure_defaults(self, user_id: int) -> UserSettings:
        existing = self.get_settings(user_id)
        if existing is not None:
            return existing

        with connect_database(self.database_path) as connection:
            cursor = connection.execute(
                """
                INSERT INTO user_settings (
                    user_id,
                    risk_mode,
                    fixed_lot,
                    risk_percent,
                    daily_profit_target,
                    daily_loss_limit,
                    max_open_trades,
                    tp_distribution_mode,
                    breakeven_enabled,
                    trailing_enabled,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    "fixed_lot",
                    "0.01",
                    "1",
                    "0",
                    "0",
                    1,
                    "all",
                    0,
                    0,
                    utc_now(),
                ),
            )
            cursor.close()

        return self.get_settings(user_id)

    def get_settings(self, user_id: int) -> UserSettings | None:
        with connect_database(self.database_path) as connection:
            cursor = connection.execute(
                """
                SELECT
                    user_id,
                    risk_mode,
                    fixed_lot,
                    risk_percent,
                    daily_profit_target,
                    daily_loss_limit,
                    max_open_trades,
                    tp_distribution_mode,
                    breakeven_enabled,
                    trailing_enabled
                FROM user_settings
                WHERE user_id = ?
                """,
                (user_id,),
            )
            try:
                row = cursor.fetchone()
            finally:
                cursor.close()

        return settings_from_row(row) if row else None

    def update_fixed_lot(self, user_id: int, fixed_lot: str | Decimal) -> UserSettings:
        value = parse_decimal(fixed_lot)
        if value < MIN_FIXED_LOT or value > MAX_FIXED_LOT:
            raise ValueError("Lote fixo fora do intervalo permitido.")
        return self._update_field(user_id, "fixed_lot", decimal_to_storage(value))

    def update_risk_mode(self, user_id: int, risk_mode: str) -> UserSettings:
        if risk_mode not in RISK_MODES:
            raise ValueError("Modo de lote invalido.")
        return self._update_field(user_id, "risk_mode", risk_mode)

    def update_risk_percent(self, user_id: int, risk_percent: str | Decimal) -> UserSettings:
        value = parse_decimal(risk_percent)
        if value < MIN_RISK_PERCENT or value > MAX_RISK_PERCENT:
            raise ValueError("Risco percentual fora do intervalo permitido.")
        return self._update_field(user_id, "risk_percent", decimal_to_storage(value))

    def update_daily_profit_target(self, user_id: int, value: str | Decimal) -> UserSettings:
        decimal_value = parse_non_negative_money(value)
        return self._update_field(user_id, "daily_profit_target", decimal_to_storage(decimal_value))

    def update_daily_loss_limit(self, user_id: int, value: str | Decimal) -> UserSettings:
        decimal_value = parse_non_negative_money(value)
        return self._update_field(user_id, "daily_loss_limit", decimal_to_storage(decimal_value))

    def update_max_open_trades(self, user_id: int, value: int) -> UserSettings:
        if value < MIN_OPEN_TRADES or value > MAX_OPEN_TRADES:
            raise ValueError("Maximo de operacoes fora do intervalo permitido.")
        return self._update_field(user_id, "max_open_trades", value)

    def update_tp_distribution_mode(self, user_id: int, mode: str) -> UserSettings:
        if mode not in TP_DISTRIBUTION_MODES:
            raise ValueError("Modo de TPs invalido.")
        return self._update_field(user_id, "tp_distribution_mode", mode)

    def update_breakeven_enabled(self, user_id: int, enabled: bool) -> UserSettings:
        return self._update_field(user_id, "breakeven_enabled", int(enabled))

    def update_trailing_enabled(self, user_id: int, enabled: bool) -> UserSettings:
        return self._update_field(user_id, "trailing_enabled", int(enabled))

    def _update_field(self, user_id: int, field_name: str, value: str | int) -> UserSettings:
        allowed_fields = {
            "risk_mode",
            "fixed_lot",
            "risk_percent",
            "daily_profit_target",
            "daily_loss_limit",
            "max_open_trades",
            "tp_distribution_mode",
            "breakeven_enabled",
            "trailing_enabled",
        }
        if field_name not in allowed_fields:
            raise ValueError("Campo de configuracao invalido.")

        self.ensure_defaults(user_id)
        with connect_database(self.database_path) as connection:
            cursor = connection.execute(
                f"UPDATE user_settings SET {field_name} = ?, updated_at = ? WHERE user_id = ?",
                (value, utc_now(), user_id),
            )
            cursor.close()

        return self.get_settings(user_id)


def settings_from_row(row: tuple[object, ...]) -> UserSettings:
    return UserSettings(
        user_id=int(row[0]),
        risk_mode=str(row[1]),
        fixed_lot=Decimal(str(row[2])),
        risk_percent=Decimal(str(row[3])),
        daily_profit_target=Decimal(str(row[4])),
        daily_loss_limit=Decimal(str(row[5])),
        max_open_trades=int(row[6]),
        tp_distribution_mode=str(row[7]),
        breakeven_enabled=bool(row[8]),
        trailing_enabled=bool(row[9]),
    )


def parse_decimal(value: str | Decimal) -> Decimal:
    try:
        decimal_value = value if isinstance(value, Decimal) else Decimal(str(value).replace(",", "."))
    except InvalidOperation as exc:
        raise ValueError("Valor numerico invalido.") from exc
    return decimal_value


def parse_non_negative_money(value: str | Decimal) -> Decimal:
    decimal_value = parse_decimal(value)
    if decimal_value < 0 or decimal_value > MAX_MONEY_LIMIT:
        raise ValueError("Valor monetario fora do intervalo permitido.")
    return decimal_value


def decimal_to_storage(value: Decimal) -> str:
    normalized = value.normalize()
    if normalized == normalized.to_integral():
        return str(normalized.quantize(Decimal("1")))
    return format(normalized, "f")
