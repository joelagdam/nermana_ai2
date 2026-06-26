from __future__ import annotations

import os
import re
import shutil
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable

from .config import AppConfig, resolve_path


FILENAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


@dataclass
class ModelPreset:
    id: str
    name: str
    size_hint: str
    notes: str
    url: str
    filename: str
    context_size: int = 4096
    thinking_mode: str = "auto"


MODEL_PRESETS = [
    ModelPreset(
        id="qwen3_06b_q8",
        name="Qwen3 0.6B Q8",
        size_hint="small official Qwen3",
        notes="Good first test model for lower-memory phones.",
        url="https://huggingface.co/Qwen/Qwen3-0.6B-GGUF/resolve/main/Qwen3-0.6B-Q8_0.gguf",
        filename="Qwen3-0.6B-Q8_0.gguf",
    ),
    ModelPreset(
        id="qwen3_17b_q8",
        name="Qwen3 1.7B Q8",
        size_hint="official Qwen3",
        notes="Higher quality but heavier than the Qwen2.5 Q4 preset.",
        url="https://huggingface.co/Qwen/Qwen3-1.7B-GGUF/resolve/main/Qwen3-1.7B-Q8_0.gguf",
        filename="Qwen3-1.7B-Q8_0.gguf",
    ),
    ModelPreset(
        id="qwen25_15b_instruct_q4",
        name="Qwen2.5 1.5B Instruct Q4",
        size_hint="recommended mobile balance",
        notes="Smaller Q4 preset for mid-range phones.",
        url="https://huggingface.co/Qwen/Qwen2.5-1.5B-Instruct-GGUF/resolve/main/qwen2.5-1.5b-instruct-q4_k_m.gguf",
        filename="qwen2.5-1.5b-instruct-q4_k_m.gguf",
        thinking_mode="no_think",
    ),
]


def list_presets() -> list[dict[str, Any]]:
    return [asdict(preset) for preset in MODEL_PRESETS]


def get_preset(preset_id: str) -> ModelPreset | None:
    return next((preset for preset in MODEL_PRESETS if preset.id == preset_id), None)


ProgressCallback = Callable[[dict[str, Any]], None]
CancelCallback = Callable[[], bool]


def download_model(
    config: AppConfig,
    url: str,
    filename: str = "",
    select: bool = False,
    progress: ProgressCallback | None = None,
    cancelled: CancelCallback | None = None,
) -> dict[str, Any]:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return {"ok": False, "error": "Only http and https model links are allowed."}
    target_name = _safe_filename(filename or Path(urllib.parse.unquote(parsed.path)).name)
    if not target_name:
        return {"ok": False, "error": "Could not determine a file name for this model."}
    if not target_name.lower().endswith(".gguf"):
        return {"ok": False, "error": "Only .gguf model downloads are allowed."}

    models_dir = resolve_path(config.model.models_dir)
    models_dir.mkdir(parents=True, exist_ok=True)
    target = models_dir / target_name
    if target.exists() and target.stat().st_size > 0:
        size = target.stat().st_size
        _progress(progress, target_name, size, size)
        if select:
            config.model.active_model = target.name
        return {"ok": True, "skipped": True, "path": str(target), "filename": target.name, "size_bytes": size, "total_bytes": size}

    partial = target.with_suffix(target.suffix + ".part")
    resume_from = partial.stat().st_size if partial.exists() else 0
    headers = {"User-Agent": "Nermana-Termux/0.1"}
    if resume_from:
        headers["Range"] = f"bytes={resume_from}-"
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            resume_active = bool(resume_from and getattr(response, "status", 200) == 206)
            if not resume_active:
                resume_from = 0
            total = _download_total(response, resume_from)
            downloaded = resume_from
            free_check = _free_space_check(models_dir, max(0, total - downloaded))
            if free_check:
                return {**free_check, "partial_path": str(partial), "resume_available": partial.exists()}
            _progress(progress, target_name, downloaded, total)
            mode = "ab" if resume_active else "wb"
            with partial.open(mode) as handle:
                while True:
                    if cancelled and cancelled():
                        return {
                            "ok": False,
                            "cancelled": True,
                            "error": "download cancelled",
                            "partial_path": str(partial),
                            "resume_available": partial.exists(),
                            "filename": target.name,
                            "bytes_read": downloaded,
                            "total_bytes": total,
                        }
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    handle.write(chunk)
                    downloaded += len(chunk)
                    _progress(progress, target_name, downloaded, total)
        os.replace(partial, target)
    except Exception as exc:
        return {
            "ok": False,
            "error": str(exc),
            "partial_path": str(partial),
            "resume_available": partial.exists(),
            "filename": target.name,
            "bytes_read": partial.stat().st_size if partial.exists() else 0,
        }

    if select:
        config.model.active_model = target.name
    size = target.stat().st_size
    _progress(progress, target_name, size, size)
    return {"ok": True, "skipped": False, "path": str(target), "filename": target.name, "size_bytes": size, "total_bytes": size}


def download_preset(
    config: AppConfig,
    preset_id: str,
    select: bool = True,
    progress: ProgressCallback | None = None,
    cancelled: CancelCallback | None = None,
) -> dict[str, Any]:
    preset = get_preset(preset_id)
    if preset is None:
        return {"ok": False, "error": f"Unknown preset: {preset_id}"}
    result = download_model(config, preset.url, preset.filename, select=select, progress=progress, cancelled=cancelled)
    result["preset"] = asdict(preset)
    if result.get("ok"):
        config.model.context_size = preset.context_size
        config.model.thinking_mode = preset.thinking_mode
    return result


def list_partial_downloads(config: AppConfig) -> list[dict[str, Any]]:
    models_dir = resolve_path(config.model.models_dir)
    if not models_dir.exists():
        return []
    partials = []
    for path in sorted(models_dir.glob("*.gguf.part")):
        try:
            size = path.stat().st_size
        except OSError:
            continue
        target_name = path.name.removesuffix(".part")
        partials.append({"filename": target_name, "partial_name": path.name, "path": str(path), "size_bytes": size})
    return partials


def delete_partial_download(config: AppConfig, filename: str) -> dict[str, Any]:
    target_name = _safe_filename(filename)
    if not target_name.lower().endswith(".gguf"):
        return {"ok": False, "error": "partial filename must end with .gguf"}
    models_dir = resolve_path(config.model.models_dir).resolve()
    partial = (models_dir / f"{target_name}.part").resolve()
    if not (partial == models_dir or models_dir in partial.parents):
        return {"ok": False, "error": "partial path is outside the models folder"}
    if not partial.exists():
        return {"ok": False, "error": "partial download not found"}
    try:
        size = partial.stat().st_size
        partial.unlink()
    except OSError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "deleted": partial.name, "size_bytes": size}


def _safe_filename(name: str) -> str:
    name = Path(name).name.strip()
    return FILENAME_RE.sub("_", name)


def _content_length(response: Any) -> int:
    value = response.headers.get("Content-Length", "")
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _download_total(response: Any, resume_from: int) -> int:
    content_range = response.headers.get("Content-Range", "")
    match = re.search(r"/(\d+)$", content_range)
    if match:
        try:
            return int(match.group(1))
        except ValueError:
            pass
    length = _content_length(response)
    return length + resume_from if resume_from and length else length


def _free_space_check(folder: Path, remaining_bytes: int) -> dict[str, Any] | None:
    if not remaining_bytes:
        return None
    try:
        free = shutil.disk_usage(folder).free
    except OSError:
        return None
    buffer = min(256 * 1024 * 1024, max(32 * 1024 * 1024, remaining_bytes // 20))
    required = remaining_bytes + buffer
    if free >= required:
        return None
    return {
        "ok": False,
        "error": f"not enough free storage: need about {required} bytes, have {free} bytes",
        "free_bytes": free,
        "required_bytes": required,
    }


def _progress(progress: ProgressCallback | None, filename: str, bytes_read: int, total_bytes: int) -> None:
    if progress is None:
        return
    percent = round((bytes_read / total_bytes) * 100, 1) if total_bytes else 0
    progress(
        {
            "filename": filename,
            "bytes_read": bytes_read,
            "total_bytes": total_bytes,
            "percent": percent,
        }
    )
