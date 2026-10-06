"""Web Push (site/PWA) para TODAS as contas MT5 ativas, sem depender do
magic number do copiador -- ao contrario do bot do Telegram (que so avisa
operacoes abertas por este sistema), aqui cobrimos tambem o resultado do
copy trading feito direto na corretora (contas product_kind='broker_copy'),
ja que o historico de deals do MT5 nao diferencia a origem da operacao.

Dois tipos de push:
1. Por operacao fechada (positiva ou negativa), assim que detectada.
2. Resumo do dia (% de resultado), uma vez por conta, logo depois que o dia
   local dela vira (ver account_service.pending_daily_summary_rows).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import logging
from pathlib import Path
from typing import Callable

from ..web_push import ExpiredPushSubscription, WebPushSender
from .account_service import MT5AccountService
from .daily_performance import (
    DEFAULT_BROKER_TIMEZONE,
    calculate_daily_performance,
)
from .ipc_lock import MT5OperationLock
from .models import MT5Account
from .pending_order_executor import mt5_constant

LOGGER = logging.getLogger(__name__)


class TradePushNotifier:
    def __init__(
        self,
        accounts: MT5AccountService,
        sender: WebPushSender,
        *,
        client_factory: Callable[[], object],
        timezone_name: str = DEFAULT_BROKER_TIMEZONE,
        logger: logging.Logger | None = None,
    ) -> None:
        self.accounts = accounts
        self.sender = sender
        self.client_factory = client_factory
        self.timezone_name = timezone_name
        self.logger = logger or LOGGER

    def process_account(self, account: MT5Account) -> None:
        if not self.sender.configured or account.terminal_path is None:
            return
        subscriptions = self.accounts.push_subscriptions_for_user(account.user_id)
        if not subscriptions:
            return

        client = self.client_factory()
        operation_lock = MT5OperationLock(account.terminal_path.parent)
        password: str | None = None
        try:
            operation_lock.acquire()
            password = self.accounts.decrypted_password_for_account(account)
            if not client.initialize(
                account.terminal_path, int(account.login), password, account.server_name
            ):
                return
            info = client.account_info()
            if info is None:
                return
            # Inscricao nova so passa a receber a partir da proxima rodada: nesta
            # os deals/resumos pendentes sao apenas marcados como avisados.
            primed = [subscription for subscription in subscriptions if subscription.primed]
            self._notify_closed_deals(client, account, primed)
            self._maybe_send_daily_summary(client, account, info.balance, primed)
            self.accounts.mark_push_subscriptions_primed(
                [subscription.endpoint for subscription in subscriptions if not subscription.primed]
            )
        except Exception:
            self.logger.exception("Falha ao processar push da conta %s", account.id)
        finally:
            password = None
            client.shutdown()
            operation_lock.close()

    def _notify_closed_deals(
        self, client: object, account: MT5Account, subscriptions: list
    ) -> None:
        now = datetime.now(tz=timezone.utc)
        deals = tuple(client.history_deals_get(now - timedelta(days=2), now + timedelta(days=1)))
        closing_entry = {
            mt5_constant(client, "DEAL_ENTRY_OUT", 1),
            mt5_constant(client, "DEAL_ENTRY_OUT_BY", 3),
        }
        for deal in deals:
            if int(field(deal, "entry", -1) or -1) not in closing_entry:
                continue
            deal_ticket = str(field(deal, "ticket", "") or "")
            if not deal_ticket:
                continue
            if self.accounts.is_trade_closed_push_notified(account.id, deal_ticket):
                continue
            profit = Decimal(str(field(deal, "profit", 0) or 0))
            for field_name in ("commission", "swap", "fee"):
                profit += Decimal(str(field(deal, field_name, 0) or 0))
            self.accounts.mark_trade_closed_push_notified(account.id, deal_ticket)
            if profit == 0:
                continue
            symbol = str(field(deal, "symbol", "") or account.broker_name)
            self._send_to_subscriptions(
                subscriptions,
                enabled=lambda subscription: subscription.trade_alerts_enabled,
                title="Operação positiva" if profit > 0 else "Operação negativa",
                body=f"{symbol}: {money(profit)}",
                tag=f"trade-{account.id}-{deal_ticket}",
                data={"type": "trade_closed", "account_id": account.id},
            )

    def _maybe_send_daily_summary(
        self,
        client: object,
        account: MT5Account,
        balance: Decimal | None,
        subscriptions: list,
    ) -> None:
        try:
            performance = calculate_daily_performance(
                client, balance, timezone_name=self.timezone_name
            )
            self.accounts.update_daily_performance(account.id, performance)
        except Exception:
            self.logger.warning(
                "Nao foi possivel atualizar o desempenho diario da conta %s para push.",
                account.id,
            )
        pending = self.accounts.pending_daily_summary_rows(account.id)
        for performance_date, return_percent in pending:
            self.accounts.mark_daily_summary_push_sent(account.id, performance_date)
            if return_percent is None:
                continue
            marker = "📈" if return_percent > 0 else "📉" if return_percent < 0 else "➖"
            self._send_to_subscriptions(
                subscriptions,
                enabled=lambda subscription: subscription.daily_summary_enabled,
                title=f"{marker} Resultado do dia",
                body=f"{percent(return_percent)} em {performance_date}",
                tag=f"daily-{account.id}-{performance_date}",
                data={"type": "daily_summary", "account_id": account.id},
            )

    def _send_to_subscriptions(
        self,
        subscriptions: list,
        *,
        enabled: Callable[[object], bool],
        title: str,
        body: str,
        tag: str,
        data: dict[str, object],
    ) -> None:
        for subscription in subscriptions:
            if not enabled(subscription):
                continue
            try:
                self.sender.send(subscription, title=title, body=body, tag=tag, data=data)
            except ExpiredPushSubscription:
                self.accounts.remove_push_subscription_by_endpoint(subscription.endpoint)


def money(value: Decimal) -> str:
    sign = "+" if value > 0 else ""
    return f"{sign}US$ {value.quantize(Decimal('0.01'))}"


def percent(value: Decimal) -> str:
    sign = "+" if value > 0 else ""
    return f"{sign}{value.quantize(Decimal('0.01'))}%"


def field(item: object, name: str, default: object = None) -> object:
    return item.get(name, default) if isinstance(item, dict) else getattr(item, name, default)
