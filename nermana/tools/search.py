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
        if provider in {"auto", "duckduckgo", "wikipedia", "hackernews"}:
            return True, "configured with offline-safe fallbacks"
        if provider == "searxng" and config.search.searxng_url:
            return True, "configured"
        if provider == "searxng":
            return True, "using DuckDuckGo/Wikipedia fallback; set a SearXNG URL for SearXNG"
        return False, f"unsupported provider: {config.search.provider}"

    def web_search(payload: dict) -> dict:
        query = str(payload.get("query", "")).strip()
        if not query:
            return {"ok": False, "error": "query is required"}
        provider = config.search.provider.lower()
        page = int(payload.get("page", 1))
        if provider == "auto" and config.search.searxng_url:
            result = _search_searxng(config, query, page)
            if result.get("ok") and result.get("results"):
                return result
            fallback = _search_no_key_chain(config, query)
            fallback["fallback_error"] = result.get("error", "SearXNG returned no results")
            return fallback
        if provider == "searxng" and config.search.searxng_url:
            result = _search_searxng(config, query, page)
            if result.get("ok") and result.get("results"):
                return result
            fallback = _search_no_key_chain(config, query)
            fallback["fallback_error"] = result.get("error", "SearXNG returned no results")
            return fallback
        if provider in {"auto", "duckduckgo", "searxng"}:
            return _search_no_key_chain(config, query)
        if provider == "wikipedia":
            return _search_wikipedia(config, query)
        if provider == "hackernews":
            return _search_hackernews(config, query)
        return {"ok": False, "error": f"unsupported provider: {config.search.provider}"}

    registry.register(
        Tool(
            name="web_search",
            description="Search through SearXNG, DuckDuckGo, Wikipedia, and Hacker News fallbacks.",
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


def _search_no_key_chain(config: AppConfig, query: str) -> dict:
    duck = _search_duckduckgo(config, query)
    if duck.get("ok") and duck.get("results"):
        return duck
    wiki = _search_wikipedia(config, query)
    if wiki.get("ok") and wiki.get("results"):
        wiki["fallback_error"] = duck.get("error", "DuckDuckGo returned no results")
        return wiki
    hn = _search_hackernews(config, query)
    if hn.get("ok") and hn.get("results"):
        hn["fallback_error"] = "; ".join(
            [
                duck.get("error", "DuckDuckGo returned no results"),
                wiki.get("error", "Wikipedia returned no results"),
            ]
        )
        return hn
    errors = [
        duck.get("error", "DuckDuckGo returned no results"),
        wiki.get("error", "Wikipedia returned no results"),
        hn.get("error", "Hacker News returned no results"),
    ]
    return {"ok": False, "provider": "auto", "query": query, "results": [], "error": "search unavailable: " + "; ".join(errors)}


def _search_duckduckgo(config: AppConfig, query: str) -> dict:
    errors = []
    for url in _duckduckgo_urls(query):
        html_result = _fetch_duckduckgo_html(config, url)
        if not html_result.get("ok"):
            errors.append(html_result.get("error", "search unavailable"))
            continue
        parser = DuckDuckGoParser(config.search.max_results)
        parser.feed(html_result["html"])
        parser.close()
        if parser.results:
            return {"ok": True, "provider": "duckduckgo", "query": query, "results": parser.results}
        errors.append("no parseable DuckDuckGo HTML results")
    instant = _search_duckduckgo_instant(config, query)
    if instant.get("ok") and instant.get("results"):
        if errors:
            instant["fallback_error"] = "; ".join(errors[:2])
        return instant
    if instant.get("error"):
        errors.append(instant["error"])
    return {"ok": False, "error": "search unavailable: " + "; ".join(errors[:3])}


def _duckduckgo_urls(query: str) -> list[str]:
    params = urllib.parse.urlencode({"q": query})
    return [
        f"https://html.duckduckgo.com/html/?{params}",
        f"https://lite.duckduckgo.com/lite/?{params}",
    ]


def _fetch_duckduckgo_html(config: AppConfig, url: str) -> dict:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (Linux; Android; Termux) Nermana/0.1",
            "Accept": "text/html,application/xhtml+xml",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=config.search.timeout_seconds) as response:
            return {"ok": True, "html": response.read().decode("utf-8", errors="replace")}
    except urllib.error.HTTPError as exc:
        return {"ok": False, "error": f"HTTP {exc.code}"}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def _search_duckduckgo_instant(config: AppConfig, query: str) -> dict:
    response = get_json(
        "https://api.duckduckgo.com/",
        {"q": query, "format": "json", "no_html": 1, "skip_disambig": 1},
        timeout=config.search.timeout_seconds,
        headers={"User-Agent": "Nermana-Termux/0.1"},
    )
    if not response.ok:
        return {"ok": False, "error": f"instant answer unavailable: {response.error}"}
    results = []
    abstract = response.data.get("AbstractText") or response.data.get("Abstract")
    if abstract:
        results.append(
            {
                "title": response.data.get("Heading") or query,
                "url": response.data.get("AbstractURL") or "",
                "content": abstract,
                "engine": "duckduckgo-instant",
            }
        )
    for topic in _instant_topics(response.data.get("RelatedTopics") or []):
        if len(results) >= config.search.max_results:
            break
        results.append(topic)
    return {"ok": True, "provider": "duckduckgo-instant", "query": query, "results": results[: config.search.max_results]}


def _search_wikipedia(config: AppConfig, query: str) -> dict:
    response = get_json(
        "https://en.wikipedia.org/w/api.php",
        {
            "action": "query",
            "format": "json",
            "generator": "search",
            "gsrsearch": query,
            "gsrlimit": config.search.max_results,
            "prop": "extracts|info",
            "exintro": 1,
            "explaintext": 1,
            "inprop": "url",
        },
        timeout=config.search.timeout_seconds,
        headers={"User-Agent": "Nermana-Termux/0.1"},
    )
    if not response.ok:
        return {"ok": False, "provider": "wikipedia", "query": query, "results": [], "error": f"wikipedia unavailable: {response.error}"}
    pages = response.data.get("query", {}).get("pages", {})
    results = []
    for page in pages.values():
        title = page.get("title", "")
        extract = " ".join(str(page.get("extract", "")).split())
        url = page.get("fullurl") or f"https://en.wikipedia.org/wiki/{urllib.parse.quote(title.replace(' ', '_'))}"
        if title:
            results.append({"title": title, "url": url, "content": extract, "engine": "wikipedia"})
    return {"ok": True, "provider": "wikipedia", "query": query, "results": results[: config.search.max_results]}


def _search_hackernews(config: AppConfig, query: str) -> dict:
    response = get_json(
        "https://hn.algolia.com/api/v1/search",
        {"query": query, "tags": "story", "hitsPerPage": config.search.max_results},
        timeout=config.search.timeout_seconds,
        headers={"User-Agent": "Nermana-Termux/0.1"},
    )
    if not response.ok:
        return {"ok": False, "provider": "hackernews", "query": query, "results": [], "error": f"hackernews unavailable: {response.error}"}
    results = []
    for item in (response.data or {}).get("hits", [])[: config.search.max_results]:
        title = item.get("title") or item.get("story_title") or ""
        if not title:
            continue
        url = item.get("url") or f"https://news.ycombinator.com/item?id={item.get('objectID', '')}"
        points = item.get("points")
        comments = item.get("num_comments")
        details = []
        if points is not None:
            details.append(f"{points} points")
        if comments is not None:
            details.append(f"{comments} comments")
        author = item.get("author")
        if author:
            details.append(f"by {author}")
        results.append({"title": title, "url": url, "content": ", ".join(details), "engine": "hackernews"})
    return {"ok": True, "provider": "hackernews", "query": query, "results": results}


def _instant_topics(items: list) -> list[dict]:
    results = []
    for item in items:
        if "Topics" in item:
            results.extend(_instant_topics(item.get("Topics") or []))
            continue
        text = item.get("Text")
        if not text:
            continue
        results.append(
            {
                "title": text.split(" - ", 1)[0][:100],
                "url": item.get("FirstURL", ""),
                "content": text,
                "engine": "duckduckgo-instant",
            }
        )
    return results


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
        elif tag == "a" and attr.get("href") and self.current is None and len(self.results) < self.max_results:
            href = attr.get("href", "")
            if not href.startswith("/") and ("duckduckgo.com" not in href or "uddg=" in href):
                self.current = {"title": "", "url": _clean_duckduckgo_url(href), "content": "", "engine": "duckduckgo"}
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
        title = (self.current or {}).get("title", "").strip()
        if title.lower() in {"next", "next page", "previous", "previous page", "home"}:
            self.current = None
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
