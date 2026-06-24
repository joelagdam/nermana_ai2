from __future__ import annotations

import re
import uuid
from typing import Any

from .config import AppConfig, load_config
from .memory import MemoryStore
from .models import ModelManager
from .tooling import ToolRegistry
from .tools import register_all_tools


class AgentCore:
    def __init__(self, config: AppConfig | None = None):
        self.config = config or load_config()
        self.memory = MemoryStore(self.config.memory)
        self.models = ModelManager(self.config)
        self.tools = ToolRegistry(self.config)
        register_all_tools(self.tools, self.config, self.memory)
        self.pending_actions: dict[str, dict[str, Any]] = {}

    def reload(self, config: AppConfig) -> None:
        self.__init__(config)

    def new_session_id(self) -> str:
        return uuid.uuid4().hex[:12]

    def chat(self, message: str, session_id: str = "default") -> dict[str, Any]:
        message = message.strip()
        if not message:
            return {"ok": False, "error": "message is required"}
        self.memory.add_message(session_id, "user", message)

        pending_result = self._maybe_run_pending_action(message, session_id)
        if pending_result is not None:
            answer = self._tool_result_to_text(pending_result)
            self.memory.add_message(session_id, "assistant", answer)
            return {"ok": True, "session_id": session_id, "reply": answer, "tool_results": [pending_result]}

        direct = self._direct_tool(message)
        tool_context = ""
        tool_results: list[dict[str, Any]] = []
        if direct:
            tool_result = self.tools.run(direct["tool"], direct["payload"])
            tool_results.append(tool_result)
            if direct["tool_only"] or not tool_result.get("ok"):
                answer = self._tool_result_to_text(tool_result)
                self.memory.add_message(session_id, "assistant", answer)
                return {"ok": True, "session_id": session_id, "reply": answer, "tool_results": tool_results}
            tool_context = self._tool_result_to_text(tool_result)
        else:
            suggestion = self._suggest_tool(message)
            if suggestion:
                if self.config.safety.confirm_semi_auto_tools:
                    answer = self._request_tool_confirmation(session_id, suggestion)
                    self.memory.add_message(session_id, "assistant", answer)
                    return {"ok": True, "session_id": session_id, "reply": answer, "pending_tool": suggestion}
                tool_result = self.tools.run(suggestion["tool"], suggestion["payload"])
                tool_results.append(tool_result)
                if tool_result.get("ok"):
                    tool_context = self._tool_result_to_text(tool_result)

        memories = self.memory.search(message, limit=4)
        messages = self._build_messages(session_id, message, memories, tool_context)
        model_reply = self.models.chat(messages)
        if model_reply.get("ok"):
            answer = model_reply["content"].strip()
        else:
            answer = self._fallback_reply(message, memories, tool_results, model_reply.get("error", ""))
        self.memory.add_message(session_id, "assistant", answer)
        self.memory.maybe_remember(message, answer)
        return {
            "ok": True,
            "session_id": session_id,
            "reply": answer,
            "tool_results": tool_results,
            "model_ok": bool(model_reply.get("ok")),
            "model_error": model_reply.get("error", ""),
        }

    def run_tool(self, name: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self.tools.run(name, payload)

    def _build_messages(self, session_id: str, message: str, memories: list, tool_context: str) -> list[dict[str, str]]:
        thinking = self._thinking_hint(message)
        system = (
            "You are Nermana, an offline-first phone AI running in Termux. "
            "Be concise, practical, and honest about unavailable tools. "
            "Use common sense: answer directly when you can, and use tools only when they materially improve the answer. "
            "For semi-automatic tools, ask for confirmation before acting. "
            "Use provided memory and tool context as evidence. "
            "Your conscience is a safety policy, not real consciousness. "
            f"Thinking mode hint: {thinking}.\n"
            f"Active capabilities and tools:\n{self._capability_context()}"
        )
        context_parts = []
        if memories:
            context_parts.append("Relevant memory:\n" + "\n".join(f"- {hit.content[:500]}" for hit in memories))
        if tool_context:
            context_parts.append("Tool context:\n" + tool_context[:4000])
        history = self.memory.get_messages(session_id, limit=12)
        messages = [{"role": "system", "content": system}]
        if context_parts:
            messages.append({"role": "system", "content": "\n\n".join(context_parts)})
        messages.extend({"role": item["role"], "content": item["content"]} for item in history[:-1])
        messages.append({"role": "user", "content": f"{message} {thinking}".strip()})
        return messages

    def _thinking_hint(self, message: str) -> str:
        mode = self.config.model.thinking_mode
        if mode == "think":
            return "/think"
        if mode == "no_think":
            return "/no_think"
        hard_words = ["reason", "calculate", "debug", "prove", "plan", "code", "analyze", "why"]
        return "/think" if any(word in message.lower() for word in hard_words) else "/no_think"

    def _direct_tool(self, message: str) -> dict[str, Any] | None:
        lower = message.lower()
        command_match = re.match(r"^/(search|weather|read|index|image|vision|models|phone)\b\s*(.*)", message, re.I)
        if command_match:
            command = command_match.group(1).lower()
            rest = command_match.group(2).strip()
            if command == "search":
                return {"tool": "web_search", "payload": {"query": rest}, "tool_only": False}
            if command == "weather":
                return {"tool": "current_weather", "payload": {"location": rest}, "tool_only": False}
            if command == "read":
                return {"tool": "read_file", "payload": {"path": rest}, "tool_only": True}
            if command == "index":
                return {"tool": "index_file", "payload": {"path": rest}, "tool_only": True}
            if command == "image":
                return {"tool": "generate_image", "payload": {"prompt": rest}, "tool_only": True}
            if command == "vision":
                parts = rest.split(" ", 1)
                return {"tool": "vision_analyze", "payload": {"path": parts[0], "question": parts[1] if len(parts) > 1 else ""}, "tool_only": True}
            if command == "phone":
                return {"tool": "phone_status", "payload": {}, "tool_only": True}
        if lower.startswith("read file "):
            return {"tool": "read_file", "payload": {"path": message[10:].strip()}, "tool_only": True}
        return None

    def _suggest_tool(self, message: str) -> dict[str, Any] | None:
        if not self.config.safety.semi_auto_tools_enabled:
            return None
        if self._should_get_weather(message):
            return {"tool": "current_weather", "payload": {"location": self._extract_location(message)}, "reason": "weather or forecast request"}
        if self._should_search(message):
            return {"tool": "web_search", "payload": {"query": message}, "reason": "current information request"}
        lower = message.lower()
        if any(word in lower for word in ["battery", "phone status", "device status"]):
            return {"tool": "phone_status", "payload": {}, "reason": "phone status request"}
        if any(word in lower for word in ["remember this", "save this to memory"]):
            return {"tool": "memory", "payload": {"content": message}, "reason": "memory request"}
        return None

    def _request_tool_confirmation(self, session_id: str, suggestion: dict[str, Any]) -> str:
        tool_name = suggestion["tool"]
        if tool_name == "memory":
            self.pending_actions[session_id] = suggestion
            return "I can save that to memory. Reply `yes` to confirm or `cancel` to skip."
        tool = self.tools.get(tool_name)
        available, details = tool.is_available()
        if not available:
            return f"I would use `{tool_name}`, but it is unavailable: {details}."
        self.pending_actions[session_id] = suggestion
        return f"I can use `{tool_name}` for this ({suggestion.get('reason', 'useful tool')}). Reply `yes` to confirm or `cancel` to skip."

    def _maybe_run_pending_action(self, message: str, session_id: str) -> dict[str, Any] | None:
        pending = self.pending_actions.get(session_id)
        if not pending:
            return None
        lowered = message.lower().strip()
        if lowered in {"cancel", "no", "stop", "skip"}:
            self.pending_actions.pop(session_id, None)
            return {"ok": True, "content": "Canceled."}
        if lowered not in {"yes", "y", "confirm", "go", "run", "do it", "ok"}:
            return None
        self.pending_actions.pop(session_id, None)
        if pending["tool"] == "memory":
            memory_id = self.memory.remember(pending["payload"]["content"], tags="conversation,user", source="confirmed-chat")
            return {"ok": True, "content": f"Saved to memory as #{memory_id}."}
        return self.tools.run(pending["tool"], pending["payload"])

    def _should_search(self, message: str) -> bool:
        lower = message.lower()
        return any(phrase in lower for phrase in ["search for ", "look up ", "latest ", "today's ", "current news"])

    def _should_get_weather(self, message: str) -> bool:
        lower = message.lower()
        return "weather" in lower or "forecast" in lower

    def _extract_location(self, message: str) -> str:
        match = re.search(r"(?:in|for)\s+([A-Za-z ,.-]+)$", message)
        return match.group(1).strip() if match else self.config.weather.location_name

    def _capability_context(self) -> str:
        active_tools = [tool for tool in self.tools.list_metadata() if tool.get("enabled") and tool.get("available")]
        llama = self.models.llama_server_status()
        search_configured = self.config.search.provider != "searxng" or bool(self.config.search.searxng_url)
        cap_lines = [
            f"- active_model: {self.config.model.active_model or 'none'}",
            f"- llama_server_binary: {'active' if llama.get('available') else 'inactive'} ({llama.get('resolved') or llama.get('configured')})",
            f"- weather_default_city: {self.config.weather.location_name}",
            f"- search_provider: {self.config.search.provider} {'configured' if search_configured else 'fallback available'}",
            f"- image_provider: {'configured' if self.config.providers.image_enabled and self.config.providers.image_endpoint else 'not configured'}",
            f"- vision_provider: {'configured' if self.config.providers.vision_enabled and self.config.providers.vision_endpoint else 'not configured'}",
        ]
        tool_lines = [f"- {tool['name']}: {tool['description']} risk={tool['risk']}" for tool in active_tools[:16]]
        return "Capabilities:\n" + "\n".join(cap_lines) + "\nTools:\n" + "\n".join(tool_lines)

    def _search_context(self, result: dict[str, Any]) -> str:
        lines = []
        for item in result.get("results", []):
            lines.append(f"{item.get('title')}\n{item.get('url')}\n{item.get('content')}")
        return "\n\n".join(lines)

    def _tool_result_to_text(self, result: dict[str, Any]) -> str:
        if not result.get("ok"):
            return result.get("error", "Tool failed.")
        if "results" in result:
            if not result["results"]:
                return "No results found."
            return "\n".join(f"- {item.get('title', '')}: {item.get('url', '')}\n  {item.get('content', '')}" for item in result["results"])
        if "content" in result:
            return result["content"]
        return str(result)

    def _fallback_reply(self, message: str, memories: list, tool_results: list[dict[str, Any]], model_error: str) -> str:
        if tool_results:
            latest = tool_results[-1]
            if latest.get("ok"):
                return self._tool_result_to_text(latest)
            return f"I could not use the requested tool: {latest.get('error', 'unavailable')}."
        memory_text = ""
        if memories:
            memory_text = "\n\nRelevant memory I found:\n" + "\n".join(f"- {hit.content[:240]}" for hit in memories)
        detail = f" Local model is unavailable: {model_error}" if model_error else ""
        return (
            "I am running offline-first, but the local model is not responding right now."
            f"{detail} Configure or start llama.cpp from the Models page, or use a direct tool command like /search, /weather, /read, /phone, /image, or /vision."
            f"{memory_text}"
        )

    def status(self) -> dict[str, Any]:
        return {
            "config": {
                "model": self.config.model.active_model,
                "base_url": self.config.model.base_url,
                "thinking_mode": self.config.model.thinking_mode,
            },
            "model_health": self.models.server_health(),
            "tools": self.tools.list_metadata(),
            "sessions": self.memory.list_sessions(),
        }

    def settings_snapshot(self) -> dict[str, Any]:
        from .config import public_config

        return public_config(self.config)
