from __future__ import annotations

from urllib.parse import urljoin

from nermana.config import AppConfig
from nermana.http_client import get_json
from nermana.tooling import Tool, ToolRegistry


def register_search_tools(registry: ToolRegistry, config: AppConfig) -> None:
    def available() -> tuple[bool, str]:
        if not config.search.enabled:
            return False, "search disabled"
        if config.search.provider != "searxng":
            return False, f"unsupported provider: {config.search.provider}"
        if not config.search.searxng_url:
            return False, "set a SearXNG URL in Providers"
        return True, "configured"

    def web_search(payload: dict) -> dict:
        query = str(payload.get("query", "")).strip()
        if not query:
            return {"ok": False, "error": "query is required"}
        base = config.search.searxng_url.rstrip("/") + "/"
        response = get_json(
            urljoin(base, "search"),
            {
                "q": query,
                "format": "json",
                "safesearch": config.search.safe_search,
                "pageno": int(payload.get("page", 1)),
            },
            timeout=config.search.timeout_seconds,
        )
        if not response.ok:
            return {"ok": False, "error": f"search unavailable: {response.error}"}
        results = []
        for item in response.data.get("results", [])[: config.search.max_results]:
            results.append(
                {
                    "title": item.get("title", ""),
                    "url": item.get("url", ""),
                    "content": item.get("content", "") or item.get("snippet", ""),
                    "engine": item.get("engine", ""),
                }
            )
        return {"ok": True, "query": query, "results": results}

    registry.register(
        Tool(
            name="web_search",
            description="Search the web through a configured SearXNG JSON endpoint.",
            provider="searxng",
            input_schema={"type": "object", "properties": {"query": {"type": "string"}, "page": {"type": "integer"}}},
            output_schema={"type": "object", "properties": {"results": {"type": "array"}}},
            online_required=True,
            risk="read",
            timeout_seconds=config.search.timeout_seconds,
            handler=web_search,
            availability=available,
        )
    )
