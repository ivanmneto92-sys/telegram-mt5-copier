from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import shutil

SAFE_COMMON_CONFIGURATION = """[Common]
KeepPrivate=0

[Experts]
Enabled=1
AllowLiveTrading=1
Account=0
Profile=0
"""
SANITIZATION_MARKER = ".telegram-mt5-sanitized"


@dataclass(frozen=True)
class ProvisionedTerminal:
    account_id: int
    account_dir: Path
    terminal_path: Path
    data_dir: Path
    logs_dir: Path


class TerminalManager:
    def __init__(self, base_dir: Path, template_path: Path | None = None) -> None:
        self.base_dir = base_dir
        self.template_path = template_path
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def account_dir(self, account_id: int) -> Path:
        return self.base_dir / str(account_id)

    def terminal_path(self, account_id: int) -> Path:
        return self.account_dir(account_id) / "terminal64.exe"

    def provision_account(
        self,
        account_id: int,
        *,
        sanitize_legacy: bool = False,
    ) -> ProvisionedTerminal:
        account_dir = self.account_dir(account_id)
        if self.template_path and self.template_path.exists() and not account_dir.exists():
            shutil.copytree(self.template_path, account_dir)
            sanitize_copied_terminal(account_dir)
        else:
            account_dir.mkdir(parents=True, exist_ok=True)
            if (
                sanitize_legacy
                and not (account_dir / SANITIZATION_MARKER).exists()
            ):
                sanitize_copied_terminal(account_dir)

        data_dir = account_dir / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        logs_dir = account_dir / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        heartbeat_path = account_dir / "heartbeat.txt"
        heartbeat_path.touch(exist_ok=True)
        terminal_path = self.terminal_path(account_id)
        if not terminal_path.exists():
            terminal_path.touch()
        return ProvisionedTerminal(
            account_id=account_id,
            account_dir=account_dir,
            terminal_path=terminal_path,
            data_dir=data_dir,
            logs_dir=logs_dir,
        )


def sanitize_copied_terminal(account_dir: Path) -> None:
    config_dir = account_dir / "config"
    for sensitive_file in (
        config_dir / "accounts.dat",
        config_dir / "common.ini",
        config_dir / "terminal.ini",
    ):
        try:
            sensitive_file.unlink()
        except FileNotFoundError:
            pass
        except PermissionError as exc:
            raise ValueError(
                "Feche o terminal MT5 desta conta antes de repetir a conexao."
            ) from exc

    for runtime_dir in (
        account_dir / "bases",
        account_dir / "logs",
        account_dir / "MQL5" / "Logs",
        account_dir / "Tester" / "logs",
    ):
        if runtime_dir.is_dir():
            try:
                shutil.rmtree(runtime_dir)
            except PermissionError as exc:
                raise ValueError(
                    "Feche o terminal MT5 desta conta antes de repetir a conexao."
                ) from exc

    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "common.ini").write_text(
        SAFE_COMMON_CONFIGURATION,
        encoding="utf-16",
    )
    (account_dir / SANITIZATION_MARKER).write_text(
        "sanitized\n",
        encoding="ascii",
    )
