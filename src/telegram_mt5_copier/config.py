from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
from typing import Mapping

ENV_FILE = ".env"

TRUE_VALUES = {"1", "true", "yes", "y", "on"}
FALSE_VALUES = {"0", "false", "no", "n", "off"}


def load_env_file(env_path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not env_path.exists():
        return values

    with env_path.open("r", encoding="utf-8") as file:
        for line_number, raw_line in enumerate(file, start=1):
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                raise ValueError(f"Linha invalida em {ENV_FILE}:{line_number}. Use CHAVE=VALOR.")

            key, value = line.split("=", 1)
            key = key.strip()
            if not key:
                raise ValueError(f"Chave vazia em {ENV_FILE}:{line_number}.")
            values[key] = _strip_quotes(value.strip())

    return values


def parse_bool(value: str | bool | None, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None or value == "":
        return default

    normalized = value.strip().lower()
    if normalized in TRUE_VALUES:
        return True
    if normalized in FALSE_VALUES:
        return False

    raise ValueError(f"Valor booleano invalido: {value!r}.")


def project_path(value: str, project_root: Path) -> Path:
    path = Path(value).expanduser()
    if path.is_absolute():
        return path
    return project_root / path


@dataclass(frozen=True)
class AppConfig:
    project_root: Path
    telegram_api_id: str | None = field(repr=False)
    telegram_api_hash: str | None = field(repr=False)
    telegram_bot_token: str | None = field(repr=False)
    source_chat_id: str | None = field(repr=False)
    source_chat_ids: tuple[str, ...] = field(repr=False)
    destination_chat_id: str | None = field(repr=False)
    bot_admin_ids: tuple[int, ...] = field(repr=False)
    bot_enabled: bool
    dry_run: bool
    data_dir: Path
    session_dir: Path
    log_dir: Path
    mt5_credential_key: str | None = field(repr=False)
    mt5_template_path: Path | None
    mt5_base_dir: Path
    mt5_execution_mode: str
    mt5_max_accounts_per_vps: int
    mt5_onboarding_url: str | None = field(repr=False)
    allow_live_accounts: bool
    default_pending_expiration_minutes: int
    global_execution_kill_switch: bool

    @classmethod
    def load(
        cls,
        project_root: Path | None = None,
        env: Mapping[str, str] | None = None,
        env_file: Path | None = None,
        create_dirs: bool = True,
    ) -> "AppConfig":
        root = Path.cwd() if project_root is None else Path(project_root)
        env_path = env_file if env_file is not None else root / ENV_FILE
        file_values = load_env_file(env_path)
        runtime_env = os.environ if env is None else env
        source_chat_ids = configured_source_chat_ids(file_values, runtime_env)

        config = cls(
            project_root=root,
            telegram_api_id=_optional_value("TELEGRAM_API_ID", file_values, runtime_env),
            telegram_api_hash=_optional_value("TELEGRAM_API_HASH", file_values, runtime_env),
            telegram_bot_token=_optional_value("TELEGRAM_BOT_TOKEN", file_values, runtime_env),
            source_chat_id=source_chat_ids[0] if source_chat_ids else None,
            source_chat_ids=source_chat_ids,
            destination_chat_id=_optional_value("DESTINATION_CHAT_ID", file_values, runtime_env),
            bot_admin_ids=parse_admin_ids(_value("BOT_ADMIN_IDS", file_values, runtime_env, "")),
            bot_enabled=parse_bool(_value("BOT_ENABLED", file_values, runtime_env, "true"), default=True),
            dry_run=parse_bool(_value("DRY_RUN", file_values, runtime_env, "true"), default=True),
            data_dir=_configured_path("DATA_DIR", file_values, runtime_env, root, "./data"),
            session_dir=_configured_path("SESSION_DIR", file_values, runtime_env, root, "./sessions"),
            log_dir=_configured_path("LOG_DIR", file_values, runtime_env, root, "./logs"),
            mt5_credential_key=_optional_value("MT5_CREDENTIAL_KEY", file_values, runtime_env),
            mt5_template_path=_optional_path("MT5_TEMPLATE_PATH", file_values, runtime_env, root),
            mt5_base_dir=_configured_path("MT5_BASE_DIR", file_values, runtime_env, root, "./mt5_accounts"),
            mt5_execution_mode=_value("MT5_EXECUTION_MODE", file_values, runtime_env, "simulation").strip().lower(),
            mt5_max_accounts_per_vps=parse_positive_int(
                _value("MT5_MAX_ACCOUNTS_PER_VPS", file_values, runtime_env, "10"),
                "MT5_MAX_ACCOUNTS_PER_VPS",
            ),
            mt5_onboarding_url=_optional_value("MT5_ONBOARDING_URL", file_values, runtime_env),
            allow_live_accounts=parse_bool(
                _value("ALLOW_LIVE_ACCOUNTS", file_values, runtime_env, "false"),
                default=False,
            ),
            default_pending_expiration_minutes=parse_positive_int(
                _value("DEFAULT_PENDING_EXPIRATION_MINUTES", file_values, runtime_env, "120"),
                "DEFAULT_PENDING_EXPIRATION_MINUTES",
            ),
            global_execution_kill_switch=parse_bool(
                _value("GLOBAL_EXECUTION_KILL_SWITCH", file_values, runtime_env, "true"),
                default=True,
            ),
        )

        if create_dirs:
            config.ensure_directories()

        return config

    def ensure_directories(self) -> None:
        for directory in (self.data_dir, self.session_dir, self.log_dir, self.mt5_base_dir):
            directory.mkdir(parents=True, exist_ok=True)

    @property
    def database_path(self) -> Path:
        return self.data_dir / "telegram_mt5_copier.sqlite3"

    @property
    def telegram_session_name(self) -> Path:
        return self.session_dir / "telegram_mt5_copier"

    def missing_required_telegram_values(self) -> tuple[str, ...]:
        required = {
            "TELEGRAM_API_ID": self.telegram_api_id,
            "TELEGRAM_API_HASH": self.telegram_api_hash,
            "SOURCE_CHAT_IDS": self.source_chat_ids,
            "DESTINATION_CHAT_ID": self.destination_chat_id,
        }
        return tuple(name for name, value in required.items() if not value)


def parse_admin_ids(value: str | None) -> tuple[int, ...]:
    if not value:
        return ()

    admin_ids: list[int] = []
    for raw_item in value.split(","):
        item = raw_item.strip()
        if not item:
            continue
        try:
            admin_ids.append(int(item))
        except ValueError as exc:
            raise ValueError("BOT_ADMIN_IDS deve conter apenas IDs numericos separados por virgula.") from exc

    return tuple(admin_ids)


def parse_source_chat_ids(value: str | None) -> tuple[str, ...]:
    if not value:
        return ()

    source_chat_ids: list[str] = []
    seen: set[str] = set()
    for raw_item in value.split(","):
        item = raw_item.strip()
        if not item:
            continue
        try:
            int(item)
        except ValueError as exc:
            raise ValueError(
                "SOURCE_CHAT_IDS deve conter apenas IDs numericos separados por virgula."
            ) from exc
        if item not in seen:
            source_chat_ids.append(item)
            seen.add(item)

    return tuple(source_chat_ids)


def configured_source_chat_ids(
    file_values: Mapping[str, str],
    runtime_env: Mapping[str, str],
) -> tuple[str, ...]:
    plural_value = _optional_value("SOURCE_CHAT_IDS", file_values, runtime_env)
    if plural_value:
        return parse_source_chat_ids(plural_value)
    return parse_source_chat_ids(_optional_value("SOURCE_CHAT_ID", file_values, runtime_env))


def parse_positive_int(value: str, name: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ValueError(f"{name} deve ser um numero inteiro.") from exc
    if parsed < 1:
        raise ValueError(f"{name} deve ser maior que zero.")
    return parsed


def _value(
    name: str,
    file_values: Mapping[str, str],
    runtime_env: Mapping[str, str],
    default: str,
) -> str:
    return runtime_env.get(name, file_values.get(name, default))


def _optional_value(
    name: str,
    file_values: Mapping[str, str],
    runtime_env: Mapping[str, str],
) -> str | None:
    value = _value(name, file_values, runtime_env, "")
    return value or None


def _configured_path(
    name: str,
    file_values: Mapping[str, str],
    runtime_env: Mapping[str, str],
    project_root: Path,
    default: str,
) -> Path:
    value = _value(name, file_values, runtime_env, default) or default
    return project_path(value, project_root)


def _optional_path(
    name: str,
    file_values: Mapping[str, str],
    runtime_env: Mapping[str, str],
    project_root: Path,
) -> Path | None:
    value = _optional_value(name, file_values, runtime_env)
    if value is None:
        return None
    return project_path(value, project_root)


def _strip_quotes(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value
