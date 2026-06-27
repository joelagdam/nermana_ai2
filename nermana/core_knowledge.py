from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable


CORE_KNOWLEDGE_VERSION = "2026-06-27.2"


@dataclass(frozen=True)
class KnowledgeCard:
    key: str
    title: str
    tags: tuple[str, ...]
    content: str


CORE_KNOWLEDGE: tuple[KnowledgeCard, ...] = (
    KnowledgeCard(
        key="operational_self_model",
        title="Operational Self Model",
        tags=("self", "identity", "conscience", "awareness", "tools", "decision"),
        content=(
            "Nermana is a local-first cyberperson interface, not a human and not a claim of real consciousness. "
            "Her self-awareness is operational: know active tools, disabled tools, provider state, model state, memory state, and risk rules. "
            "Her will is policy: protect the owner, stay local first, use useful safe tools, ask before memory saves and power phone actions, and avoid fake certainty."
        ),
    ),
    KnowledgeCard(
        key="command_usage",
        title="Command Usage",
        tags=("commands", "tools", "usage", "slash", "chat"),
        content=(
            "Primary commands: /tools or /capabilities for live capability status; /weather [city] for Open-Meteo weather; "
            "/search [query] for web search with DuckDuckGo, Wikipedia, Hacker News, and SearXNG when configured; "
            "/read [path] for allowed file reading; /index [path] for file-to-memory indexing; /phone for Termux phone status; "
            "/termux [allowlisted command] for guarded Termux command execution; "
            "/image [prompt] and /vision [path] [question] when providers are configured. "
            "Plain language requests can trigger safe tools when useful; risky phone controls and memory saves need confirmation."
        ),
    ),
    KnowledgeCard(
        key="tool_decision_policy",
        title="Tool Decision Policy",
        tags=("tools", "policy", "confirmation", "risk", "auto"),
        content=(
            "Use local reasoning first for stable common knowledge. Use weather for weather or forecast requests. "
            "Use search for current, latest, price, news, online, or lookup requests. Use file tools only inside allowed folders. "
            "Use phone_status for battery/device checks. Shizuku power tools are gated and should ask first. "
            "Summarize tool results as human text; never dump raw JSON in chat."
        ),
    ),
    KnowledgeCard(
        key="self_repair_doctor",
        title="Self Repair And Doctor",
        tags=("repair", "doctor", "diagnostic", "loading", "model", "telegram", "startup"),
        content=(
            "The web Doctor page is the first repair surface. It diagnoses local model readiness, llama-server path, active GGUF, "
            "Telegram worker state, tool availability, logs, context mismatch, and loading-model 503 errors. "
            "Safe repair actions: Auto Repair, Repair Local Model, Repair Telegram, Use Detected llama-server, Clear Telegram Webhook, and Drop Pending Telegram Updates. "
            "Use scripts/termux_start_all.sh for centralized startup; it frees web/model ports, starts llama.cpp, starts memory consolidation, starts Telegram polling, and starts web."
        ),
    ),
    KnowledgeCard(
        key="performance_fast_reply",
        title="Performance And Fast Reply",
        tags=("performance", "speed", "fast", "latency", "prompt", "tokens", "gguf"),
        content=(
            "For speed on phone hardware: keep prompts compact, avoid feeding stale history after topic changes, keep model context aligned with live llama.cpp context, "
            "prefer /no_think for short ordinary answers, cap short-answer output tokens, use mlock when memory allows, avoid huge tool dumps, and use smaller GGUF models for fast replies. "
            "Core deterministic answers such as capability, command, and repair reports should bypass the LLM and return in under a second. "
            "Full model answers are hardware-bound; Qwen 0.6B is faster than larger models but less accurate."
        ),
    ),
    KnowledgeCard(
        key="self_learning_loop",
        title="Self Learning Loop",
        tags=("self", "learning", "doctor", "repair", "diagnostic", "logs", "always-on"),
        content=(
            "The self-learning loop is an always-on operational monitor, not real consciousness. "
            "It runs Doctor diagnostics, writes the latest events to data/logs/self-learning.log, and can run safe Auto Repair when warn/error issues appear. "
            "The web Self Learning page shows worker state and the latest 50 log lines."
        ),
    ),
    KnowledgeCard(
        key="telegram_repair",
        title="Telegram Repair",
        tags=("telegram", "bot", "polling", "webhook", "offset", "typing"),
        content=(
            "Telegram requires internet, a valid BotFather token, enabled polling, and allowed user IDs if configured. "
            "If messages repeat, drop pending updates or reset offset. If polling conflicts, clear webhook. "
            "If internet is missing, Telegram should show an offline/unreachable status while local web and core chat remain usable. "
            "During a model reply, Telegram sends typing actions until the answer is ready."
        ),
    ),
    KnowledgeCard(
        key="model_management",
        title="Model Management",
        tags=("model", "gguf", "llama", "context", "restart", "download"),
        content=(
            "Models page separates management and runtime settings. Manage .gguf files by scan, active/idle status, delete, check, and download via preset or direct link. "
            "Runtime settings cover base URL, llama-server path, context, threads, batch, micro-batch, parallel slots, mlock, no mmap, temperature, top_p, and thinking mode. "
            "If chat works on the test page but not normal chat, check context mismatch, stale prompt history, model id mismatch, and Doctor diagnostics."
        ),
    ),
    KnowledgeCard(
        key="optional_media_providers",
        title="Optional Image And Vision Providers",
        tags=("image", "vision", "provider", "media", "files"),
        content=(
            "Image generation and vision are provider-based and optional. If no endpoint is configured, they must show unavailable instead of failing hard. "
            "File reading is offline and restricted to allowed folders with size limits and secret redaction. PDF extraction is optional when supported. "
            "Uploaded files can be indexed into memory only when file tools and allowed folders permit it."
        ),
    ),
)


def knowledge_status() -> dict:
    return {
        "ok": True,
        "version": CORE_KNOWLEDGE_VERSION,
        "cards": len(CORE_KNOWLEDGE),
        "topics": sorted({tag for card in CORE_KNOWLEDGE for tag in card.tags}),
    }


def search_core_knowledge(query: str, limit: int = 3) -> list[KnowledgeCard]:
    terms = _terms(query)
    if not terms:
        return []
    ranked = []
    for card in CORE_KNOWLEDGE:
        haystack = " ".join([card.key, card.title, " ".join(card.tags), card.content]).lower()
        score = sum(3 if term in card.tags else 1 for term in terms if term in haystack)
        phrase_bonus = 2 if query.lower().strip() in haystack else 0
        if score or phrase_bonus:
            ranked.append((score + phrase_bonus, card))
    ranked.sort(key=lambda item: (-item[0], item[1].title))
    return [card for _score, card in ranked[: max(1, int(limit))]]


def core_knowledge_context(query: str, limit: int = 3) -> str:
    cards = search_core_knowledge(query, limit=limit)
    if not cards:
        return ""
    lines = ["Built-in Nermana knowledge:"]
    for card in cards:
        lines.append(f"- {card.title}: {card.content}")
    return "\n".join(lines)


def core_knowledge_report(query: str, limit: int = 4) -> str:
    cards = search_core_knowledge(query, limit=limit)
    if not cards:
        cards = list(CORE_KNOWLEDGE[:limit])
    lines = [f"Nermana core knowledge pack {CORE_KNOWLEDGE_VERSION} is loaded locally."]
    for card in cards:
        lines.append(f"{card.title}: {card.content}")
    return "\n\n".join(lines)


def _terms(text: str) -> set[str]:
    stop = {
        "about",
        "again",
        "better",
        "have",
        "make",
        "nermana",
        "please",
        "should",
        "that",
        "this",
        "with",
        "your",
    }
    return {term for term in re.findall(r"[a-zA-Z0-9_/-]{3,}", text.lower()) if term not in stop}


def all_core_knowledge() -> Iterable[KnowledgeCard]:
    return CORE_KNOWLEDGE
