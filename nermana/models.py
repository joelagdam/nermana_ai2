from __future__ import annotations

import subprocess
import time
import shutil
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import AppConfig, resolve_path, save_config
from .http_client import get_json, post_json


@dataclass
class ModelInfo:
    name: str
    path: str
    size_mb: float
    active: bool
    loadable: bool


class ModelManager:
    def __init__(self, config: AppConfig, persist: bool = True):
        self.config = config
        self.persist = persist
        self._process: subprocess.Popen | None = None

    @property
    def models_dir(self) -> Path:
        return resolve_path(self.config.model.models_dir)

    def scan(self) -> list[ModelInfo]:
        self.models_dir.mkdir(parents=True, exist_ok=True)
        models: list[ModelInfo] = []
        for path in sorted(self.models_dir.iterdir()):
            suffix = path.suffix.lower()
            if suffix not in {".gguf", ".guff"}:
                continue
            models.append(
                ModelInfo(
                    name=path.name,
                    path=str(path),
                    size_mb=round(path.stat().st_size / (1024 * 1024), 2),
                    active=path.name == self.config.model.active_model,
                    loadable=suffix == ".gguf",
                )
            )
        return models

    def active_path(self) -> Path | None:
        if not self.config.model.active_model:
            return None
        candidate = self.models_dir / self.config.model.active_model
        return candidate if candidate.exists() else None

    def llama_server_status(self) -> dict[str, Any]:
        configured = self.config.model.llama_server_path or "auto"
        resolved = self.resolve_llama_server()
        return {
            "configured": configured,
            "resolved": resolved,
            "available": resolved is not None,
            "candidates": [str(path) for path in self.llama_server_candidates()],
        }

    def resolve_llama_server(self) -> str | None:
        configured = (self.config.model.llama_server_path or "auto").strip()
        if configured not in {"", "auto"}:
            if "/" in configured or "\\" in configured or configured.startswith("~"):
                path = Path(configured).expanduser()
                if path.exists():
                    return str(path)
                resolved = resolve_path(configured)
                if resolved.exists():
                    return str(resolved)
                return None
            found = shutil.which(configured)
            if found:
                return found
        found = shutil.which("llama-server")
        if found:
            return found
        for candidate in self.llama_server_candidates():
            if candidate.exists():
                return str(candidate)
        return None

    def llama_server_candidates(self) -> list[Path]:
        home = Path.home()
        return [
            home / "llama.cpp" / "build" / "bin" / "llama-server",
            home / "llama.cpp" / "llama-server",
            home / "llama.cpp" / "server",
        ]

    def switch(self, model_name: str) -> dict[str, Any]:
        match = next((model for model in self.scan() if model.name == model_name), None)
        if match is None:
            return {"ok": False, "error": f"Model not found: {model_name}"}
        if not match.loadable:
            return {"ok": False, "error": "Only .gguf files can be loaded. .guff is treated as a typo in the UI."}
        previous = self.config.model.active_model
        self.config.model.fallback_model = previous or self.config.model.fallback_model
        self.config.model.active_model = model_name
        self._save()
        return {"ok": True, "active_model": model_name, "previous_model": previous}

    def check_model(self, model_name: str) -> dict[str, Any]:
        match = next((model for model in self.scan() if model.name == model_name), None)
        if match is None:
            return {"ok": False, "error": f"Model not found: {model_name}"}
        result = {
            "ok": True,
            "name": match.name,
            "path": match.path,
            "size_mb": match.size_mb,
            "active": match.active,
            "loadable": match.loadable,
            "status": "active" if match.active else "idle",
        }
        if match.active:
            result["server_health"] = self.server_health()
        return result

    def delete_model(self, model_name: str, force: bool = False) -> dict[str, Any]:
        match = next((model for model in self.scan() if model.name == model_name), None)
        if match is None:
            return {"ok": False, "error": f"Model not found: {model_name}"}
        if match.active and not force:
            return {"ok": False, "error": "Refusing to delete the active model. Switch models first or pass force=true."}
        root = self.models_dir.resolve()
        target = Path(match.path).resolve()
        if not (target == root or root in target.parents):
            return {"ok": False, "error": "Model path is outside the models folder."}
        try:
            target.unlink()
        except OSError as exc:
            return {"ok": False, "error": str(exc)}
        if self.config.model.active_model == model_name:
            self.config.model.active_model = ""
            self._save()
        return {"ok": True, "deleted": model_name, "path": str(target)}

    def server_health(self) -> dict[str, Any]:
        response = get_json(f"{self.config.model.base_url.rstrip('/')}/models", timeout=2)
        if response.ok:
            return {"ok": True, "base_url": self.config.model.base_url, "data": response.data}
        return {"ok": False, "base_url": self.config.model.base_url, "error": response.error}

    def chat(self, messages: list[dict[str, str]], max_tokens: int = 512) -> dict[str, Any]:
        payload = {
            "model": self.config.model.active_model or "local",
            "messages": messages,
            "temperature": self.config.model.temperature,
            "top_p": self.config.model.top_p,
            "max_tokens": max_tokens,
            "stream": False,
        }
        response = post_json(
            f"{self.config.model.base_url.rstrip('/')}/chat/completions",
            payload,
            timeout=self.config.model.request_timeout_seconds,
        )
        if not response.ok:
            return {"ok": False, "error": response.error}
        try:
            content = response.data["choices"][0]["message"]["content"]
        except Exception:
            return {"ok": False, "error": "Model response did not match OpenAI chat format.", "raw": response.data}
        return {"ok": True, "content": content, "raw": response.data}

    def restart_server(self) -> dict[str, Any]:
        model = self.active_path()
        if model is None:
            return {"ok": False, "error": "Select a .gguf model first."}
        llama_server = self.resolve_llama_server()
        if llama_server is None:
            return {
                "ok": False,
                "error": "llama-server was not found. Set the path in Models, for example ~/llama.cpp/build/bin/llama-server.",
                "llama_server": self.llama_server_status(),
            }
        self.stop_server()
        port = self._port_from_base_url()
        command = self._server_command(llama_server, model, port, fast=True)
        try:
            self._process = self._start_process(command)
            time.sleep(1)
            if self._process.poll() is not None and self._uses_memory_flags(command):
                fallback = self._server_command(llama_server, model, port, fast=False)
                self._process = self._start_process(fallback)
                command = fallback
        except Exception as exc:
            if self.config.model.fallback_model:
                self.config.model.active_model = self.config.model.fallback_model
                self._save()
            return {"ok": False, "error": str(exc), "command": command}
        time.sleep(1)
        health = self.server_health()
        health["started_process"] = self._process.pid if self._process else None
        health["command"] = command
        return health

    def _server_command(self, llama_server: str, model: Path, port: int, fast: bool) -> list[str]:
        threads = self.config.model.threads or max(1, os.cpu_count() or 1)
        command = [
            llama_server,
            "-m",
            str(model),
            "-c",
            str(self.config.model.context_size),
            "-t",
            str(threads),
            "-b",
            str(self.config.model.batch_size),
            "-ub",
            str(self.config.model.ubatch_size),
            "--parallel",
            str(self.config.model.parallel_slots),
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ]
        if fast and self.config.model.mlock:
            command.append("--mlock")
        if fast and self.config.model.no_mmap:
            command.append("--no-mmap")
        return command

    def _start_process(self, command: list[str]) -> subprocess.Popen:
        return subprocess.Popen(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def _uses_memory_flags(self, command: list[str]) -> bool:
        return "--mlock" in command or "--no-mmap" in command

    def stop_server(self) -> None:
        if self._process and self._process.poll() is None:
            self._process.terminate()
            try:
                self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._process.kill()
        self._process = None

    def _port_from_base_url(self) -> int:
        marker = "://"
        url = self.config.model.base_url
        host_part = url.split(marker, 1)[1] if marker in url else url
        host_part = host_part.split("/", 1)[0]
        if ":" in host_part:
            return int(host_part.rsplit(":", 1)[1])
        return 8080

    def _save(self) -> None:
        if self.persist:
            save_config(self.config)
