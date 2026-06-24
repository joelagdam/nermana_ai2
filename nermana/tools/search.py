from __future__ import annotations

import urllib.error
import urllib.parse
import urllib.request
from html import unescape
from html.parser import HTMLParser
from urllib.parse import urljoin

from nermana.config import AppConfig
from nermana.http_client import get_json
from nermana.tooling import Tool, ToolRegistry


def register_search_tools(registry: ToolRegistry, config: AppConfig) -> None:
    def available() -> tuple[bool, str]:
        if not config.search.enabled:
            return False, "search disabled"
        provider = config.search.provider.lower()
        if provider in {"auto", "duckduckgo"}:
            return True, "configured"
        if provider == "searxng" and config.search.searxng_url:
            return True, "configured"
        if provider == "searxng":
            return True, "using DuckDuckGo fallback; set a SearXNG URL for SearXNG"
        return False, f"unsupported provider: {config.search.provider}"

    def web_search(payload: dict) -> dict:
        query = str(payload.get("query", "")).strip()
        if not query:
            return {"ok": False, "error": "query is required"}
        provider = config.search.provider.lower()
        page = int(payload.get("page", 1))
        if provider == "auto" and config.search.searxng_url:
            result = _search_searxng(config, query, page)
            if result.get("ok"):
                return result
            fallback = _search_duckduckgo(config, query)
            fallback["fallback_error"] = result.get("error", "")
            return fallback
        if provider == "searxng" and config.search.searxng_url:
            return _search_searxng(config, query, page)
        if provider in {"auto", "duckduckgo", "searxng"}:
            return _search_duckduckgo(config, query)
        return {"ok": False, "error": f"unsupported provider: {config.search.provider}"}

    registry.register(
        Tool(
            name="web_search",
            description="Search the web through DuckDuckGo or a configured SearXNG JSON endpoint.",
            provider=config.search.provider,
            input_schema={"type": "object", "properties": {"query": {"type": "string"}, "page": {"type": "integer"}}},
            output_schema={"type": "object", "properties": {"results": {"type": "array"}}},
            online_required=True,
            risk="read",
            timeout_seconds=config.search.timeout_seconds,
            handler=web_search,
            availability=available,
        )
    )


def _search_searxng(config: AppConfig, query: str, page: int) -> dict:
    base = config.search.searxng_url.rstrip("/") + "/"
    response = get_json(
        urljoin(base, "search"),
        {
            "q": query,
            "format": "json",
            "safesearch": config.search.safe_search,
            "pageno": page,
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
                "engine": item.get("engine", "searxng"),
            }
        )
    return {"ok": True, "provider": "searxng", "query": query, "results": results}


def _search_duckduckgo(config: AppConfig, query: str) -> dict:
    params = urllib.parse.urlencode({"q": query})
    request = urllib.request.Request(
        f"https://html.duckduckgo.com/html/?{params}",
        headers={
            "User-Agent": "Nermana-Termux/0.1",
            "Accept": "text/html,application/xhtml+xml",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=config.search.timeout_seconds) as response:
            html = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return {"ok": False, "error": f"search unavailable: HTTP {exc.code}"}
    except Exception as exc:
        return {"ok": False, "error": f"search unavailable: {exc}"}

    parser = DuckDuckGoParser(config.search.max_results)
    parser.feed(html)
    parser.close()
    return {"ok": True, "provider": "duckduckgo", "query": query, "results": parser.results}


class DuckDuckGoParser(HTMLParser):
    def __init__(self, max_results: int):
        super().__init__()
        self.max_results = max(1, max_results)
        self.results: list[dict] = []
        self.current: dict | None = None
        self.in_title = False
        self.in_snippet = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr = {key: value or "" for key, value in attrs}
        classes = set(attr.get("class", "").split())
        if tag == "a" and ("result__a" in classes or "result-link" in classes):
            self._finish_current()
            self.current = {"title": "", "url": _clean_duckduckgo_url(attr.get("href", "")), "content": "", "engine": "duckduckgo"}
            self.in_title = True
        elif "result__snippet" in classes or "result-snippet" in classes:
            self.in_snippet = True

    def handle_data(self, data: str) -> None:
        if self.current is None:
            return
        text = " ".join(unescape(data).split())
        if not text:
            return
        if self.in_title:
            self.current["title"] = f"{self.current['title']} {text}".strip()
        elif self.in_snippet:
            self.current["content"] = f"{self.current['content']} {text}".strip()

    def handle_endtag(self, tag: str) -> None:
        if tag == "a":
            self.in_title = False
        if self.in_snippet and tag in {"a", "td", "div"}:
            self.in_snippet = False

    def close(self) -> None:
        super().close()
        self._finish_current()

    def _finish_current(self) -> None:
        if len(self.results) >= self.max_results:
            self.current = None
            self.in_title = False
            self.in_snippet = False
            return
        if self.current and self.current.get("title") and self.current.get("url"):
            self.results.append(self.current)
        self.current = None
        self.in_title = False
        self.in_snippet = False


def _clean_duckduckgo_url(href: str) -> str:
    href = unescape(href)
    if href.startswith("//"):
        href = f"https:{href}"
    parsed = urllib.parse.urlparse(href)
    query = urllib.parse.parse_qs(parsed.query)
    if "uddg" in query and query["uddg"]:
        return query["uddg"][0]
    return href
