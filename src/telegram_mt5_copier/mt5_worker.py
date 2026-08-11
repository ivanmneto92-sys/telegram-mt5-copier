from __future__ import annotations

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
from .telegram_notifier import TelegramUserNotifier

POSITION_PROTECTION_POLL_SECONDS = 1
ACCOUNT_HEARTBEAT_SECONDS = 15


def main() -> int:
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
                for account_id in tuple(workers):
                    if account_id not in active_accounts:
                        workers.pop(account_id).close()
                        last_account_checks.pop(account_id, None)
                        account_connection_states.pop(account_id, None)

                for account_id, (account, profile) in active_accounts.items():
                    worker = workers.get(account_id)
                    if worker is None:
                        worker = MT5AccountWorker(
                            accounts=accounts,
                            account=account,
                            command_queue=command_queue,
                        )
                        workers[account_id] = worker
                    now = time.monotonic()
                    last_check = last_account_checks.get(account_id)
                    if last_check is None or now - last_check >= ACCOUNT_HEARTBEAT_SECONDS:
                        account_connection_states[account_id] = worker.process_once()
                        last_account_checks[account_id] = time.monotonic()
                    if account_connection_states.get(account_id, False):
                        try:
                            position_manager.manage_account(account, profile)
                            settlement_monitor.deliver_pending(account)
                        except Exception as exc:
                            print(f"Falha ao gerenciar posicoes da conta {account.id}: {exc}", file=sys.stderr)

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


if __name__ == "__main__":
    raise SystemExit(main())
