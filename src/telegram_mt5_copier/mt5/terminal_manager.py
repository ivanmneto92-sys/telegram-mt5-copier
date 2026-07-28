from __future__ import annotations

import configparser
from dataclasses import dataclass
import io
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
SENSITIVE_CONFIGURATION_KEYS = {
    "auth",
    "certpassword",
    "login",
    "mql5login",
    "mql5password",
    "password",
    "proxylogin",
    "proxypassword",
}


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
    sanitize_common_configuration(config_dir / "common.ini")
    (account_dir / SANITIZATION_MARKER).write_text(
        "sanitized\n",
        encoding="ascii",
    )


def sanitize_common_configuration(path: Path) -> None:
    parser = configparser.ConfigParser(
        interpolation=None,
        strict=False,
    )
    parser.optionxform = str
    source = read_configuration_text(path)
    if source:
        try:
            parser.read_string(source)
        except configparser.Error:
            parser = configparser.ConfigParser(interpolation=None, strict=False)
            parser.optionxform = str

    for section in parser.sections():
        for option in tuple(parser[section]):
            if option.strip().lower() in SENSITIVE_CONFIGURATION_KEYS:
                parser.remove_option(section, option)

    ensure_section(parser, "Common")
    ensure_section(parser, "Experts")
    set_case_insensitive(parser["Common"], "KeepPrivate", "0")
    for option, setting in (
        ("Enabled", "1"),
        ("AllowLiveTrading", "1"),
        ("Account", "0"),
        ("Profile", "0"),
    ):
        set_case_insensitive(parser["Experts"], option, setting)

    output = io.StringIO()
    parser.write(output, space_around_delimiters=False)
    text = output.getvalue() or SAFE_COMMON_CONFIGURATION
    path.write_text(text, encoding="utf-16")


def read_configuration_text(path: Path) -> str:
    if not path.is_file():
        return ""
    for encoding in ("utf-16", "utf-8-sig", "utf-8"):
        try:
            return path.read_text(encoding=encoding)
        except (UnicodeError, UnicodeDecodeError):
            continue
    return ""


def ensure_section(parser: configparser.ConfigParser, name: str) -> None:
    if not parser.has_section(name):
        parser.add_section(name)


def set_case_insensitive(
    section: configparser.SectionProxy,
    option: str,
    value: str,
) -> None:
    for existing in tuple(section):
        if existing.lower() == option.lower():
            section[existing] = value
            return
    section[option] = value
