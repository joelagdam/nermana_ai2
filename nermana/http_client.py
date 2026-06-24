from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any


@dataclass
class HttpResponse:
    ok: bool
    status: int
    data: Any
    error: str = ""


def get_json(url: str, params: dict[str, Any] | None = None, timeout: float = 10.0, headers: dict[str, str] | None = None) -> HttpResponse:
    if params:
        separator = "&" if "?" in url else "?"
        url = f"{url}{separator}{urllib.parse.urlencode(params)}"
    request = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = response.read().decode("utf-8", errors="replace")
            return HttpResponse(True, response.status, json.loads(payload))
    except urllib.error.HTTPError as exc:
        return HttpResponse(False, exc.code, None, str(exc))
    except Exception as exc:  # Network failures are expected on an offline-first phone.
        return HttpResponse(False, 0, None, str(exc))


def post_json(url: str, payload: dict[str, Any], timeout: float = 10.0, headers: dict[str, str] | None = None) -> HttpResponse:
    body = json.dumps(payload).encode("utf-8")
    merged_headers = {"Content-Type": "application/json"}
    merged_headers.update(headers or {})
    request = urllib.request.Request(url, data=body, headers=merged_headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            text = response.read().decode("utf-8", errors="replace")
            return HttpResponse(True, response.status, json.loads(text) if text else {})
    except urllib.error.HTTPError as exc:
        return HttpResponse(False, exc.code, None, str(exc))
    except Exception as exc:
        return HttpResponse(False, 0, None, str(exc))
