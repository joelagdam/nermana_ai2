from __future__ import annotations

from nermana.config import AppConfig
from nermana.memory import MemoryStore
from nermana.tooling import ToolRegistry

from .files import register_file_tools
from .media import register_media_tools
from .phone import register_phone_tools
from .search import register_search_tools
from .weather import register_weather_tools


def register_all_tools(registry: ToolRegistry, config: AppConfig, memory: MemoryStore) -> None:
    register_search_tools(registry, config)
    register_weather_tools(registry, config)
    register_file_tools(registry, config, memory)
    register_media_tools(registry, config)
    register_phone_tools(registry, config)
