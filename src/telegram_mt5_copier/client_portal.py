from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Mapping

from .access_control import paid_access_decision
from .channel_catalog import ChannelCatalogService
from .daily_schedule import next_daily_signal_resume_at
from .client_auth import normalize_email, validate_customer_name, validate_phone
from .database import connect_database, initialize_database, utc_now
from .mt5.account_service import MT5AccountForm, MT5AccountService
from .mt5.models import PRODUCT_KIND_BROKER_COPY, PRODUCT_KIND_SIGNAL_COPIER
from .mt5.pending_order_executor import rejection_reason_label
from .mt5.settlement_monitor import get_result_mode, set_result_mode
from .settings_service import SettingsService
from .users import USER_STATUS_ACTIVE, USER_STATUS_PAUSED, UserRepository
from .web_app import WebAppValidationError, validate_broker_name, validate_server_name


class AccountNotFoundError(ValueError):
    """A conta MT5 pedida nao existe ou nao pertence ao cliente autenticado."""


_ACCOUNT_COLUMNS = """
    id, account_alias, broker_name, server_name, login, account_type,
    connection_status, balance, equity, worker_heartbeat_at, last_error, product_kind
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
        vapid_public_key: str | None = None,
    ) -> None:
        self.database_path = database_path
        self.brand_name = brand_name
        self.mt5_accounts = mt5_accounts
        self.market_news_enabled = market_news_enabled
        self.market_news_minutes_before = market_news_minutes_before
        self.market_news_minutes_after = market_news_minutes_after
        self.vapid_public_key = vapid_public_key
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
        product_kind: str = PRODUCT_KIND_SIGNAL_COPIER,
    ) -> dict[str, object]:
        if product_kind not in {PRODUCT_KIND_SIGNAL_COPIER, PRODUCT_KIND_BROKER_COPY}:
            raise ValueError("Escolha o tipo da conta: Sistema Automatico ou Copy Trader.")
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
            product_kind=product_kind,
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

    def test_mt5_connection(self, user_id: int, account_id: int) -> dict[str, object]:
        """Testa a conexao com o terminal MT5 sob demanda, reaproveitando
        MT5AccountService.test_connection -- o mesmo usado pelo bot no botao
        "Testar conexao". Checa posse primeiro (levanta AccountNotFoundError,
        mesmo padrao de remove_account) -- MT5AccountService.get_account
        levantaria so um ValueError generico, que nao deve vazar como 404."""
        if self.mt5_accounts is None:
            raise ValueError("Teste de conexão MT5 indisponível nesta instância.")
        with connect_database(self.database_path) as db:
            self._select_account(db, user_id, account_id)  # levanta AccountNotFoundError
        updated = self.mt5_accounts.test_connection(user_id, account_id, startup_retry=True)
        with connect_database(self.database_path) as db:
            row = self._select_account(db, user_id, updated.id)
        return {"account": self._account(row)}

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
            floating_total = Decimal("0")
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
                # Lucro/prejuizo flutuante de posicoes ainda abertas nessa conta
                # -- account_daily_performance so cobre negocios ja fechados
                # (history_deals_get); somamos aqui pro "resultado do dia"
                # refletir a posicao de verdade, nao so o que ja fechou hoje.
                floating_row = db.execute(
                    """
                    SELECT COALESCE(SUM(CAST(o.floating_profit AS REAL)), 0)
                    FROM execution_orders o
                    JOIN execution_groups g ON g.id = o.execution_group_id
                    WHERE g.mt5_account_id = ? AND o.floating_profit IS NOT NULL
                    """,
                    (int(account[0]),),
                ).fetchone()
                floating_total = Decimal(str(floating_row[0] or 0))
                # Entradas manuais no MT5 (retrato recente do worker) tambem
                # contam no resultado do dia.
                manual_row = db.execute(
                    """
                    SELECT COALESCE(SUM(CAST(profit AS REAL)), 0) FROM mt5_open_positions
                    WHERE mt5_account_id = ?
                      AND datetime(updated_at) >= datetime('now', '-3 minutes')
                    """,
                    (int(account[0]),),
                ).fetchone()
                floating_total += Decimal(str(manual_row[0] or 0))
            # Sem conta escolhida, conta as operacoes de todas as contas do cliente.
            active_count = db.execute(
                """
                SELECT COUNT(*) FROM execution_groups
                WHERE user_id = ? AND status IN ('pending_active', 'filled', 'open')
                  AND (? IS NULL OR mt5_account_id = ?)
                """,
                (user_id, account_id, account_id),
            ).fetchone()[0]
            # Posicoes abertas manualmente tambem sao operacoes ativas.
            active_count += db.execute(
                """
                SELECT COUNT(*) FROM mt5_open_positions p
                JOIN mt5_accounts a ON a.id = p.mt5_account_id
                WHERE a.user_id = ? AND (? IS NULL OR a.id = ?)
                  AND datetime(p.updated_at) >= datetime('now', '-3 minutes')
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
            "daily_performance": self._performance(
                performance, floating_total, divisor=_currency_divisor(account)
            ),
            "active_operations": int(active_count),
        }

    def performance_calendar(
        self, user_id: int, *, month: str, account_id: int | None = None
    ) -> dict[str, object]:
        """Resultado liquido por dia num mes (`month` no formato "YYYY-MM"),
        pra alimentar o calendario de historico do portal. Cobre qualquer
        conta MT5 ativa, Sistema Automatico ou Copy Trader -- os dois tipos
        alimentam account_daily_performance a partir do historico de deals
        real da corretora, sem depender de sinal disparado por este sistema.
        """
        try:
            year_text, month_text = month.split("-", 1)
            year, month_number = int(year_text), int(month_text)
            if not 1 <= month_number <= 12:
                raise ValueError
        except ValueError as exc:
            raise ValueError("Mes invalido. Use o formato AAAA-MM.") from exc
        start = f"{year:04d}-{month_number:02d}-01"
        if month_number == 12:
            end = f"{year + 1:04d}-01-01"
        else:
            end = f"{year:04d}-{month_number + 1:02d}-01"

        with connect_database(self.database_path) as db:
            account = self._select_account(db, user_id, account_id)
            if account is None:
                return {"month": month, "days": []}
            divisor = _currency_divisor(account)
            rows = db.execute(
                """
                SELECT performance_date, realized_profit, return_percent
                FROM account_daily_performance
                WHERE mt5_account_id = ? AND performance_date >= ? AND performance_date < ?
                ORDER BY performance_date ASC
                """,
                (int(account[0]), start, end),
            ).fetchall()
        return {
            "month": month,
            "days": [
                {
                    "date": str(row[0]),
                    "net_profit": _scaled(row[1], divisor),
                    "return_percent": str(row[2]) if row[2] is not None else None,
                }
                for row in rows
            ],
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

    def set_channel_mode(self, user_id: int, mode: str) -> dict[str, object]:
        """Muda entre "seguir todos os canais aprovados" e "escolher manualmente",
        reaproveitando ChannelCatalogService.set_selection_mode -- o mesmo
        usado pelo bot no menu de canais."""
        self.channels_catalog.set_selection_mode(user_id, mode)
        return self.channels(user_id)

    def suggest_channel(self, user_id: int, raw_link: str) -> dict[str, object]:
        """Sugere um canal novo pro catalogo, reaproveitando
        ChannelCatalogService.submit_request -- a mesma logica do bot no
        fluxo "Sugerir canal" (link publico/privado/@username)."""
        result = self.channels_catalog.submit_request(user_id, raw_link)
        return {
            "status": result.status,
            "canonical_link": result.canonical_link,
            "title": result.title,
        }

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

    def daily_stop_status(self, user_id: int) -> dict[str, object]:
        user = self.users.get_by_id(user_id)
        return self._daily_stop_payload(user.daily_signal_pause_until)

    def stop_signals_today(self, user_id: int) -> dict[str, object]:
        """Para novas entradas so ate a retomada automatica (23h/dia util),
        reaproveitando UserRepository.set_daily_signal_pause_until e
        next_daily_signal_resume_at -- os mesmos usados pelo bot no menu
        "Parar sinais hoje". Operacoes/ordens ja existentes nao sao afetadas.
        """
        user = self.users.get_by_id(user_id)
        if user.status != USER_STATUS_ACTIVE or not paid_access_decision(
            self.database_path, user_id
        ).allowed:
            raise ValueError("Não há novas entradas liberadas para interromper neste momento.")
        resume_at = next_daily_signal_resume_at()
        updated = self.users.set_daily_signal_pause_until(user_id, resume_at.isoformat())
        return self._daily_stop_payload(updated.daily_signal_pause_until)

    def resume_signals_today(self, user_id: int) -> dict[str, object]:
        updated = self.users.set_daily_signal_pause_until(user_id, None)
        return self._daily_stop_payload(updated.daily_signal_pause_until)

    def toggle_daily_stop(self, user_id: int) -> dict[str, object]:
        current = self.daily_stop_status(user_id)
        if current["daily_signal_pause_active"]:
            return self.resume_signals_today(user_id)
        return self.stop_signals_today(user_id)

    @staticmethod
    def _daily_stop_payload(pause_until: str | None) -> dict[str, object]:
        active = False
        if pause_until:
            try:
                parsed = datetime.fromisoformat(pause_until)
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                active = parsed.astimezone(timezone.utc) > datetime.now(tz=timezone.utc)
            except ValueError:
                active = False
        return {
            "daily_signal_pause_until": pause_until if active else None,
            "daily_signal_pause_active": active,
        }

    def news_preference(self, user_id: int) -> dict[str, object]:
        settings = self.settings.ensure_defaults(user_id)
        return {
            "avoid_high_impact_news": settings.avoid_high_impact_news,
            "market_news_available": self.market_news_enabled,
            "minutes_before": self.market_news_minutes_before,
            "minutes_after": self.market_news_minutes_after,
            "result_alerts_enabled": get_result_mode(self.database_path, user_id) != "off",
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

    def set_result_alerts(self, user_id: int, enabled: bool) -> dict[str, object]:
        """Liga/desliga o aviso de operacao fechada -- reaproveita set_result_mode,
        o mesmo usado pelo bot no menu "Alertas de resultados"."""
        set_result_mode(self.database_path, user_id, "all" if enabled else "off")
        return self.news_preference(user_id)

    def push_vapid_public_key(self) -> dict[str, object]:
        """Chave publica que o navegador usa como applicationServerKey ao
        chamar pushManager.subscribe() -- nao e segredo, pode ser publica."""
        return {"vapid_public_key": self.vapid_public_key}

    def push_subscribe(
        self,
        user_id: int,
        *,
        endpoint: str,
        p256dh_key: str,
        auth_key: str,
        user_agent: str | None,
    ) -> dict[str, object]:
        """Registra (ou atualiza) a inscricao Web Push deste navegador/celular
        -- usuario pode ter varias (um por dispositivo)."""
        if self.mt5_accounts is None:
            raise ValueError("Notificacao push indisponivel nesta instancia.")
        if not endpoint or not p256dh_key or not auth_key:
            raise ValueError("Inscricao de notificacao incompleta.")
        self.mt5_accounts.save_push_subscription(
            user_id,
            endpoint=endpoint,
            p256dh_key=p256dh_key,
            auth_key=auth_key,
            user_agent=user_agent,
        )
        return {"subscribed": True}

    def push_unsubscribe(self, user_id: int, *, endpoint: str) -> dict[str, object]:
        if self.mt5_accounts is None:
            raise ValueError("Notificacao push indisponivel nesta instancia.")
        self.mt5_accounts.remove_push_subscription(user_id, endpoint)
        return {"subscribed": False}

    def operations(
        self,
        user_id: int,
        *,
        limit: int = 100,
        account_id: int | None = None,
        date: str | None = None,
    ) -> dict[str, object]:
        """Lista de operacoes do cliente. `date` ("AAAA-MM-DD") filtra pelo dia
        em que o sinal foi recebido (g.created_at) -- usado pelo calendario de
        historico do portal; sem ele, mantem o comportamento de sempre
        (ultimas `limit` operacoes, de qualquer dia)."""
        safe_limit = max(1, min(limit, 200))
        with connect_database(self.database_path) as db:
            if account_id is not None:
                self._select_account(db, user_id, account_id)
            rows = db.execute(
                """
                SELECT g.id, g.status, g.symbol, g.direction, g.order_type,
                       g.selected_entry_price, g.stop_loss, g.total_volume,
                       g.created_at, g.error_code, c.title,
                       COUNT(o.id),
                       COALESCE(SUM(CAST(COALESCE(o.net_profit, o.floating_profit, '0') AS REAL)), 0)
                FROM execution_groups g
                LEFT JOIN signals sig ON sig.signature = g.signal_id
                LEFT JOIN source_channels c ON c.telegram_chat_id = sig.source_chat_id
                LEFT JOIN execution_orders o ON o.execution_group_id = g.id
                WHERE g.user_id = ? AND (? IS NULL OR g.mt5_account_id = ?)
                  AND (? IS NULL OR date(g.created_at) = ?)
                GROUP BY g.id
                ORDER BY g.id DESC LIMIT ?
                """,
                (user_id, account_id, account_id, date, date, safe_limit),
            ).fetchall()
            external = []
            if date is None:
                # So o retrato recente (worker atualiza a cada ciclo): conta
                # desconectada nao fica mostrando posicao velha.
                external = db.execute(
                    """
                    SELECT p.mt5_account_id, p.ticket, p.symbol, p.direction, p.volume,
                           p.price_open, p.stop_loss, p.take_profit, p.profit, p.opened_at,
                           a.product_kind
                    FROM mt5_open_positions p
                    JOIN mt5_accounts a ON a.id = p.mt5_account_id
                    WHERE a.user_id = ? AND (? IS NULL OR a.id = ?)
                      AND datetime(p.updated_at) >= datetime('now', '-3 minutes')
                    ORDER BY p.opened_at DESC
                    """,
                    (user_id, account_id, account_id),
                ).fetchall()
        return {
            "manual_positions": [
                {
                    "account_id": int(r[0]), "ticket": r[1], "symbol": r[2],
                    "direction": r[3], "volume": r[4], "entry_price": r[5],
                    "stop_loss": r[6], "take_profit": r[7],
                    "floating_profit": _scaled(r[8], _currency_divisor_kind(r[10])),
                    "opened_at": r[9],
                }
                for r in external
            ],
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
                       trailing_enabled, take_profit_limit, tp1_breakeven_enabled, updated_at,
                       max_spread_points, max_slippage_points, entry_execution_mode,
                       entry_price_mode, pending_expiration_minutes
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
            "max_spread_points", "max_slippage_points", "entry_execution_mode",
            "entry_price_mode", "pending_expiration_minutes",
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
        if field == "entry_execution_mode":
            if raw not in {"pending_order", "market_on_zone", "market_immediate"}:
                raise ValueError("Modo de entrada invalido.")
            return raw
        if field == "entry_price_mode":
            if raw not in {"first_touch", "middle", "distributed"}:
                raise ValueError("Preco da faixa invalido.")
            return raw
        if field in {"split_tps", "breakeven_enabled", "trailing_enabled", "tp1_breakeven_enabled"}:
            if raw not in {"0", "1", "false", "true"}:
                raise ValueError("Valor booleano invalido.")
            return int(raw in {"1", "true"})
        if field in {
            "max_open_signals", "take_profit_limit", "max_spread_points",
            "max_slippage_points", "pending_expiration_minutes",
        }:
            try:
                value = int(raw)
            except ValueError as exc:
                raise ValueError("Valor inteiro invalido.") from exc
            if field == "max_open_signals" and not 1 <= value <= 100:
                raise ValueError("Maximo de sinais deve ficar entre 1 e 100.")
            if field == "take_profit_limit" and not 0 <= value <= 10:
                raise ValueError("Quantidade de Take Profits deve ficar entre 0 e 10.")
            if field in {"max_spread_points", "max_slippage_points"} and not 0 <= value <= 100000:
                raise ValueError("Limite deve ficar entre 0 e 100000 pontos.")
            if field == "pending_expiration_minutes" and not 1 <= value <= 100000:
                raise ValueError("Validade da ordem invalida.")
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
            "max_spread_points": int(row[13]), "max_slippage_points": int(row[14]),
            "entry_execution_mode": row[15], "entry_price_mode": row[16],
            "pending_expiration_minutes": int(row[17]),
        }

    @staticmethod
    def _account(row: object) -> dict[str, object] | None:
        if row is None:
            return None
        divisor = _currency_divisor(row)
        return {
            "id": int(row[0]), "alias": row[1], "broker": row[2], "server": row[3],
            "masked_login": f"••••{str(row[4])[-4:]}", "account_type": row[5],
            "connection_status": row[6],
            "balance": _scaled(row[7], divisor), "equity": _scaled(row[8], divisor),
            "worker_heartbeat_at": row[9], "last_error": row[10], "currency": "USD",
            "product_kind": row[11],
        }

    @staticmethod
    def _performance(
        row: object, floating_total: Decimal = Decimal("0"), *, divisor: Decimal = Decimal(1)
    ) -> dict[str, object] | None:
        if row is None:
            return None
        # net_profit devolvido aqui e o resultado "real real": realizado hoje
        # (account_daily_performance, so negocios ja fechados) + flutuante
        # agora (posicoes ainda abertas) -- o mesmo numero que o cliente veria
        # somando o resultado do dia com o que esta em aberto no MT5.
        realized = Decimal(str(row[1]))
        combined = realized + floating_total
        starting_balance = row[4]
        return_percent = row[5]
        if starting_balance is not None:
            try:
                starting_balance_dec = Decimal(str(starting_balance))
                if starting_balance_dec > 0:
                    return_percent = str(combined * Decimal("100") / starting_balance_dec)
            except InvalidOperation:
                pass
        return {
            "date": row[0], "net_profit": str(combined / divisor),
            "gross_profit": _scaled(row[2], divisor),
            "trading_costs": _scaled(row[3], divisor),
            "starting_balance": _scaled(starting_balance, divisor),
            "return_percent": return_percent, "updated_at": row[6],
        }


def _currency_divisor(account_row: object) -> Decimal:
    """Conta Copy Trader e sempre Cents na corretora: o portal mostra o valor
    em USD (dividido por 100), igual ao bot (bot_service.account_currency_divisor)."""
    if account_row is not None and account_row[11] == PRODUCT_KIND_BROKER_COPY:
        return Decimal(100)
    return Decimal(1)


def _currency_divisor_kind(product_kind: object) -> Decimal:
    return Decimal(100) if product_kind == PRODUCT_KIND_BROKER_COPY else Decimal(1)


def _scaled(value: object, divisor: Decimal) -> object:
    if value is None or divisor == 1:
        return value
    try:
        return str(Decimal(str(value)) / divisor)
    except InvalidOperation:
        return value
