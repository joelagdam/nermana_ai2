from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Callable

from .config import AppConfig
from .safety import DecisionGate


ToolHandler = Callable[[dict[str, Any]], dict[str, Any]]
AvailabilityHandler = Callable[[], tuple[bool, str]]


@dataclass
class Tool:
    name: str
    description: str
    provider: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any] = field(default_factory=dict)
    online_required: bool = False
    offline_required: bool = False
    risk: str = "safe"
    timeout_seconds: float = 10.0
    retries: int = 0
    enabled: bool = True
    handler: ToolHandler | None = None
    availability: AvailabilityHandler | None = None

    def metadata(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("handler", None)
        data.pop("availability", None)
        available, details = self.is_available()
        data["available"] = available
        data["details"] = details
        return data

    def is_available(self) -> tuple[bool, str]:
        if not self.enabled:
            return False, "disabled"
        if self.availability is None:
            return True, "available"
        try:
            return self.availability()
        except Exception as exc:
            return False, str(exc)


class ToolRegistry:
    def __init__(self, config: AppConfig):
        self.config = config
        self.gate = DecisionGate(config.safety)
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        if tool.name in self.config.tool_enabled:
            tool.enabled = bool(self.config.tool_enabled[tool.name])
        self._tools[tool.name] = tool

    def set_enabled(self, name: str, enabled: bool) -> None:
        if name not in self._tools:
            raise KeyError(name)
        self._tools[name].enabled = enabled
        self.config.tool_enabled[name] = enabled

    def get(self, name: str) -> Tool:
        return self._tools[name]

    def names(self) -> list[str]:
        return sorted(self._tools)

    def list_metadata(self) -> list[dict[str, Any]]:
        return [self._tools[name].metadata() for name in self.names()]

    def run(self, name: str, payload: dict[str, Any]) -> dict[str, Any]:
        if name not in self._tools:
            return {"ok": False, "error": f"Unknown tool: {name}"}
        tool = self._tools[name]
        if not tool.enabled:
            return {"ok": False, "error": f"{name} is disabled"}
        available, details = tool.is_available()
        if not available:
            return {"ok": False, "error": f"{name} unavailable: {details}"}
        decision = self.gate.evaluate(name, tool.risk)
        if not decision.allowed:
            return {"ok": False, "error": decision.reason}
        if decision.requires_confirmation and not payload.get("confirmed"):
            return {"ok": False, "error": decision.reason, "requires_confirmation": True}
        if tool.handler is None:
            return {"ok": False, "error": f"{name} has no handler"}
        try:
            result = tool.handler(payload)
            result.setdefault("ok", True)
            result.setdefault("tool", name)
            return result
        except Exception as exc:
            return {"ok": False, "error": str(exc), "tool": name}
