from __future__ import annotations

import configparser
from dataclasses import dataclass
import io
from pathlib import Path
import re
import shutil
from typing import Mapping

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

BROKER_DISPLAY_NAMES = {
    "HFM": "HFM",
    "FTMO": "FTMO",
    "FXGLOBE": "FXGlobe",
    "EXNESS": "Exness",
    "INFINOX": "INFINOX",
}
BROKER_ALIASES = {
    "HF": "HFM",
    "HFM": "HFM",
    "HFMARKETS": "HFM",
    "HOTFOREX": "HFM",
    "FTMO": "FTMO",
    "FXGLOBE": "FXGLOBE",
    "EXNESS": "EXNESS",
    "INFINOX": "INFINOX",
}


@dataclass(frozen=True)
class ProvisionedTerminal:
    account_id: int
    account_dir: Path
    terminal_path: Path
    data_dir: Path
    logs_dir: Path


class TerminalManager:
    def __init__(
        self,
        base_dir: Path,
        template_path: Path | None = None,
        broker_template_paths: Mapping[str, Path] | None = None,
        broker_server_names: Mapping[str, tuple[str, ...]] | None = None,
    ) -> None:
        self.base_dir = base_dir
        self.template_path = template_path
        self.broker_template_paths = {
            canonical_broker_key(name): Path(path)
            for name, path in (broker_template_paths or {}).items()
        }
        self.broker_server_names = {
            canonical_broker_key(name): tuple(servers)
            for name, servers in (broker_server_names or {}).items()
        }
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def available_brokers(self) -> tuple[str, ...]:
        keys = set(self.broker_template_paths)
        if self.template_path is not None:
            keys.add("HFM")
        return tuple(
            BROKER_DISPLAY_NAMES.get(key, key)
            for key in sorted(keys, key=lambda item: BROKER_DISPLAY_NAMES.get(item, item))
        )

    def template_for_broker(self, broker_name: str | None) -> Path | None:
        if not self.broker_template_paths:
            return self.template_path
        key = canonical_broker_key(broker_name or "")
        template = self.broker_template_paths.get(key)
        if template is None and key == "HFM":
            template = self.template_path
        if template is None:
            supported = ", ".join(self.available_brokers())
            raise ValueError(
                f"Corretora sem template MT5 configurado. Disponiveis: {supported}."
            )
        if not (template / "terminal64.exe").is_file():
            raise ValueError(
                f"Template MT5 de {BROKER_DISPLAY_NAMES.get(key, key)} invalido: "
                f"terminal64.exe nao encontrado em {template}."
            )
        return template

    def available_servers(self, broker_name: str) -> tuple[str, ...]:
        key = canonical_broker_key(broker_name)
        servers = list(self.broker_server_names.get(key, ()))
        try:
            template = self.template_for_broker(broker_name)
        except ValueError:
            template = None
        if template is not None:
            config_dir = template / "config"
            if config_dir.is_dir():
                servers.extend(path.stem for path in config_dir.glob("*.srv"))
        return tuple(sorted(
            dict.fromkeys(server.strip() for server in servers if server.strip()),
            key=str.casefold,
        ))

    def account_dir(self, account_id: int) -> Path:
        return self.base_dir / str(account_id)

    def terminal_path(self, account_id: int) -> Path:
        return self.account_dir(account_id) / "terminal64.exe"

    def provision_account(
        self,
        account_id: int,
        *,
        broker_name: str | None = None,
        sanitize_legacy: bool = False,
    ) -> ProvisionedTerminal:
        account_dir = self.account_dir(account_id)
        template_path = self.template_for_broker(broker_name)
        if template_path and template_path.exists() and not account_dir.exists():
            shutil.copytree(template_path, account_dir)
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


def canonical_broker_key(value: str) -> str:
    normalized = re.sub(r"[^A-Z0-9]+", "", value.upper())
    return BROKER_ALIASES.get(normalized, normalized)


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
