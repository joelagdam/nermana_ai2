from __future__ import annotations

import os
import re
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

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


def download_model(config: AppConfig, url: str, filename: str = "", select: bool = False) -> dict[str, Any]:
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
        if select:
            config.model.active_model = target.name
        return {"ok": True, "skipped": True, "path": str(target), "filename": target.name, "size_bytes": target.stat().st_size}

    partial = target.with_suffix(target.suffix + ".part")
    request = urllib.request.Request(url, headers={"User-Agent": "Nermana-Termux/0.1"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response, partial.open("wb") as handle:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                handle.write(chunk)
        os.replace(partial, target)
    except Exception as exc:
        if partial.exists():
            partial.unlink()
        return {"ok": False, "error": str(exc)}

    if select:
        config.model.active_model = target.name
    return {"ok": True, "skipped": False, "path": str(target), "filename": target.name, "size_bytes": target.stat().st_size}


def download_preset(config: AppConfig, preset_id: str, select: bool = True) -> dict[str, Any]:
    preset = get_preset(preset_id)
    if preset is None:
        return {"ok": False, "error": f"Unknown preset: {preset_id}"}
    result = download_model(config, preset.url, preset.filename, select=select)
    result["preset"] = asdict(preset)
    if result.get("ok"):
        config.model.context_size = preset.context_size
        config.model.thinking_mode = preset.thinking_mode
    return result


def _safe_filename(name: str) -> str:
    name = Path(name).name.strip()
    return FILENAME_RE.sub("_", name)
