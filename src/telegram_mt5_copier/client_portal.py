from __future__ import annotations

from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Mapping

from .channel_catalog import ChannelCatalogService
from .client_auth import normalize_email, validate_customer_name, validate_phone
from .database import connect_database, initialize_database, utc_now
from .mt5.account_service import MT5AccountForm, MT5AccountService
from .mt5.pending_order_executor import rejection_reason_label
from .settings_service import SettingsService
from .users import USER_STATUS_ACTIVE, USER_STATUS_PAUSED, UserRepository
from .web_app import WebAppValidationError, validate_broker_name, validate_server_name


class AccountNotFoundError(ValueError):
    """A conta MT5 pedida nao existe ou nao pertence ao cliente autenticado."""


_ACCOUNT_COLUMNS = """
    id, account_alias, broker_name, server_name, login, account_type,
    connection_status, balance, equity, worker_heartbeat_at, last_error
"""


class ClientPortalService:
    """Dados e alterações do portal, sempre limitados ao user_id autenticado."""

    def __init__(
        self,
        database_path: Path,
        *,
        brand_name: str,
        mt5_accounts: MT5AccountService | None = None,
        broker_servers: Mapping[str, tuple[str, ...]] | None = None,
        market_news_enabled: bool = False,
        market_news_minutes_before: int = 0,
        market_news_minutes_after: int = 0,
    ) -> None:
        self.database_path = database_path
        self.brand_name = brand_name
        self.mt5_accounts = mt5_accounts
        self.market_news_enabled = market_news_enabled
        self.market_news_minutes_before = market_news_minutes_before
        self.market_news_minutes_after = market_news_minutes_after
        # Cadastro de conta pelo site reaproveita a mesma validacao de corretora/
        # servidor do fluxo do bot no Telegram (web_app.py), incluindo o mesmo
        # catalogo: sem ele (broker_servers=None), qualquer corretora/servidor
        # digitado e aceito, exatamente como no onboarding do bot.
        self._broker_catalog = dict(broker_servers or {})
        self._enforce_broker_catalog = broker_servers is not None
        self._broker_names_by_key = {
            name.strip().casefold(): name for name in self._broker_catalog
        }
        self._broker_servers_by_key = {
            name.strip().casefold(): tuple(servers)
            for name, servers in self._broker_catalog.items()
        }
        initialize_database(database_path)
        self.channels_catalog = ChannelCatalogService(database_path)
        self.users = UserRepository(database_path)
        self.settings = SettingsService(database_path)

    def broker_catalog(self) -> dict[str, object]:
        return {
            "brokers": [
                {"name": name, "servers": list(servers)}
                for name, servers in self._broker_catalog.items()
            ]
        }

    def add_account(
        self,
        user_id: int,
        *,
        broker_name: str,
        server_name: str,
        custom_server_name: str = "",
        login: str,
        password: str,
        account_alias: str,
    ) -> dict[str, object]:
        if self.mt5_accounts is None:
            raise ValueError("Cadastro de conta MT5 indisponivel nesta instancia.")
        try:
            broker_name = validate_broker_name(
                broker_name, self._broker_names_by_key, enforce=self._enforce_broker_catalog
            )
            server_name = validate_server_name(
                broker_name,
                server_name,
                custom_server_name,
                self._broker_servers_by_key,
                enforce=self._enforce_broker_catalog,
            )
        except WebAppValidationError as exc:
            # Vira ValueError comum para cair no tratamento generico de erro de
            # validacao do portal (HTTP 400 com esta mensagem), em vez do
            # tratamento especifico de sessao do Telegram.
            raise ValueError(str(exc)) from exc
        form = MT5AccountForm(
            broker_name=broker_name,
            server_name=server_name,
            login=login,
            password=password,
            account_alias=account_alias,
        )
        account = self.mt5_accounts.register_account(
            user_id, form, keep_on_connection_failure=True
        )
        with connect_database(self.database_path) as db:
            row = self._select_account(db, user_id, account.id)
        return {"account": self._account(row)}

    def remove_account(self, user_id: int, account_id: int) -> dict[str, object]:
        """Remove a conta e devolve os dados dela (para o aviso por e-mail)."""
        if self.mt5_accounts is None:
            raise ValueError("Remocao de conta MT5 indisponivel nesta instancia.")
        with connect_database(self.database_path) as db:
            row = self._select_account(db, user_id, account_id)  # levanta AccountNotFoundError
        removed = self._account(row)
        self.mt5_accounts.remove_account(user_id, account_id)
        return {"account": removed}

    @staticmethod
    def _select_account(db: object, user_id: int, account_id: int | None) -> object | None:
        """Conta escolhida pelo cliente ou, sem escolha, a principal (conectada primeiro).

        Uma conta de outro cliente e tratada como inexistente, nunca como acesso negado,
        para nao revelar quais IDs existem.
        """
        if account_id is not None:
            row = db.execute(
                f"SELECT {_ACCOUNT_COLUMNS} FROM mt5_accounts WHERE id = ? AND user_id = ?",
                (account_id, user_id),
            ).fetchone()
            if row is None:
                raise AccountNotFoundError("Conta MT5 nao encontrada.")
            return row
        return db.execute(
            f"""
            SELECT {_ACCOUNT_COLUMNS} FROM mt5_accounts WHERE user_id = ?
            ORDER BY CASE connection_status WHEN 'connected' THEN 0 ELSE 1 END, id DESC LIMIT 1
            """,
            (user_id,),
        ).fetchone()

    def accounts(self, user_id: int) -> dict[str, object]:
        with connect_database(self.database_path) as db:
            rows = db.execute(
                f"""
                SELECT {_ACCOUNT_COLUMNS} FROM mt5_accounts WHERE user_id = ?
                ORDER BY CASE connection_status WHEN 'connected' THEN 0 ELSE 1 END, id DESC
                """,
                (user_id,),
            ).fetchall()
        return {"accounts": [self._account(row) for row in rows]}

    def dashboard(self, user_id: int, account_id: int | None = None) -> dict[str, object]:
        with connect_database(self.database_path) as db:
            user = db.execute(
                "SELECT telegram_username, status, daily_signal_pause_until FROM users WHERE id = ?",
                (user_id,),
            ).fetchone()
            if user is None:
                raise ValueError("Cliente nao encontrado.")
            account = self._select_account(db, user_id, account_id)
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
            # Sem conta escolhida, conta as operacoes de todas as contas do cliente.
            active_count = db.execute(
                """
                SELECT COUNT(*) FROM execution_groups
                WHERE user_id = ? AND status IN ('pending_active', 'filled', 'open')
                  AND (? IS NULL OR mt5_account_id = ?)
                """,
                (user_id, account_id, account_id),
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

    def toggle_channel(self, user_id: int, channel_id: int) -> dict[str, object]:
        """Liga/desliga um canal aprovado pro cliente autenticado.

        Reaproveita ChannelCatalogService.toggle_subscription -- a mesma
        logica que o bot do Telegram usa pelo botao inline -- entao um canal
        novo continua nunca sendo seguido automaticamente por quem ja tinha
        selecao "todos" (ver _freeze_follow_all_modes) e a troca aqui vira
        selecao "custom" do mesmo jeito que trocaria vindo do bot.
        """
        enabled = self.channels_catalog.toggle_subscription(user_id, channel_id)
        return {"channel_id": channel_id, "enabled": enabled}

    def toggle_copier_pause(self, user_id: int) -> dict[str, object]:
        """Pausa/reativa o copiador pro cliente autenticado.

        Reaproveita UserRepository.set_status -- o mesmo campo que o bot usa
        no fluxo "Pausar novas entradas" e que o admin usa pra ativar/pausar
        pelo painel -- entao os tres lugares sempre leem o mesmo estado.
        Reativar aqui nunca libera sinais por si so: a execucao ao vivo exige
        billing em dia de forma independente (accounts_for_approved_users),
        entao um cliente pausado por falta de pagamento nao ganha acesso so
        por reativar o proprio status.
        """
        current = self.users.get_by_id(user_id)
        next_status = (
            USER_STATUS_ACTIVE if current.status == USER_STATUS_PAUSED else USER_STATUS_PAUSED
        )
        updated = self.users.set_status(user_id, next_status)
        return {"status": updated.status}

    def news_preference(self, user_id: int) -> dict[str, object]:
        settings = self.settings.ensure_defaults(user_id)
        return {
            "avoid_high_impact_news": settings.avoid_high_impact_news,
            "market_news_available": self.market_news_enabled,
            "minutes_before": self.market_news_minutes_before,
            "minutes_after": self.market_news_minutes_after,
        }

    def set_news_preference(self, user_id: int, avoid_high_impact_news: bool) -> dict[str, object]:
        """Liga/desliga o bloqueio de novas entradas em noticias fortes.

        Reaproveita SettingsService.update_avoid_high_impact_news -- o mesmo
        metodo que o bot usa no menu "Noticias do mercado" -- e o campo e
        lido direto pelo MarketNewsService na execucao real, entao a troca
        aqui tem efeito imediato nos dois canais (bot e portal).
        """
        self.settings.update_avoid_high_impact_news(user_id, avoid_high_impact_news)
        return self.news_preference(user_id)

    def operations(
        self, user_id: int, *, limit: int = 100, account_id: int | None = None
    ) -> dict[str, object]:
        safe_limit = max(1, min(limit, 200))
        with connect_database(self.database_path) as db:
            if account_id is not None:
                self._select_account(db, user_id, account_id)
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
                WHERE g.user_id = ? AND (? IS NULL OR g.mt5_account_id = ?)
                GROUP BY g.id
                ORDER BY g.id DESC LIMIT ?
                """,
                (user_id, account_id, account_id, safe_limit),
            ).fetchall()
        return {
            "operations": [
                {
                    "id": int(r[0]), "status": r[1], "symbol": r[2], "direction": r[3],
                    "order_type": r[4], "entry_price": r[5], "stop_loss": r[6],
                    "total_volume": r[7], "created_at": r[8], "error_code": r[9],
                    "reason_label": rejection_reason_label(r[9]) if r[9] else None,
                    "channel_name": r[10] or "Canal nao identificado", "order_count": int(r[11]),
                    "net_profit": str(r[12]),
                }
                for r in rows
            ]
        }

    def profile(self, user_id: int) -> dict[str, object]:
        with connect_database(self.database_path) as db:
            row = db.execute(
                """
                SELECT u.telegram_username, u.status, b.customer_name, b.email, b.phone,
                       c.email_confirmed_at
                FROM users u
                LEFT JOIN customer_billing b ON b.user_id = u.id
                LEFT JOIN client_credentials c ON c.user_id = u.id
                WHERE u.id = ?
                """,
                (user_id,),
            ).fetchone()
        if row is None:
            raise ValueError("Cliente nao encontrado.")
        return {
            "profile": {
                "username": row[0],
                "status": row[1],
                "customer_name": row[2],
                "email": row[3],
                "phone": row[4],
                "email_confirmed": row[5] is not None,
            }
        }

    def update_profile(
        self,
        user_id: int,
        *,
        customer_name: str,
        email: str,
        phone: str,
    ) -> dict[str, object]:
        clean_name = validate_customer_name(customer_name)
        clean_email = normalize_email(email)
        clean_phone = validate_phone(phone)
        now = utc_now()
        with connect_database(self.database_path) as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM users WHERE id = ?", (user_id,)).fetchone() is None:
                raise ValueError("Cliente nao encontrado.")
            owner = db.execute(
                """
                SELECT user_id FROM client_credentials WHERE email = ? COLLATE NOCASE
                UNION ALL
                SELECT user_id FROM customer_billing WHERE email = ? COLLATE NOCASE
                LIMIT 1
                """,
                (clean_email, clean_email),
            ).fetchone()
            if owner is not None and int(owner[0]) != user_id:
                raise ValueError("Este e-mail já está em uso.")
            db.execute(
                """
                INSERT INTO customer_billing (
                    user_id, customer_name, email, phone, plan_name, monthly_amount,
                    due_date, billing_status, last_paid_at, notes, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'Mensal', '0', NULL, 'pending', NULL, NULL, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    customer_name = excluded.customer_name,
                    email = excluded.email,
                    phone = excluded.phone,
                    updated_at = excluded.updated_at
                """,
                (user_id, clean_name, clean_email, clean_phone, now, now),
            )
            db.execute(
                """
                UPDATE client_credentials
                SET email = ?,
                    email_confirmed_at = CASE
                        WHEN email = ? COLLATE NOCASE THEN email_confirmed_at ELSE NULL
                    END,
                    updated_at = ?
                WHERE user_id = ?
                """,
                (clean_email, clean_email, now, user_id),
            )
        return self.profile(user_id)

    def financial(self, user_id: int) -> dict[str, object]:
        with connect_database(self.database_path) as db:
            billing = db.execute(
                """
                SELECT plan_name, monthly_amount, due_date, billing_status, last_paid_at
                FROM customer_billing WHERE user_id = ?
                """,
                (user_id,),
            ).fetchone()
            if db.execute("SELECT 1 FROM users WHERE id = ?", (user_id,)).fetchone() is None:
                raise ValueError("Cliente nao encontrado.")
            payments = db.execute(
                """
                SELECT id, amount, paid_at, period_start, period_end, method, status
                FROM customer_payments WHERE user_id = ?
                ORDER BY paid_at DESC, id DESC LIMIT 20
                """,
                (user_id,),
            ).fetchall()
        return {
            "billing": None if billing is None else {
                "plan_name": billing[0],
                "monthly_amount": billing[1],
                "due_date": billing[2],
                "status": billing[3],
                "last_paid_at": billing[4],
            },
            "payments": [
                {
                    "id": int(row[0]),
                    "amount": row[1],
                    "paid_at": row[2],
                    "period_start": row[3],
                    "period_end": row[4],
                    "method": row[5],
                    "status": row[6],
                }
                for row in payments
            ],
        }

    def risk(self, user_id: int, account_id: int | None = None) -> dict[str, object]:
        with connect_database(self.database_path) as db:
            account = self._select_account(db, user_id, account_id)
            if account is None:
                return {"account": None, "risk": None}
            row = db.execute(
                """
                SELECT enabled, risk_mode, fixed_lot, risk_percent, daily_profit_target,
                       daily_loss_limit, max_open_signals, split_tps, breakeven_enabled,
                       trailing_enabled, take_profit_limit, tp1_breakeven_enabled, updated_at
                FROM execution_profiles WHERE user_id = ? AND mt5_account_id = ?
                """,
                (user_id, int(account[0])),
            ).fetchone()
        return {
            "account": {
                "id": int(account[0]),
                "alias": account[1],
                "masked_login": f"••••{str(account[4])[-4:]}",
            },
            "risk": self._risk(row),
        }

    def update_risk(
        self, user_id: int, fields: dict[str, str], account_id: int | None = None
    ) -> dict[str, object]:
        current = self.risk(user_id, account_id)
        account = current["account"]
        if not isinstance(account, dict):
            raise ValueError("Cadastre uma conta MT5 antes de configurar o risco.")
        account_id = int(account["id"])
        allowed = {
            "risk_mode", "fixed_lot", "risk_percent", "daily_profit_target",
            "daily_loss_limit", "max_open_signals", "split_tps", "breakeven_enabled",
            "trailing_enabled", "take_profit_limit", "tp1_breakeven_enabled",
        }
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError("Campo de risco invalido.")
        if not fields:
            raise ValueError("Informe ao menos uma configuracao de risco.")
        values = {key: self._validate_risk_value(key, value) for key, value in fields.items()}
        assignments = ", ".join(f"{key} = ?" for key in values)
        with connect_database(self.database_path) as db:
            db.execute("BEGIN IMMEDIATE")
            exists = db.execute(
                "SELECT 1 FROM execution_profiles WHERE user_id = ? AND mt5_account_id = ?",
                (user_id, account_id),
            ).fetchone()
            if exists is None:
                raise ValueError("Perfil de execucao nao encontrado.")
            db.execute(
                f"UPDATE execution_profiles SET {assignments}, updated_at = ? WHERE user_id = ? AND mt5_account_id = ?",
                (*values.values(), utc_now(), user_id, account_id),
            )
        return self.risk(user_id, account_id)

    @staticmethod
    def _validate_risk_value(field: str, raw: str) -> str | int:
        if field == "risk_mode":
            if raw not in {"fixed_lot", "risk_percent"}:
                raise ValueError("Modo de risco invalido.")
            return raw
        if field in {"split_tps", "breakeven_enabled", "trailing_enabled", "tp1_breakeven_enabled"}:
            if raw not in {"0", "1", "false", "true"}:
                raise ValueError("Valor booleano invalido.")
            return int(raw in {"1", "true"})
        if field in {"max_open_signals", "take_profit_limit"}:
            try:
                value = int(raw)
            except ValueError as exc:
                raise ValueError("Valor inteiro invalido.") from exc
            if field == "max_open_signals" and not 1 <= value <= 100:
                raise ValueError("Maximo de sinais deve ficar entre 1 e 100.")
            if field == "take_profit_limit" and not 0 <= value <= 10:
                raise ValueError("Quantidade de Take Profits deve ficar entre 0 e 10.")
            return value
        try:
            value = Decimal(raw.replace(",", "."))
        except (InvalidOperation, AttributeError) as exc:
            raise ValueError("Valor decimal invalido.") from exc
        if not value.is_finite():
            raise ValueError("Valor decimal invalido.")
        if field == "fixed_lot" and not Decimal("0") < value <= Decimal("100"):
            raise ValueError("Lote fixo deve ficar entre 0 e 100.")
        if field == "risk_percent" and not Decimal("0") < value <= Decimal("100"):
            raise ValueError("Risco percentual deve ficar entre 0 e 100.")
        if field in {"daily_profit_target", "daily_loss_limit"} and value < 0:
            raise ValueError("Limite financeiro nao pode ser negativo.")
        return format(value, "f")

    @staticmethod
    def _risk(row: object) -> dict[str, object] | None:
        if row is None:
            return None
        return {
            "enabled": bool(row[0]), "risk_mode": row[1], "fixed_lot": row[2],
            "risk_percent": row[3], "daily_profit_target": row[4],
            "daily_loss_limit": row[5], "max_open_signals": int(row[6]),
            "split_tps": bool(row[7]), "breakeven_enabled": bool(row[8]),
            "trailing_enabled": bool(row[9]), "take_profit_limit": int(row[10]),
            "tp1_breakeven_enabled": bool(row[11]), "updated_at": row[12],
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
