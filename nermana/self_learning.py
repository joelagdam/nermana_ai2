from __future__ import annotations

import json
import time
from collections import deque
from pathlib import Path
from typing import Any

from .config import AppConfig, resolve_path


def self_learning_log_path(config: AppConfig) -> Path:
    return resolve_path(config.self_learning.log_path)


def append_self_learning_log(config: AppConfig, event: str, message: str, details: dict[str, Any] | None = None) -> None:
    path = self_learning_log_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    suffix = ""
    if details:
        compact = json.dumps(_compact_details(details), sort_keys=True, ensure_ascii=True)
        suffix = f" | {compact}"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(f"{stamp} | {event} | {message}{suffix}\n")


def tail_self_learning_log(config: AppConfig, limit: int | None = None) -> dict[str, Any]:
    path = self_learning_log_path(config)
    count = max(1, min(500, int(limit or config.self_learning.tail_lines or 50)))
    if not path.exists():
        return {"ok": True, "path": str(path), "lines": [], "count": 0}
    lines: deque[str] = deque(maxlen=count)
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            lines.append(line.rstrip("\n"))
    return {"ok": True, "path": str(path), "lines": list(lines), "count": len(lines)}


def _compact_details(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _compact_details(item) for key, item in list(value.items())[:12]}
    if isinstance(value, list):
        return [_compact_details(item) for item in value[:12]]
    if isinstance(value, str):
        return value if len(value) <= 220 else value[:217] + "..."
    return value
