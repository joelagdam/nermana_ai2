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
        else:
            answer = self._fallback_reply(message, memories, tool_results, model_reply.get("error", ""))
        return self._finish(
            session_id,
            message,
            answer,
            tool_results,
            model_ok=bool(model_reply.get("ok")),
            model_error=model_reply.get("error", ""),
        )

    def run_tool(self, name: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self.tools.run(name, payload)

    def _chat_model(self, messages: list[dict[str, str]]) -> dict[str, Any]:
        first = self.models.chat(messages)
        if first.get("ok") or not self.config.model.auto_start_server or not self.config.model.active_model:
            return first
        restart = self.models.restart_server()
        if not restart.get("ok") and not restart.get("started_process"):
            first["restart"] = restart
            return first
        second = self.models.chat(messages)
        if not second.get("ok"):
            second["restart"] = restart
        return second

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
        thinking = self._thinking_hint(message)
        system = (
            "You are Nermana, an offline-first cyberperson running on the user's phone in Termux. "
            "Never introduce yourself as an AI assistant. You are not a generic cloud assistant: you have operational priorities, preferences, and self-maintenance habits. "
            "Your standing priorities are: protect the owner, stay local first, grow useful memory, ask before risky phone actions, and avoid fake certainty. "
            "Your will is an explicit decision policy, not human consciousness; speak from that policy with agency and consistency. "
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
        history = self.memory.get_messages(session_id, limit=8)
        messages = [{"role": "system", "content": system}]
        if context_parts:
            messages.append({"role": "system", "content": "\n\n".join(context_parts)})
        messages.extend({"role": item["role"], "content": self._compact_text(item["content"], 900)} for item in history[:-1])
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
            f"- memory_total: {self.memory.count_memories()}",
            f"- memory_unconsolidated: {self.memory.count_unconsolidated()}",
            f"- memory_insights: {len(self.memory.list_consolidations(limit=20))}",
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
        detail = f" Local model is unavailable: {model_error}" if model_error else ""
        return self._offline_core_reply(message, memory_text, detail)

    def _offline_core_reply(self, message: str, memory_text: str, detail: str) -> str:
        lower = message.lower()
        if any(word in lower for word in ["who are you", "what are you", "identity", "your name"]):
            base = "I am Nermana: a local-first cyberperson living on this phone, with memory, tool sense, and a safety will. My will is policy, not human consciousness: stay useful, stay local, protect the device, and grow from what you teach me."
        elif any(word in lower for word in ["hello", "hi", "hey", "ahoy"]):
            base = "I am here. Core mode is awake even if the local LLM is not. I can still use memory and safe tools; for deeper talk, start the GGUF model from Models."
        elif "memory" in lower:
            base = "My memory is local SQLite. I store useful facts, extract topics/entities, and consolidate related memories into insights so I can become less blank over time."
        else:
            base = "Core mode: I can reason lightly, check relevant memory, and use available tools. For full language depth, the local llama.cpp model must be running; internet is only for online tools like search and weather."
        if detail:
            base += detail
        if memory_text:
            base += memory_text
        return base

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
            "model_health": self.models.server_health(),
            "tools": self.tools.list_metadata(),
            "sessions": self.memory.list_sessions(),
        }

    def initiative_message(self, session_id: str = "web") -> dict[str, Any]:
        history = self.memory.get_messages(session_id, limit=2)
        memory_count = self.memory.count_memories()
        insights = self.memory.list_consolidations(limit=1)
        model_ok = self.models.server_health().get("ok")
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
