from __future__ import annotations

import sys
import time

from .config import AppConfig
from .credential_service import CredentialService
from .command_queue import CommandQueue
from .mt5.account_service import MT5AccountService
from .mt5.account_worker import MT5AccountWorker
from .mt5.client import MT5Client
from .mt5.position_manager import PositionManager
from .mt5.pending_order_monitor import PendingOrderMonitor

POSITION_PROTECTION_POLL_SECONDS = 5


def main() -> int:
    try:
        config = AppConfig.load(create_dirs=True)
        if not config.mt5_credential_key:
            raise ValueError("MT5_CREDENTIAL_KEY nao configurada.")

        accounts = MT5AccountService(
            config.database_path,
            credential_service=CredentialService(config.mt5_credential_key),
            client_factory=MT5Client,
            allow_live_accounts=config.allow_live_accounts,
            max_accounts_per_vps=config.mt5_max_accounts_per_vps,
        )
        command_queue = CommandQueue(config.database_path)
        position_manager = PositionManager(config.database_path, accounts, MT5Client)
        pending_monitor = PendingOrderMonitor(config.database_path)
        workers: dict[int, MT5AccountWorker] = {}
        try:
            while True:
                active_accounts = {
                    account.id: (account, profile)
                    for account, profile in accounts.accounts_for_active_users()
                }
                for account_id in tuple(workers):
                    if account_id not in active_accounts:
                        workers.pop(account_id).close()

                for account_id, (account, profile) in active_accounts.items():
                    worker = workers.get(account_id)
                    if worker is None:
                        worker = MT5AccountWorker(
                            accounts=accounts,
                            account=account,
                            command_queue=command_queue,
                        )
                        workers[account_id] = worker
                    if worker.process_once():
                        try:
                            position_manager.manage_account(account, profile)
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
