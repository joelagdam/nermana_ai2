from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any, TypeVar, get_args, get_origin, get_type_hints


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = PROJECT_ROOT / "data"
MODELS_DIR = PROJECT_ROOT / "models"
DEFAULT_CONFIG_PATH = DATA_DIR / "config.json"
WINDOWS_PATH_RE = re.compile(r"^[A-Za-z]:[\\/]")


def is_termux() -> bool:
    prefix = os.environ.get("PREFIX", "")
    return "com.termux" in prefix or Path("/data/data/com.termux/files/home").exists()


def resolve_path(value: str | Path) -> Path:
    path = Path(value).expanduser()
    if path.is_absolute():
        return path
    return PROJECT_ROOT / path


def _windows_path(value: str) -> bool:
    return bool(WINDOWS_PATH_RE.match(value)) or "\\" in value


@dataclass
class ServerConfig:
    host: str = "127.0.0.1"
    port: int = 8765
    public_url: str = ""


@dataclass
class ModelConfig:
    models_dir: str = "models"
    active_model: str = ""
    fallback_model: str = ""
    llama_server_path: str = "auto"
    base_url: str = "http://127.0.0.1:8080/v1"
    context_size: int = 4096
    temperature: float = 0.7
    top_p: float = 0.8
    threads: int = 0
    batch_size: int = 512
    ubatch_size: int = 128
    parallel_slots: int = 1
    mlock: bool = True
    no_mmap: bool = False
    request_timeout_seconds: float = 120.0
    thinking_mode: str = "auto"
    auto_start_server: bool = True


@dataclass
class SearchConfig:
    enabled: bool = True
    provider: str = "auto"
    searxng_url: str = ""
    timeout_seconds: float = 8.0
    max_results: int = 5
    safe_search: int = 1


@dataclass
class WeatherConfig:
    enabled: bool = True
    location_name: str = "Tagum City"
    latitude: float | None = None
    longitude: float | None = None
    temperature_unit: str = "celsius"
    wind_speed_unit: str = "kmh"
    timezone: str = "auto"
    timeout_seconds: float = 8.0


@dataclass
class FileConfig:
    enabled: bool = True
    allowed_dirs: list[str] = field(default_factory=lambda: ["data"])
    max_read_mb: int = 5
    index_memory: bool = True


@dataclass
class ProviderConfig:
    image_enabled: bool = False
    image_endpoint: str = ""
    image_api_key: str = ""
    vision_enabled: bool = False
    vision_endpoint: str = ""
    vision_api_key: str = ""
    timeout_seconds: float = 60.0


@dataclass
class PhoneConfig:
    enabled: bool = True
    termux_enabled: bool = True
    shizuku_enabled: bool = True
    rish_path: str = "rish"
    command_timeout_seconds: float = 12.0
    autonomy: str = "power_user"
    allowed_settings_namespaces: list[str] = field(default_factory=lambda: ["system", "secure", "global"])


@dataclass
class TelegramConfig:
    enabled: bool = False
    token: str = ""
    allowed_user_ids: list[int] = field(default_factory=list)
    poll_interval_seconds: float = 2.0
    offset_path: str = "data/telegram_offset.txt"


@dataclass
class MemoryConfig:
    db_path: str = "data/nermana.sqlite3"
    retain_messages: int = 200
    auto_remember: bool = True
    consolidate_every_seconds: float = 900.0
    min_consolidate_items: int = 4


@dataclass
class SafetyConfig:
    autonomy: str = "power_user"
    require_confirmation_for_power: bool = False
    semi_auto_tools_enabled: bool = True
    confirm_semi_auto_tools: bool = True
    blocked_tools: list[str] = field(default_factory=lambda: ["phone_shell"])
    max_tool_risk: str = "power"


@dataclass
class AppConfig:
    server: ServerConfig = field(default_factory=ServerConfig)
    model: ModelConfig = field(default_factory=ModelConfig)
    search: SearchConfig = field(default_factory=SearchConfig)
    weather: WeatherConfig = field(default_factory=WeatherConfig)
    files: FileConfig = field(default_factory=FileConfig)
    providers: ProviderConfig = field(default_factory=ProviderConfig)
    phone: PhoneConfig = field(default_factory=PhoneConfig)
    telegram: TelegramConfig = field(default_factory=TelegramConfig)
    memory: MemoryConfig = field(default_factory=MemoryConfig)
    safety: SafetyConfig = field(default_factory=SafetyConfig)
    tool_enabled: dict[str, bool] = field(default_factory=dict)


T = TypeVar("T")


def config_path() -> Path:
    return resolve_path(os.environ.get("NERMANA_CONFIG", DEFAULT_CONFIG_PATH))


def ensure_runtime_dirs(config: AppConfig | None = None) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    if config is not None:
        resolve_path(config.model.models_dir).mkdir(parents=True, exist_ok=True)
        resolve_path(config.memory.db_path).parent.mkdir(parents=True, exist_ok=True)
        for folder in config.files.allowed_dirs:
            try:
                resolve_path(folder).mkdir(parents=True, exist_ok=True)
            except OSError:
                pass


def load_config(path: str | Path | None = None) -> AppConfig:
    target = Path(path) if path else config_path()
    ensure_runtime_dirs()
    if not target.exists():
        cfg = AppConfig()
        save_config(cfg, target)
        return cfg
    with target.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    cfg = sanitize_for_runtime(_from_dict(AppConfig, data))
    ensure_runtime_dirs(cfg)
    return cfg


def save_config(config: AppConfig, path: str | Path | None = None) -> None:
    target = Path(path) if path else config_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8") as handle:
        json.dump(asdict(config), handle, indent=2, sort_keys=True)


def merge_config(config: AppConfig, patch: dict[str, Any]) -> AppConfig:
    data = asdict(config)
    _deep_update(data, patch)
    return sanitize_for_runtime(_from_dict(AppConfig, data))


def sanitize_for_runtime(config: AppConfig) -> AppConfig:
    if not is_termux():
        return config
    defaults = AppConfig()
    if _windows_path(config.model.models_dir):
        config.model.models_dir = defaults.model.models_dir
    if _windows_path(config.memory.db_path):
        config.memory.db_path = defaults.memory.db_path
    if any(_windows_path(folder) for folder in config.files.allowed_dirs):
        config.files.allowed_dirs = defaults.files.allowed_dirs
    return config


def public_config(config: AppConfig) -> dict[str, Any]:
    data = asdict(config)
    if data["telegram"]["token"]:
        data["telegram"]["token"] = "***"
    if data["providers"]["image_api_key"]:
        data["providers"]["image_api_key"] = "***"
    if data["providers"]["vision_api_key"]:
        data["providers"]["vision_api_key"] = "***"
    return data


def default_public_config() -> dict[str, Any]:
    return public_config(sanitize_for_runtime(AppConfig()))


def reset_config_defaults(
    current: AppConfig,
    preserve_secrets: bool = True,
    preserve_model_selection: bool = True,
) -> AppConfig:
    new_config = AppConfig()
    if preserve_model_selection:
        new_config.model.active_model = current.model.active_model
        new_config.model.fallback_model = current.model.fallback_model
        new_config.model.models_dir = current.model.models_dir
    if preserve_secrets:
        new_config.telegram.token = current.telegram.token
        new_config.telegram.allowed_user_ids = list(current.telegram.allowed_user_ids)
        new_config.telegram.enabled = current.telegram.enabled
        new_config.providers.image_api_key = current.providers.image_api_key
        new_config.providers.vision_api_key = current.providers.vision_api_key
    return sanitize_for_runtime(new_config)


def _deep_update(base: dict[str, Any], patch: dict[str, Any]) -> None:
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_update(base[key], value)
        else:
            base[key] = value


def _from_dict(cls: type[T], data: dict[str, Any]) -> T:
    kwargs: dict[str, Any] = {}
    type_hints = get_type_hints(cls)
    for item in fields(cls):
        if item.name not in data:
            continue
        value = data[item.name]
        field_type = type_hints.get(item.name, item.type)
        origin = get_origin(field_type)
        args = get_args(field_type)
        if is_dataclass(field_type) and isinstance(value, dict):
            kwargs[item.name] = _from_dict(field_type, value)
        elif origin is list and args:
            kwargs[item.name] = list(value or [])
        elif origin is dict:
            kwargs[item.name] = dict(value or {})
        else:
            kwargs[item.name] = value
    return cls(**kwargs)
