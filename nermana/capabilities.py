from __future__ import annotations

import shutil
import socket
from dataclasses import dataclass
from typing import Any

from .config import AppConfig
from .models import ModelManager
from .tooling import ToolRegistry


@dataclass
class Capability:
    name: str
    available: bool
    details: str


def internet_available(timeout: float = 1.5) -> bool:
    try:
        with socket.create_connection(("1.1.1.1", 53), timeout=timeout):
            return True
    except OSError:
        return False


def collect_capabilities(config: AppConfig, models: ModelManager, tools: ToolRegistry, model_health: dict[str, Any] | None = None) -> list[Capability]:
    model_health = model_health or models.runtime_status()
    caps = [
        Capability("internet", internet_available(), "network probe"),
        Capability("local_model", bool(model_health.get("ok")), model_health.get("error") or model_health.get("state", "server healthy")),
        Capability("llama_server_binary", models.resolve_llama_server() is not None, models.resolve_llama_server() or config.model.llama_server_path),
        Capability("termux_api", shutil.which("termux-battery-status") is not None, "termux-battery-status"),
        Capability("shizuku_rish", shutil.which(config.phone.rish_path) is not None, config.phone.rish_path),
        Capability("image_provider", bool(config.providers.image_enabled and config.providers.image_endpoint), config.providers.image_endpoint or "not configured"),
        Capability("vision_provider", bool(config.providers.vision_enabled and config.providers.vision_endpoint), config.providers.vision_endpoint or "not configured"),
        Capability("telegram", bool(config.telegram.enabled and config.telegram.token), "configured" if config.telegram.token else "missing token"),
    ]
    for tool in tools.list_metadata():
        caps.append(Capability(f"tool:{tool['name']}", bool(tool["available"]), tool["details"]))
    return caps
