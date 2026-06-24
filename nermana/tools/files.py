from __future__ import annotations

import mimetypes
from pathlib import Path

from nermana.config import AppConfig, resolve_path
from nermana.memory import MemoryStore
from nermana.tooling import Tool, ToolRegistry


def register_file_tools(registry: ToolRegistry, config: AppConfig, memory: MemoryStore) -> None:
    def available() -> tuple[bool, str]:
        if not config.files.enabled:
            return False, "file tools disabled"
        return True, f"{len(config.files.allowed_dirs)} allowed folders"

    def list_files(payload: dict) -> dict:
        folder = _safe_path(config, str(payload.get("path", config.files.allowed_dirs[0])))
        if not folder.exists() or not folder.is_dir():
            return {"ok": False, "error": "folder not found"}
        files = []
        for item in sorted(folder.iterdir()):
            files.append(
                {
                    "name": item.name,
                    "path": str(item),
                    "type": "dir" if item.is_dir() else "file",
                    "size": item.stat().st_size if item.is_file() else 0,
                }
            )
        return {"ok": True, "path": str(folder), "files": files}

    def read_file(payload: dict) -> dict:
        path = _safe_path(config, str(payload.get("path", "")))
        if not path.exists() or not path.is_file():
            return {"ok": False, "error": "file not found"}
        max_bytes = int(config.files.max_read_mb) * 1024 * 1024
        if path.stat().st_size > max_bytes:
            return {"ok": False, "error": f"file exceeds {config.files.max_read_mb} MB limit"}
        content_type = mimetypes.guess_type(path.name)[0] or "text/plain"
        if path.suffix.lower() == ".pdf":
            text = _read_pdf(path)
        else:
            text = path.read_text(encoding="utf-8", errors="replace")
        return {"ok": True, "path": str(path), "content_type": content_type, "content": text}

    def index_file(payload: dict) -> dict:
        result = read_file(payload)
        if not result.get("ok"):
            return result
        memory_id = memory.remember(result["content"], tags="file,indexed", source=result["path"])
        return {"ok": True, "memory_id": memory_id, "path": result["path"]}

    registry.register(
        Tool(
            name="list_files",
            description="List files inside an allowed folder.",
            provider="filesystem",
            input_schema={"type": "object", "properties": {"path": {"type": "string"}}},
            offline_required=True,
            risk="read",
            handler=list_files,
            availability=available,
        )
    )
    registry.register(
        Tool(
            name="read_file",
            description="Read text or PDF content from an allowed folder.",
            provider="filesystem",
            input_schema={"type": "object", "properties": {"path": {"type": "string"}}},
            offline_required=True,
            risk="read",
            handler=read_file,
            availability=available,
        )
    )
    registry.register(
        Tool(
            name="index_file",
            description="Read a file and save its content to long-term memory.",
            provider="filesystem",
            input_schema={"type": "object", "properties": {"path": {"type": "string"}}},
            offline_required=True,
            risk="read",
            handler=index_file,
            availability=available,
        )
    )


def _safe_path(config: AppConfig, raw_path: str) -> Path:
    if not raw_path:
        raise ValueError("path is required")
    candidate = resolve_path(raw_path).resolve()
    allowed = [resolve_path(folder).resolve() for folder in config.files.allowed_dirs]
    if not any(candidate == root or root in candidate.parents for root in allowed):
        raise PermissionError("path is outside allowed folders")
    return candidate


def _read_pdf(path: Path) -> str:
    try:
        from pypdf import PdfReader
    except Exception as exc:
        raise RuntimeError("PDF reading needs the optional pypdf package") from exc
    reader = PdfReader(str(path))
    return "\n".join(page.extract_text() or "" for page in reader.pages)
