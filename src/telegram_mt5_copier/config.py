from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
import re
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
    instance_id: str
    brand_name: str
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
    mt5_broker_template_paths: dict[str, Path]
    mt5_broker_servers: dict[str, tuple[str, ...]]
    mt5_base_dir: Path
    mt5_execution_mode: str
    mt5_max_accounts_per_vps: int
    mt5_onboarding_url: str | None = field(repr=False)
    client_app_url: str | None = field(repr=False)
    onboarding_host: str
    onboarding_port: int
    allow_live_accounts: bool
    default_pending_expiration_minutes: int
    global_execution_kill_switch: bool
    operational_alerts_enabled: bool
    health_check_interval_seconds: int
    health_stale_after_seconds: int
    operational_alert_repeat_minutes: int
    daily_performance_timezone: str
    market_news_enabled: bool
    economic_calendar_api_key: str | None = field(repr=False)
    market_news_minutes_before: int
    market_news_minutes_after: int
    market_news_poll_seconds: int
    telegram_image_ocr_enabled: bool
    telegram_image_ocr_chat_ids: tuple[str, ...] = field(repr=False)
    tesseract_command: str | None = field(repr=False)

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
        instance_id = parse_instance_id(
            _value("INSTANCE_ID", file_values, runtime_env, "main")
        )
        brand_name = _value(
            "BRAND_NAME", file_values, runtime_env, "Instituto Trader"
        ).strip()
        if not brand_name:
            raise ValueError("BRAND_NAME nao pode ficar vazio.")
        onboarding_host = _value(
            "ONBOARDING_HOST", file_values, runtime_env, "127.0.0.1"
        ).strip()
        if not onboarding_host:
            raise ValueError("ONBOARDING_HOST nao pode ficar vazio.")

        config = cls(
            project_root=root,
            instance_id=instance_id,
            brand_name=brand_name,
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
            mt5_broker_template_paths=parse_broker_template_paths(
                _value("MT5_BROKER_TEMPLATES", file_values, runtime_env, ""),
                root,
            ),
            mt5_broker_servers=parse_broker_servers(
                _value("MT5_BROKER_SERVERS", file_values, runtime_env, "")
            ),
            mt5_base_dir=_configured_path("MT5_BASE_DIR", file_values, runtime_env, root, "./mt5_accounts"),
            mt5_execution_mode=_value("MT5_EXECUTION_MODE", file_values, runtime_env, "simulation").strip().lower(),
            mt5_max_accounts_per_vps=parse_positive_int(
                _value("MT5_MAX_ACCOUNTS_PER_VPS", file_values, runtime_env, "10"),
                "MT5_MAX_ACCOUNTS_PER_VPS",
            ),
            mt5_onboarding_url=_optional_value("MT5_ONBOARDING_URL", file_values, runtime_env),
            client_app_url=_optional_value("CLIENT_APP_URL", file_values, runtime_env),
            onboarding_host=onboarding_host,
            onboarding_port=parse_port(
                _value("ONBOARDING_PORT", file_values, runtime_env, "8080")
            ),
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
            operational_alerts_enabled=parse_bool(
                _value("OPERATIONAL_ALERTS_ENABLED", file_values, runtime_env, "true"),
                default=True,
            ),
            health_check_interval_seconds=parse_positive_int(
                _value("HEALTH_CHECK_INTERVAL_SECONDS", file_values, runtime_env, "30"),
                "HEALTH_CHECK_INTERVAL_SECONDS",
            ),
            health_stale_after_seconds=parse_positive_int(
                _value("HEALTH_STALE_AFTER_SECONDS", file_values, runtime_env, "90"),
                "HEALTH_STALE_AFTER_SECONDS",
            ),
            operational_alert_repeat_minutes=parse_positive_int(
                _value("OPERATIONAL_ALERT_REPEAT_MINUTES", file_values, runtime_env, "360"),
                "OPERATIONAL_ALERT_REPEAT_MINUTES",
            ),
            daily_performance_timezone=_value(
                "DAILY_PERFORMANCE_TIMEZONE",
                file_values,
                runtime_env,
                "Europe/Athens",
            ).strip(),
            market_news_enabled=parse_bool(
                _value("MARKET_NEWS_ENABLED", file_values, runtime_env, "false"),
                default=False,
            ),
            economic_calendar_api_key=_optional_value(
                "ECONOMIC_CALENDAR_API_KEY", file_values, runtime_env
            ),
            market_news_minutes_before=parse_non_negative_int(
                _value("MARKET_NEWS_MINUTES_BEFORE", file_values, runtime_env, "10"),
                "MARKET_NEWS_MINUTES_BEFORE",
            ),
            market_news_minutes_after=parse_non_negative_int(
                _value("MARKET_NEWS_MINUTES_AFTER", file_values, runtime_env, "10"),
                "MARKET_NEWS_MINUTES_AFTER",
            ),
            market_news_poll_seconds=parse_positive_int(
                _value("MARKET_NEWS_POLL_SECONDS", file_values, runtime_env, "30"),
                "MARKET_NEWS_POLL_SECONDS",
            ),
            telegram_image_ocr_enabled=parse_bool(
                _value("TELEGRAM_IMAGE_OCR_ENABLED", file_values, runtime_env, "false"),
                default=False,
            ),
            telegram_image_ocr_chat_ids=parse_source_chat_ids(
                _optional_value("TELEGRAM_IMAGE_OCR_CHAT_IDS", file_values, runtime_env)
            ),
            tesseract_command=_optional_value(
                "TESSERACT_CMD", file_values, runtime_env
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
        return self.data_dir / f"telegram_mt5_copier{self.instance_suffix}.sqlite3"

    @property
    def telegram_session_name(self) -> Path:
        return self.session_dir / f"telegram_mt5_copier{self.instance_suffix}"

    @property
    def instance_suffix(self) -> str:
        return "" if self.instance_id == "main" else f"_{self.instance_id}"

    @property
    def supervisor_lock_path(self) -> Path:
        return self.data_dir / f"telegram-mt5-supervisor{self.instance_suffix}.lock"

    @property
    def signal_monitor_lock_path(self) -> Path:
        return self.data_dir / f"telegram-signal-monitor{self.instance_suffix}.lock"

    @property
    def signal_monitor_log_path(self) -> Path:
        return self.log_dir / f"telegram-mt5-copier{self.instance_suffix}.log"

    @property
    def local_onboarding_url(self) -> str:
        return f"http://{self.onboarding_host}:{self.onboarding_port}"

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


def parse_instance_id(value: str) -> str:
    normalized = value.strip().lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,39}", normalized):
        raise ValueError(
            "INSTANCE_ID deve ter de 1 a 40 caracteres: letras, numeros, _ ou -."
        )
    return normalized


def parse_port(value: str) -> int:
    try:
        port = int(value)
    except ValueError as exc:
        raise ValueError("ONBOARDING_PORT deve ser um numero inteiro.") from exc
    if not 1 <= port <= 65535:
        raise ValueError("ONBOARDING_PORT deve ficar entre 1 e 65535.")
    return port


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


def parse_broker_template_paths(value: str | None, project_root: Path) -> dict[str, Path]:
    if not value:
        return {}

    templates: dict[str, Path] = {}
    for raw_item in value.split(";"):
        item = raw_item.strip()
        if not item:
            continue
        if "=" not in item:
            raise ValueError(
                "MT5_BROKER_TEMPLATES deve usar CORRETORA=CAMINHO, separado por ponto e virgula."
            )
        broker, raw_path = (part.strip() for part in item.split("=", 1))
        if not broker or not raw_path:
            raise ValueError("MT5_BROKER_TEMPLATES contem corretora ou caminho vazio.")
        if broker.upper() in templates:
            raise ValueError(f"Template MT5 duplicado para a corretora {broker}.")
        templates[broker.upper()] = project_path(raw_path, project_root)
    return templates


def parse_broker_servers(value: str | None) -> dict[str, tuple[str, ...]]:
    """Parse CORRETORA=Servidor1|Servidor2;OUTRA=Servidor into a safe catalog."""
    if not value:
        return {}
    catalog: dict[str, tuple[str, ...]] = {}
    for raw_item in value.split(";"):
        item = raw_item.strip()
        if not item:
            continue
        if "=" not in item:
            raise ValueError(
                "MT5_BROKER_SERVERS deve usar CORRETORA=SERVIDOR1|SERVIDOR2."
            )
        broker, raw_servers = (part.strip() for part in item.split("=", 1))
        key = broker.upper()
        servers = tuple(dict.fromkeys(
            server.strip() for server in raw_servers.split("|") if server.strip()
        ))
        if not key or not servers:
            raise ValueError("MT5_BROKER_SERVERS contem corretora ou servidor vazio.")
        if key in catalog:
            raise ValueError(f"Servidores MT5 duplicados para a corretora {broker}.")
        catalog[key] = servers
    return catalog


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


def parse_non_negative_int(value: str, name: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ValueError(f"{name} deve ser um numero inteiro.") from exc
    if parsed < 0:
        raise ValueError(f"{name} nao pode ser negativo.")
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
