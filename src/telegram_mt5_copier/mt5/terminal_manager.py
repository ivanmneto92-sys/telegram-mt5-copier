from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import shutil


@dataclass(frozen=True)
class ProvisionedTerminal:
    account_id: int
    account_dir: Path
    terminal_path: Path


class TerminalManager:
    def __init__(self, base_dir: Path, template_path: Path | None = None) -> None:
        self.base_dir = base_dir
        self.template_path = template_path
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def account_dir(self, account_id: int) -> Path:
        return self.base_dir / str(account_id)

    def terminal_path(self, account_id: int) -> Path:
        return self.account_dir(account_id) / "terminal64.exe"

    def provision_account(self, account_id: int) -> ProvisionedTerminal:
        account_dir = self.account_dir(account_id)
        if self.template_path and self.template_path.exists() and not account_dir.exists():
            shutil.copytree(self.template_path, account_dir)
        else:
            account_dir.mkdir(parents=True, exist_ok=True)

        logs_dir = account_dir / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        heartbeat_path = account_dir / "heartbeat.txt"
        heartbeat_path.touch(exist_ok=True)
        terminal_path = self.terminal_path(account_id)
        if not terminal_path.exists():
            terminal_path.touch()
        return ProvisionedTerminal(account_id=account_id, account_dir=account_dir, terminal_path=terminal_path)
