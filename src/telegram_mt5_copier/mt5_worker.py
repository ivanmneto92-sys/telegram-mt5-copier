from __future__ import annotations

import argparse
import logging
import sys
import time

from .config import AppConfig
from .credential_service import CredentialService
from .command_queue import CommandQueue
from .mt5.account_service import MT5AccountService
from .mt5.account_worker import MT5AccountWorker
from .mt5.client import MT5Client
from .mt5.position_manager import PositionManager
from .mt5.settlement_monitor import SettlementMonitor
from .mt5.pending_order_monitor import PendingOrderMonitor
from .mt5.models import ExecutionProfile, MT5Account
from .telegram_notifier import TelegramUserNotifier

POSITION_PROTECTION_POLL_SECONDS = 1
ACCOUNT_HEARTBEAT_SECONDS = 15


def process_account(
    account: MT5Account,
    profile: ExecutionProfile,
    *,
    workers: dict[int, MT5AccountWorker],
    accounts: MT5AccountService,
    command_queue: CommandQueue,
    position_manager: PositionManager,
    settlement_monitor: SettlementMonitor,
    last_account_checks: dict[int, float],
    account_connection_states: dict[int, bool],
) -> None:
    """Advance one account by one worker tick, in isolation.

    One account's failure (missing terminal, stale lock, broker IPC error,
    ...) must never take down every other account on this VPS. Before this
    guard, an unhandled exception anywhere in here escaped the caller's
    for-loop entirely, crashing the whole mt5_worker process; the supervisor
    then restarted it, but every account went unprotected (no daily
    target/loss kill switch, no BE after TP1, no pending-order management)
    for the length of that crash loop, not just the one broken account.
    """
    try:
        worker = workers.get(account.id)
        if worker is None:
            worker = MT5AccountWorker(
                accounts=accounts,
                account=account,
                command_queue=command_queue,
            )
            workers[account.id] = worker
        now = time.monotonic()
        last_check = last_account_checks.get(account.id)
        if last_check is None or now - last_check >= ACCOUNT_HEARTBEAT_SECONDS:
            account_connection_states[account.id] = worker.process_once()
            last_account_checks[account.id] = time.monotonic()
        if account_connection_states.get(account.id, False):
            position_manager.manage_account(account, profile)
            settlement_monitor.deliver_pending(account)
    except Exception as exc:
        print(f"Falha ao processar conta {account.id}: {exc}", file=sys.stderr)


def run(only_account_id: int | None = None) -> int:
    """Run the position-management loop.

    With only_account_id=None, manages every active account of this
    instance in one process (legacy, single-process mode). With an account
    id, manages exactly that one account and exits as soon as it stops
    being active -- this is the mode telegram-mt5-worker-pool spawns one
    subprocess per account with, since the MetaTrader5 package only
    supports a single live terminal connection per process: two accounts
    can never truly run in parallel inside one process, only take turns.
    Running them as separate processes is what makes a hang on one
    account's terminal (which no exception handler can interrupt, since the
    process is blocked inside a native IPC call) unable to freeze every
    other account too.
    """
    try:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(levelname)s %(message)s",
        )
        config = AppConfig.load(create_dirs=True)
        if not config.mt5_credential_key:
            raise ValueError("MT5_CREDENTIAL_KEY nao configurada.")

        accounts = MT5AccountService(
            config.database_path,
            credential_service=CredentialService(config.mt5_credential_key),
            client_factory=MT5Client,
            allow_live_accounts=config.allow_live_accounts,
            max_accounts_per_vps=config.mt5_max_accounts_per_vps,
            daily_performance_timezone=config.daily_performance_timezone,
        )
        command_queue = CommandQueue(config.database_path)
        user_notifier = TelegramUserNotifier(
            config.telegram_bot_token,
            logger=logging.getLogger("mt5-result-notifier"),
        )
        settlement_monitor = SettlementMonitor(
            config.database_path,
            user_notifier,
            timezone_name=config.daily_performance_timezone,
        )
        position_manager = PositionManager(
            config.database_path,
            accounts,
            MT5Client,
            settlement_monitor,
            user_notifier,
        )
        pending_monitor = PendingOrderMonitor(config.database_path)
        workers: dict[int, MT5AccountWorker] = {}
        last_account_checks: dict[int, float] = {}
        account_connection_states: dict[int, bool] = {}
        try:
            while True:
                active_accounts = {
                    account.id: (account, profile)
                    for account, profile in accounts.accounts_for_active_users()
                }
                if only_account_id is not None:
                    if only_account_id not in active_accounts:
                        return 0
                    active_accounts = {only_account_id: active_accounts[only_account_id]}

                for account_id in tuple(workers):
                    if account_id not in active_accounts:
                        workers.pop(account_id).close()
                        last_account_checks.pop(account_id, None)
                        account_connection_states.pop(account_id, None)

                for account, profile in active_accounts.values():
                    process_account(
                        account,
                        profile,
                        workers=workers,
                        accounts=accounts,
                        command_queue=command_queue,
                        position_manager=position_manager,
                        settlement_monitor=settlement_monitor,
                        last_account_checks=last_account_checks,
                        account_connection_states=account_connection_states,
                    )

                pending_monitor.expire_orders()

                time.sleep(POSITION_PROTECTION_POLL_SECONDS)
        finally:
            for worker in workers.values():
                worker.close()
            command_queue.close()
            pending_monitor.close()
            accounts.close()
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        print(f"Falha no worker MT5: {exc}", file=sys.stderr)
        return 2


def main() -> int:
    parser = argparse.ArgumentParser(prog="telegram-mt5-worker")
    parser.add_argument(
        "--account-id",
        type=int,
        default=None,
        help="Gerencia apenas esta conta e sai quando ela deixar de estar ativa.",
    )
    args = parser.parse_args()
    return run(args.account_id)


if __name__ == "__main__":
    raise SystemExit(main())
