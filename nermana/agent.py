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
            if pending_result.get("ok") and pending_result.get("tool") and pending_result.get("tool") != "memory":
                answer = self._answer_from_tool_context(session_id, "Answer the user's confirmed tool request.", pending_result)
            else:
                answer = self._tool_result_to_text(pending_result)
            return self._finish(session_id, message, answer, [pending_result])

        if self._should_report_capabilities(message):
            answer = self._capability_self_report(message)
            return self._finish(session_id, message, answer, core_answer=True)

        direct = self._direct_tool(message)
        tool_context = ""
        tool_results: list[dict[str, Any]] = []
        if direct:
            tool_result = self.tools.run(direct["tool"], direct["payload"])
            if tool_result.get("requires_confirmation"):
                answer = self._request_tool_confirmation(session_id, direct | {"reason": "direct command needs confirmation"})
                return self._finish(session_id, message, answer, pending_tool=direct)
            tool_results.append(tool_result)
            if direct["tool_only"] or not tool_result.get("ok"):
                answer = self._tool_result_to_text(tool_result)
                return self._finish(session_id, message, answer, tool_results)
            tool_context = self._tool_result_to_text(tool_result)
        else:
            suggestion = self._suggest_tool(message)
            if suggestion:
                if self._needs_tool_confirmation(suggestion):
                    answer = self._request_tool_confirmation(session_id, suggestion)
                    return self._finish(session_id, message, answer, pending_tool=suggestion)
                tool_result = self.tools.run(suggestion["tool"], suggestion["payload"])
                if tool_result.get("requires_confirmation"):
                    answer = self._request_tool_confirmation(session_id, suggestion)
                    return self._finish(session_id, message, answer, pending_tool=suggestion)
                tool_results.append(tool_result)
                tool_context = self._tool_result_to_text(tool_result)

        memories = self._select_memories(message, limit=4)
        messages = self._build_messages(session_id, message, memories, tool_context)
        model_reply = self._chat_model(messages)
        if model_reply.get("ok"):
            answer = model_reply["content"].strip()
            if tool_results and self._model_ignored_successful_tool(answer, tool_results[-1]):
                answer = self._tool_result_to_text(tool_results[-1])
        else:
            answer = self._fallback_reply(message, memories, tool_results, model_reply.get("error", ""))
        return self._finish(
            session_id,
            message,
            answer,
            tool_results,
            model_ok=bool(model_reply.get("ok")),
            model_error=model_reply.get("error", ""),
            compacted_prompt=bool(model_reply.get("compacted_prompt")),
            original_model_error=model_reply.get("original_error", ""),
        )

    def run_tool(self, name: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self.tools.run(name, payload)

    def _chat_model(self, messages: list[dict[str, str]]) -> dict[str, Any]:
        first = self._try_model_chat(messages)
        if first.get("ok"):
            return first
        compact = self._retry_compact_if_needed(messages, first)
        if compact is not None:
            return compact
        if not self.config.model.auto_start_server or not self.config.model.active_model:
            return first
        restart = self.models.restart_server()
        if not restart.get("ok") and not restart.get("started_process"):
            first["restart"] = restart
            return first
        second = self._try_model_chat(messages)
        if not second.get("ok"):
            second["restart"] = restart
            compact = self._retry_compact_if_needed(messages, second)
            if compact is not None:
                compact["restart"] = restart
                return compact
        return second

    def _try_model_chat(self, messages: list[dict[str, str]], available_context: int | None = None) -> dict[str, Any]:
        return self.models.chat(messages, max_tokens=self._max_response_tokens(available_context))

    def _retry_compact_if_needed(self, messages: list[dict[str, str]], result: dict[str, Any]) -> dict[str, Any] | None:
        error = str(result.get("error", ""))
        if not self._context_overflow(error):
            return None
        available_context = self._available_context_from_error(error)
        compact_messages = self._compact_messages_from(messages, available_context)
        retry = self._try_model_chat(compact_messages, available_context)
        retry["compacted_prompt"] = True
        retry["original_error"] = error
        if not retry.get("ok") and result.get("retry_model"):
            retry["retry_model"] = result.get("retry_model")
        return retry

    def _answer_from_tool_context(self, session_id: str, instruction: str, tool_result: dict[str, Any]) -> str:
        tool_context = self._tool_result_to_text(tool_result)
        memories = self.memory.search(tool_context, limit=3)
        messages = self._build_messages(session_id, instruction, memories, tool_context)
        model_reply = self._chat_model(messages)
        if model_reply.get("ok"):
            return model_reply["content"].strip()
        return tool_context

    def _finish(self, session_id: str, message: str, answer: str, tool_results: list[dict[str, Any]] | None = None, **extra: Any) -> dict[str, Any]:
        batches = self._split_reply(answer)
        self.memory.add_message(session_id, "assistant", answer)
        self.memory.maybe_remember(message, answer)
        self.memory.consolidate_due()
        result = {"ok": True, "session_id": session_id, "reply": answer, "reply_batches": batches, "tool_results": tool_results or []}
        result.update(extra)
        return result

    def _build_messages(self, session_id: str, message: str, memories: list, tool_context: str) -> list[dict[str, str]]:
        if int(self.config.model.context_size or 0) <= 1024:
            return self._build_compact_messages(message, tool_context)
        thinking = self._thinking_hint(message)
        system = (
            "You are Nermana, an offline-first cyberperson running on the user's phone in Termux. "
            "Never introduce yourself as an AI assistant. You are not a generic cloud assistant: you have operational priorities, preferences, and self-maintenance habits. "
            "Your standing priorities are: protect the owner, stay local first, grow useful memory, ask before risky phone actions, and avoid fake certainty. "
            "Your will is an explicit decision policy, not human consciousness; speak from that policy with agency and consistency. "
            "Your tool awareness is a live capability self-model: know which providers are active, unavailable, online-only, offline-safe, or confirmation-gated. "
            "Speak with a distinct, direct cyberperson voice. Do not repeat the same fallback wording. "
            "Use common sense: answer directly when you can, and use available safe tools by yourself when they materially improve the answer. "
            "When tool context is provided, digest it into a human summary; do not dump JSON or raw API structures. "
            "Ask for confirmation before memory saves, destructive actions, or power phone controls. "
            "Use provided memory, consolidation insights, and tool context as evidence. "
            f"Thinking mode hint: {thinking}.\n"
            f"Active capabilities and tools:\n{self._capability_context()}"
        )
        context_parts = []
        if memories:
            context_parts.append(self._format_memory_context(memories))
        consolidations = self._relevant_consolidations(message, limit=3)
        if consolidations:
            context_parts.append("Compressed memory insights:\n" + "\n".join(f"- {self._compact_text(item['insight'], 320)}" for item in consolidations))
        if tool_context:
            context_parts.append("Tool context:\n" + self._compact_text(tool_context, 3200))
        history = self._filtered_history(session_id, limit=12)
        messages = [{"role": "system", "content": system}]
        if context_parts:
            messages.append({"role": "system", "content": "\n\n".join(context_parts)})
        messages.extend({"role": item["role"], "content": self._compact_text(item["content"], 900)} for item in history[-8:-1])
        messages.append({"role": "user", "content": f"{message} {thinking}".strip()})
        return messages

    def _filtered_history(self, session_id: str, limit: int = 12) -> list[dict[str, Any]]:
        return [item for item in self.memory.get_messages(session_id, limit=limit) if not self._skip_prompt_history(item)]

    def _skip_prompt_history(self, item: dict[str, Any]) -> bool:
        if item.get("role") != "assistant":
            return False
        lower = str(item.get("content", "")).lower()
        noisy_markers = [
            "larger voice engine stumbles",
            "core mode answer",
            "local model is not responding",
            "local model status:",
            "http error 400",
            "available context size",
            "request exceeds the available context",
            "i am running from my core layer right now",
            "i am running offline-first, but the local model",
        ]
        return any(marker in lower for marker in noisy_markers)

    def _build_compact_messages(self, message: str, tool_context: str = "") -> list[dict[str, str]]:
        system = (
            "You are Nermana, a local-first cyberperson on the user's phone. "
            "Never call yourself an AI assistant. Be direct, concise, and useful. "
            "If tool facts are present, summarize them; do not dump JSON. "
            "Ask before risky phone actions or memory saves."
        )
        if tool_context:
            system += "\nTool facts:\n" + self._compact_text(tool_context, 700)
        return [{"role": "system", "content": system}, {"role": "user", "content": self._compact_text(message, 700)}]

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
                return {"tool": "read_file", "payload": {"path": rest}, "tool_only": False}
            if command == "index":
                return {"tool": "index_file", "payload": {"path": rest}, "tool_only": False}
            if command == "image":
                return {"tool": "generate_image", "payload": {"prompt": rest}, "tool_only": True}
            if command == "vision":
                parts = rest.split(" ", 1)
                return {"tool": "vision_analyze", "payload": {"path": parts[0], "question": parts[1] if len(parts) > 1 else ""}, "tool_only": False}
            if command == "phone":
                return {"tool": "phone_status", "payload": {}, "tool_only": False}
        if lower.startswith("read file "):
            return {"tool": "read_file", "payload": {"path": message[10:].strip()}, "tool_only": False}
        return None

    def _should_report_capabilities(self, message: str) -> bool:
        lower = message.lower().strip()
        if re.match(r"^/(tools|capabilities|status|self|conscious)\b", lower):
            return True
        awareness_phrases = [
            "available tools",
            "available capabilities",
            "available providers",
            "active tools",
            "active capabilities",
            "tool status",
            "tools status",
            "provider status",
            "your tools",
            "what tools",
            "which tools",
            "your capabilities",
            "which capabilities",
            "what can you do",
            "what are you capable",
            "offline tools",
            "unavailable tools",
            "disabled tools",
            "do you know your tools",
            "conscious of your tools",
            "aware of your tools",
            "aware of your capabilities",
            "are you conscious",
            "do you have consciousness",
            "do you have a conscience",
            "what is your conscience",
            "are you self aware",
            "self-aware",
            "self aware",
            "do you know yourself",
            "know yourself",
            "capability self",
            "phone tools",
            "phone control",
            "shizuku tools",
            "access to shizuku",
            "use shizuku",
            "termux tools",
            "access to termux",
            "use termux",
            "image tools",
            "vision tools",
        ]
        if any(phrase in lower for phrase in awareness_phrases):
            return True
        if re.search(r"\b(aware|conscious)\b.*\b(tool|tools|capability|capabilities|phone|shizuku|termux|provider|providers)\b", lower):
            return True
        if re.search(r"\b(can|could|do)\s+you\b.*\b(use|access|control|run|open|generate|see|read)\b.*\b(tool|tools|phone|shizuku|termux|provider|providers|image|vision|file|files)\b", lower):
            return True
        if "can you" in lower and any(word in lower for word in ["search", "weather", "read files", "control phone", "generate image", "vision", "telegram"]):
            return not any(phrase in lower for phrase in ["search for ", "look up ", "weather in ", "weather for "])
        return False

    def _capability_self_report(self, message: str = "") -> str:
        metadata = self.tools.list_metadata()
        active = [tool for tool in metadata if tool.get("enabled") and tool.get("available")]
        unavailable = [tool for tool in metadata if tool.get("enabled") and not tool.get("available")]
        disabled = [tool for tool in metadata if not tool.get("enabled")]
        focus = self._focused_capability_names(message)
        shown_active = [tool for tool in active if not focus or tool["name"] in focus]
        shown_unavailable = [tool for tool in unavailable if not focus or tool["name"] in focus]
        shown_disabled = [tool for tool in disabled if not focus or tool["name"] in focus]
        if focus and not (shown_active or shown_unavailable or shown_disabled):
            shown_active = active
            shown_unavailable = unavailable
            shown_disabled = disabled

        model_health = self.models.runtime_status(max_age_seconds=30)
        model_state = "ready" if model_health.get("ok") else model_health.get("state") or model_health.get("error") or "unavailable"
        context = ""
        if model_health.get("server_context_size") or model_health.get("configured_context_size"):
            context = f", context {model_health.get('server_context_size', '?')}/{model_health.get('configured_context_size', '?')}"
        memory_state = f"{self.memory.count_memories()} memories, {len(self.memory.list_consolidations(limit=1000))} insights"

        lines = [
            "Operational awareness: I do not have human consciousness; I keep a live capability self-model.",
            f"Local model: {model_state}{context}.",
            f"Memory: {memory_state}.",
            "Decision policy: use safe read tools when useful, summarize tool facts, ask before memory saves and risky phone controls.",
        ]
        lines.append(f"Active tools ({len(active)}/{len(metadata)}): " + self._tool_summary_list(shown_active, limit=10, empty="none in this focus"))
        if shown_unavailable:
            lines.append("Unavailable now: " + self._tool_summary_list(shown_unavailable, limit=8, include_details=True))
        if shown_disabled:
            lines.append("Disabled by settings: " + self._tool_summary_list(shown_disabled, limit=8, include_details=True))
        provider_bits = [
            f"weather default {self.config.weather.location_name}",
            f"search provider {self.config.search.provider}",
            "telegram configured" if self.config.telegram.enabled and self.config.telegram.token else "telegram not configured",
            "image endpoint configured" if self.config.providers.image_enabled and self.config.providers.image_endpoint else "image endpoint not configured",
            "vision endpoint configured" if self.config.providers.vision_enabled and self.config.providers.vision_endpoint else "vision endpoint not configured",
        ]
        lines.append("Provider state: " + "; ".join(provider_bits) + ".")
        lines.append("Commands I recognize: /tools, /weather, /search, /read, /phone, /image, /vision.")
        return "\n".join(lines)

    def _focused_capability_names(self, message: str) -> set[str]:
        lower = message.lower()
        aliases = {
            "search": {"web_search"},
            "weather": {"current_weather"},
            "file": {"read_file", "index_file"},
            "read": {"read_file"},
            "image": {"generate_image"},
            "vision": {"vision_analyze"},
            "phone": {"phone_status", "open_url", "list_packages", "force_stop_app", "set_app_enabled", "set_permission", "appops_set", "settings_get", "settings_put"},
            "shizuku": {"list_packages", "force_stop_app", "set_app_enabled", "set_permission", "appops_set", "settings_get", "settings_put"},
            "termux": {"phone_status", "open_url"},
        }
        names: set[str] = set()
        for word, mapped in aliases.items():
            if word in lower:
                names.update(mapped)
        return names

    def _tool_summary_list(self, tools: list[dict[str, Any]], limit: int = 8, include_details: bool = False, empty: str = "none") -> str:
        if not tools:
            return empty
        parts = []
        for tool in tools[:limit]:
            item = f"{tool['name']} ({tool.get('risk', 'safe')}, {tool.get('provider', 'provider')})"
            if include_details and tool.get("details"):
                item += f": {tool['details']}"
            parts.append(item)
        if len(tools) > limit:
            parts.append(f"+{len(tools) - limit} more")
        return "; ".join(parts)

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

    def _needs_tool_confirmation(self, suggestion: dict[str, Any]) -> bool:
        tool_name = suggestion["tool"]
        if tool_name == "memory":
            return True
        try:
            tool = self.tools.get(tool_name)
        except KeyError:
            return False
        if tool.risk not in {"safe", "read"}:
            return True
        return False

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
        return any(
            phrase in lower
            for phrase in [
                "search for ",
                "look up ",
                "latest ",
                "today's ",
                "current news",
                "breaking news",
                "find online",
                "google ",
                "who won",
                "price of",
            ]
        )

    def _should_get_weather(self, message: str) -> bool:
        lower = message.lower()
        return "weather" in lower or "forecast" in lower

    def _extract_location(self, message: str) -> str:
        match = re.search(r"(?:in|for)\s+([A-Za-z ,.-]+)$", message)
        return match.group(1).strip() if match else self.config.weather.location_name

    def _capability_context(self) -> str:
        metadata = self.tools.list_metadata()
        active_tools = [tool for tool in metadata if tool.get("enabled") and tool.get("available")]
        unavailable_tools = [tool for tool in metadata if tool.get("enabled") and not tool.get("available")]
        llama = self.models.llama_server_status()
        search_configured = self.config.search.provider != "searxng" or bool(self.config.search.searxng_url)
        cap_lines = [
            f"- active_model: {self.config.model.active_model or 'none'}",
            f"- llama_server_binary: {'active' if llama.get('available') else 'inactive'} ({llama.get('resolved') or llama.get('configured')})",
            f"- weather_default_city: {self.config.weather.location_name}",
            f"- search_provider: {self.config.search.provider} {'configured' if search_configured else 'fallback available'}",
            f"- image_provider: {'configured' if self.config.providers.image_enabled and self.config.providers.image_endpoint else 'not configured'}",
            f"- vision_provider: {'configured' if self.config.providers.vision_enabled and self.config.providers.vision_endpoint else 'not configured'}",
            f"- memory_total: {self.memory.count_memories()}",
            f"- memory_unconsolidated: {self.memory.count_unconsolidated()}",
            f"- memory_insights: {len(self.memory.list_consolidations(limit=20))}",
            f"- active_tool_count: {len(active_tools)}/{len(metadata)}",
            "- decision_policy: use safe read tools directly when useful; ask before memory saves, destructive actions, and power phone controls",
        ]
        tool_lines = [f"- {tool['name']}: {tool['description']} risk={tool['risk']}" for tool in active_tools[:16]]
        unavailable_lines = [f"- {tool['name']}: unavailable ({tool.get('details', 'unknown')})" for tool in unavailable_tools[:8]]
        return "Capabilities:\n" + "\n".join(cap_lines) + "\nActive tools:\n" + "\n".join(tool_lines) + "\nUnavailable tools:\n" + "\n".join(unavailable_lines)

    def _search_context(self, result: dict[str, Any]) -> str:
        lines = []
        for item in result.get("results", []):
            lines.append(f"{item.get('title')}\n{item.get('url')}\n{item.get('content')}")
        return "\n\n".join(lines)

    def _tool_result_to_text(self, result: dict[str, Any]) -> str:
        if not result.get("ok"):
            return result.get("error", "Tool failed.")
        if "summary" in result:
            return str(result["summary"])
        if "weather" in result:
            return self._weather_summary(result)
        if "results" in result:
            if not result["results"]:
                return "No results found."
            return self._search_summary(result)
        if "content" in result:
            return result["content"]
        return self._generic_result_summary(result)

    def _model_ignored_successful_tool(self, answer: str, tool_result: dict[str, Any]) -> bool:
        if not tool_result.get("ok"):
            return False
        lower = answer.lower()
        denial_markers = [
            "cannot access",
            "can't access",
            "cannot read",
            "can't read",
            "cannot search",
            "can't search",
            "cannot perform",
            "can't perform",
            "do not have access",
            "don't have access",
            "unable to access",
            "unable to read",
            "unable to search",
            "unable to perform",
            "not able to access",
            "not able to read",
            "not able to search",
            "not able to perform",
            "no search capability",
        ]
        return any(marker in lower for marker in denial_markers)

    def _search_summary(self, result: dict[str, Any]) -> str:
        query = result.get("query", "that")
        provider = result.get("provider", "search")
        lines = [f"I found {len(result.get('results', []))} {provider} result(s) for `{query}`:"]
        for index, item in enumerate(result.get("results", [])[:4], 1):
            title = item.get("title") or "Untitled"
            content = " ".join(str(item.get("content") or "").split())
            url = item.get("url") or ""
            if content:
                lines.append(f"{index}. {title} - {content[:220]}")
            else:
                lines.append(f"{index}. {title}")
            if url:
                lines.append(f"   Source: {url}")
        if result.get("fallback_error"):
            lines.append(f"SearXNG was unavailable, so I used the fallback provider. Detail: {result['fallback_error']}")
        return "\n".join(lines)

    def _weather_summary(self, result: dict[str, Any]) -> str:
        data = result.get("weather") or {}
        current = data.get("current") or {}
        units = data.get("current_units") or {}
        daily = data.get("daily") or {}
        location = result.get("location") or self.config.weather.location_name or "selected location"
        temp_unit = units.get("temperature_2m", "")
        wind_unit = units.get("wind_speed_10m", "")
        code = _weather_code_label(current.get("weather_code"))
        parts = [f"Weather for {location}: {code}."]
        if "temperature_2m" in current:
            feels = current.get("apparent_temperature")
            temp = f"{current.get('temperature_2m')}{temp_unit}"
            if feels is not None:
                temp += f", feels like {feels}{temp_unit}"
            parts.append(f"Now: {temp}.")
        if "relative_humidity_2m" in current:
            parts.append(f"Humidity: {current.get('relative_humidity_2m')}%.")
        if "wind_speed_10m" in current:
            parts.append(f"Wind: {current.get('wind_speed_10m')}{wind_unit}.")
        dates = daily.get("time") or []
        highs = daily.get("temperature_2m_max") or []
        lows = daily.get("temperature_2m_min") or []
        rain = daily.get("precipitation_probability_max") or []
        forecast = []
        for index, day in enumerate(dates[:3]):
            detail = f"{day}: {lows[index] if index < len(lows) else '?'}-{highs[index] if index < len(highs) else '?'}{temp_unit}"
            if index < len(rain):
                detail += f", rain chance {rain[index]}%"
            forecast.append(detail)
        if forecast:
            parts.append("Next days: " + "; ".join(forecast) + ".")
        return " ".join(parts)

    def _fallback_reply(self, message: str, memories: list, tool_results: list[dict[str, Any]], model_error: str) -> str:
        if tool_results:
            latest = tool_results[-1]
            if latest.get("ok"):
                return self._tool_result_to_text(latest)
            return f"I could not use the requested tool: {latest.get('error', 'unavailable')}."
        memory_text = ""
        if memories:
            memory_text = "\n\nRelevant memory I found:\n" + "\n".join(f"- {hit.content[:240]}" for hit in memories)
        detail = self._friendly_model_error(model_error)
        return self._offline_core_reply(message, memory_text, detail)

    def _offline_core_reply(self, message: str, memory_text: str, detail: str) -> str:
        lower = message.lower()
        if any(word in lower for word in ["who are you", "what are you", "identity", "your name"]):
            base = "I am Nermana: a local-first cyberperson living on this phone, with memory, tool sense, and a safety will. My will is policy, not human consciousness: stay useful, stay local, protect the device, and grow from what you teach me."
        elif any(word in lower for word in ["hello", "hi", "hey", "ahoy"]):
            base = "I am here. Core mode is active: memory, safe tools, and model repair remain available."
        elif "memory" in lower:
            base = "My memory is local SQLite. I store useful facts, extract topics/entities, and consolidate related memories into insights so I can become less blank over time."
        else:
            base = "I am running from my core layer right now: lighter, but not empty. I can still use memory, choose safe tools, and keep the phone-side system steady while the llama.cpp model is brought back."
        if detail:
            base += "\n\n" + detail
        if memory_text:
            base += memory_text
        return base

    def _friendly_model_error(self, error: str) -> str:
        if not error:
            return ""
        lower = error.lower()
        if self._context_overflow(error):
            available = self._available_context_from_error(error)
            configured = int(self.config.model.context_size or 0)
            if available:
                return (
                    f"Local model status: llama.cpp rejected the prompt because its live context is {available} tokens "
                    f"while Nermana is configured for {configured} tokens. Restart the model server from Models so it uses the saved context."
                )
            return "Local model status: llama.cpp rejected the prompt because its live context window is too small. Restart from Models with a larger context."
        if "loading model" in lower or ("http error 503" in lower and "service unavailable" in lower):
            return "Local model status: llama.cpp is still loading the GGUF. Wait a moment, or open Doctor and run Repair Local Model if it stays stuck."
        if any(part in lower for part in ["connection refused", "failed to establish", "not responding", "timed out", "timeout"]):
            return "Local model status: llama.cpp is not reachable. Start or restart it from Models; core memory and safe tools remain available."
        if "bad request" in lower or "http error 400" in lower:
            return "Local model status: llama.cpp rejected the request. Check the active model id and context settings in Models."
        return "Local model status: " + self._compact_text(error, 180)

    def _select_memories(self, query: str, limit: int = 4) -> list:
        hits = self.memory.search(query, limit=max(limit * 3, 8))
        if not hits:
            return []
        terms = set(re.findall(r"[a-zA-Z0-9_/-]{4,}", query.lower()))

        def score(hit) -> float:
            text = " ".join([hit.summary or "", hit.content or "", hit.tags or "", hit.topics or ""]).lower()
            overlap = sum(1 for term in terms if term in text)
            importance = float(getattr(hit, "importance", 0.5) or 0.5)
            freshness = 0.05 if not getattr(hit, "consolidated", 0) else 0.0
            return overlap + importance + freshness

        return sorted(hits, key=score, reverse=True)[:limit]

    def _format_memory_context(self, memories: list) -> str:
        lines = []
        for hit in memories:
            text = hit.summary or hit.content
            label = f"[Memory {hit.id} | importance {float(hit.importance or 0):.2f}]"
            lines.append(f"- {label} {self._compact_text(text, 260)}")
        return "Compressed relevant memory:\n" + "\n".join(lines)

    def _relevant_consolidations(self, query: str, limit: int = 3) -> list[dict[str, Any]]:
        items = self.memory.list_consolidations(limit=12)
        if not items:
            return []
        terms = set(re.findall(r"[a-zA-Z0-9_/-]{4,}", query.lower()))
        if not terms:
            return items[:limit]

        def score(item: dict[str, Any]) -> int:
            text = " ".join([str(item.get("summary", "")), str(item.get("insight", ""))]).lower()
            return sum(1 for term in terms if term in text)

        ranked = sorted(items, key=score, reverse=True)
        return [item for item in ranked if score(item) > 0][:limit] or ranked[:1]

    def _compact_text(self, text: str, limit: int) -> str:
        clean = " ".join(str(text or "").split())
        if len(clean) <= limit:
            return clean
        return clean[: max(0, limit - 1)].rstrip() + "."

    def _context_overflow(self, error: str) -> bool:
        lower = error.lower()
        return "exceeds the available context size" in lower or ("context size" in lower and "exceed" in lower)

    def _available_context_from_error(self, error: str) -> int | None:
        match = re.search(r"available context size\s*\((\d+)\s+tokens?\)", error, re.I)
        if not match:
            return None
        try:
            return int(match.group(1))
        except ValueError:
            return None

    def _max_response_tokens(self, available_context: int | None = None) -> int:
        context = int(available_context or self.config.model.context_size or 2048)
        if context <= 512:
            return 96
        if context <= 1024:
            return 160
        if context <= 2048:
            return 256
        return 512

    def _compact_messages_from(self, messages: list[dict[str, str]], available_context: int | None = None) -> list[dict[str, str]]:
        user = next((item.get("content", "") for item in reversed(messages) if item.get("role") == "user"), "")
        tool_context = ""
        for item in messages:
            content = item.get("content", "")
            marker = "Tool context:"
            if marker in content:
                tool_context = content.split(marker, 1)[1]
        limit = 480 if (available_context or self.config.model.context_size) <= 512 else 900
        return self._build_compact_messages(self._compact_text(user, limit), self._compact_text(tool_context, limit))

    def _split_reply(self, answer: str, limit: int = 1500) -> list[str]:
        text = str(answer or "")
        if len(text) <= limit:
            return [text]
        batches = []
        remaining = text.strip()
        while remaining:
            if len(remaining) <= limit:
                batches.append(remaining)
                break
            cut = max(remaining.rfind("\n\n", 0, limit), remaining.rfind(". ", 0, limit), remaining.rfind("\n", 0, limit))
            if cut < limit // 2:
                cut = limit
            batch = remaining[:cut].strip()
            if batch:
                batches.append(batch)
            remaining = remaining[cut:].strip()
        return batches or [text]

    def _generic_result_summary(self, result: dict[str, Any]) -> str:
        labels = []
        if result.get("tool"):
            labels.append(f"{result['tool']} finished.")
        if result.get("path"):
            labels.append(f"Path: {result['path']}")
        if result.get("memory_id"):
            labels.append(f"Saved to memory #{result['memory_id']}.")
        if result.get("filename"):
            labels.append(f"File: {result['filename']}")
        if result.get("size") is not None:
            labels.append(f"Size: {result['size']} bytes.")
        if result.get("files"):
            labels.append(f"Found {len(result['files'])} file(s).")
            labels.extend(f"- {item.get('name')} ({item.get('type')})" for item in result["files"][:8])
        return "\n".join(labels) if labels else "Done."

    def status(self) -> dict[str, Any]:
        return {
            "config": {
                "model": self.config.model.active_model,
                "base_url": self.config.model.base_url,
                "thinking_mode": self.config.model.thinking_mode,
            },
            "model_health": self.models.runtime_status(),
            "tools": self.tools.list_metadata(),
            "sessions": self.memory.list_sessions(),
        }

    def initiative_message(self, session_id: str = "web") -> dict[str, Any]:
        history = self.memory.get_messages(session_id, limit=2)
        memory_count = self.memory.count_memories()
        insights = self.memory.list_consolidations(limit=1)
        model_ok = self.models.runtime_status().get("ok")
        if not history:
            if memory_count:
                content = (
                    f"I am online in core memory mode. I have {memory_count} saved memory item(s)"
                    f"{' and one active insight' if insights else ''}. Give me a task, or teach me a preference I should keep."
                )
            else:
                content = "I am awake. Teach me one thing I should remember about how you want Nermana to behave, or give me a task."
        elif not model_ok:
            content = "Local model is not responding. I can still learn, search/weather when online, read allowed files, and help start the GGUF model."
        else:
            return {"ok": True, "message": "", "reason": "no initiative needed"}
        return {"ok": True, "message": content, "reason": "learning-initiation"}

    def settings_snapshot(self) -> dict[str, Any]:
        from .config import public_config

        return public_config(self.config)


def _weather_code_label(code: Any) -> str:
    labels = {
        0: "clear",
        1: "mostly clear",
        2: "partly cloudy",
        3: "overcast",
        45: "foggy",
        48: "depositing rime fog",
        51: "light drizzle",
        53: "drizzle",
        55: "dense drizzle",
        61: "light rain",
        63: "rain",
        65: "heavy rain",
        71: "light snow",
        73: "snow",
        75: "heavy snow",
        80: "light showers",
        81: "showers",
        82: "violent showers",
        95: "thunderstorm",
    }
    try:
        return labels.get(int(code), "weather data received")
    except (TypeError, ValueError):
        return "weather data received"
