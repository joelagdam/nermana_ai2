from __future__ import annotations

import errno
import base64
import json
import mimetypes
import threading
import time
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

from .agent import AgentCore
from .capabilities import collect_capabilities
from .config import default_public_config, merge_config, reset_config_defaults, save_config
from .model_downloads import download_model, download_preset, list_presets
from .telegram_bot import TelegramBot
from .tools.files import _safe_path
from .updater import update_status, update_system


STATIC_DIR = Path(__file__).resolve().parent / "web" / "static"


class SimpleNermanaServer:
    def __init__(self, agent: AgentCore | None = None):
        self.agent = agent or AgentCore()
        self.downloads: dict[str, dict] = {}
        self.download_lock = threading.Lock()
        self.telegram_thread: threading.Thread | None = None

    def serve(self, host: str, port: int) -> None:
        outer = self

        class Handler(NermanaHandler):
            server_state = outer

        try:
            httpd = ThreadingHTTPServer((host, port), Handler)
        except OSError as exc:
            if exc.errno == errno.EADDRINUSE:
                print(f"web: {host}:{port} is already in use")
                print("web: stop the existing Nermana process with: pkill -f nermana")
                print("web: example: NERMANA_PORT=8766 sh scripts/termux_start_all.sh")
                raise SystemExit(98) from exc
            raise
        print(f"Nermana running on http://{host}:{port}")
        httpd.serve_forever()

    def start_model_download(self, body: dict) -> dict:
        job_id = uuid.uuid4().hex[:12]
        job = {
            "ok": True,
            "id": job_id,
            "state": "queued",
            "message": "Queued",
            "filename": "",
            "bytes_read": 0,
            "total_bytes": 0,
            "percent": 0,
            "result": None,
            "error": "",
            "started_at": time.time(),
            "updated_at": time.time(),
        }
        with self.download_lock:
            self.downloads[job_id] = job
        thread = threading.Thread(target=self._run_model_download, args=(job_id, dict(body)), name=f"model-download-{job_id}", daemon=True)
        thread.start()
        return {"ok": True, "job_id": job_id, "job": self.download_status(job_id)}

    def download_status(self, job_id: str) -> dict:
        with self.download_lock:
            job = self.downloads.get(job_id)
            if job is None:
                return {"ok": False, "error": "download job not found"}
            return dict(job)

    def _run_model_download(self, job_id: str, body: dict) -> None:
        def report(progress: dict) -> None:
            self._update_download(job_id, state="running", message="Downloading", **progress)

        self._update_download(job_id, state="running", message="Starting")
        try:
            if body.get("preset_id"):
                result = download_preset(self.agent.config, str(body.get("preset_id")), select=bool(body.get("select", True)), progress=report)
            else:
                result = download_model(
                    self.agent.config,
                    str(body.get("url", "")),
                    str(body.get("filename", "")),
                    select=bool(body.get("select", True)),
                    progress=report,
                )
            if result.get("ok"):
                save_config(self.agent.config)
            state = "complete" if result.get("ok") else "error"
            updates = {
                "ok": bool(result.get("ok")),
                "state": state,
                "message": "Download complete" if result.get("ok") else "Download failed",
                "result": result,
                "error": result.get("error", ""),
            }
            if result.get("size_bytes"):
                updates["bytes_read"] = result["size_bytes"]
            if result.get("total_bytes"):
                updates["total_bytes"] = result["total_bytes"]
                updates["percent"] = 100 if result.get("ok") else 0
            self._update_download(job_id, **updates)
        except Exception as exc:
            self._update_download(job_id, ok=False, state="error", message="Download failed", error=str(exc))

    def _update_download(self, job_id: str, **updates) -> None:
        with self.download_lock:
            job = self.downloads.get(job_id)
            if job is None:
                return
            job.update(updates)
            job["updated_at"] = time.time()

    def list_downloads(self) -> list[dict]:
        with self.download_lock:
            jobs = [dict(job) for job in self.downloads.values()]
        return sorted(jobs, key=lambda job: float(job.get("updated_at") or 0), reverse=True)

    def dashboard_snapshot(self) -> dict:
        agent_status = self.agent.status()
        capabilities = [
            cap.__dict__
            for cap in collect_capabilities(self.agent.config, self.agent.models, self.agent.tools, agent_status.get("model_health"))
        ]
        tools = list(agent_status.get("tools", []))
        sessions = list(agent_status.get("sessions", []))
        memory_count = self.agent.memory.count_memories()
        memory_insights = self.agent.memory.list_consolidations(limit=20)
        recent_memories = self.agent.memory.list_memories(5)
        downloads = self.list_downloads()
        active_downloads = [job for job in downloads if job.get("state") in {"queued", "running"}]
        workers = self._dashboard_workers(agent_status, capabilities, tools, active_downloads)
        working_count = sum(1 for worker in workers if worker["state"] in {"working", "busy", "ready"})
        enabled_tools = [tool for tool in tools if tool.get("enabled")]
        available_tools = [tool for tool in enabled_tools if tool.get("available")]
        return {
            "ok": True,
            "generated_at": time.time(),
            "agent": agent_status,
            "capabilities": capabilities,
            "workers": workers,
            "stats": {
                "workers_total": len(workers),
                "workers_working": working_count,
                "tools_total": len(tools),
                "tools_enabled": len(enabled_tools),
                "tools_working": len(available_tools),
                "sessions": len(sessions),
                "memories": memory_count,
                "memory_insights": len(memory_insights),
                "memory_unconsolidated": self.agent.memory.count_unconsolidated(),
                "downloads_total": len(downloads),
                "downloads_active": len(active_downloads),
                "model": self.agent.config.model.active_model or "no model selected",
                "weather_city": self.agent.config.weather.location_name,
                "allowed_folders": len(self.agent.config.files.allowed_dirs),
                "telegram_users": len(self.agent.config.telegram.allowed_user_ids),
            },
            "recent_sessions": sessions[:6],
            "recent_memories": recent_memories,
            "downloads": downloads[:6],
        }

    def _dashboard_workers(self, agent_status: dict, capabilities: list[dict], tools: list[dict], active_downloads: list[dict]) -> list[dict]:
        caps = {cap["name"]: cap for cap in capabilities}
        tool_map = {tool["name"]: tool for tool in tools}
        model_health = agent_status.get("model_health") or {}
        workers: list[dict] = []

        def capability_worker(label: str, key: str, ready_label: str = "working") -> None:
            cap = caps.get(key, {"available": False, "details": "not reported"})
            workers.append(
                {
                    "name": label,
                    "state": ready_label if cap.get("available") else "offline",
                    "details": cap.get("details") or "available",
                }
            )

        def tool_worker(label: str, key: str) -> None:
            tool = tool_map.get(key)
            if not tool:
                workers.append({"name": label, "state": "offline", "details": "tool missing"})
                return
            if not tool.get("enabled"):
                state = "disabled"
            elif tool.get("available"):
                state = "working"
            else:
                state = "offline"
            details = tool.get("details") or tool.get("description") or tool.get("provider") or "tool"
            workers.append({"name": label, "state": state, "details": details})

        workers.append(
            {
                "name": "Local model",
                "state": "working" if model_health.get("ok") else "offline",
                "details": model_health.get("error") or model_health.get("model") or self.agent.config.model.active_model or "no model selected",
            }
        )
        capability_worker("Internet", "internet")
        capability_worker("llama.cpp binary", "llama_server_binary", "ready")
        tool_worker("Search", "web_search")
        tool_worker("Weather", "current_weather")
        tool_worker("Files", "read_file")
        tool_worker("Phone control", "phone_status")
        capability_worker("Termux API", "termux_api", "ready")
        capability_worker("Shizuku", "shizuku_rish", "ready")
        tool_worker("Image generation", "generate_image")
        tool_worker("Vision", "vision_analyze")
        capability_worker("Telegram", "telegram")
        for job in active_downloads[:2]:
            workers.append(
                {
                    "name": f"Model download {job.get('filename') or job.get('id')}",
                    "state": "busy",
                    "details": job.get("message") or job.get("state") or "running",
                }
            )
        return workers

    def start_telegram(self) -> dict:
        cfg = self.agent.config.telegram
        if not cfg.enabled or not cfg.token:
            return {"ok": False, "error": "Telegram is disabled or token is missing."}
        if self.telegram_thread and self.telegram_thread.is_alive():
            return {"ok": True, "message": "Telegram polling already running."}
        bot = TelegramBot(self.agent)
        status = bot.status()
        if not status.get("ok"):
            return status
        clear = bot.delete_webhook(drop_pending_updates=False)
        if not clear.get("ok"):
            return clear
        self.telegram_thread = threading.Thread(target=bot.run_forever, name="nermana-telegram-web", daemon=True)
        self.telegram_thread.start()
        return {"ok": True, "message": "Telegram polling started.", "offset": bot.offset, "bot": status.get("bot"), "webhook": clear}


class NermanaHandler(BaseHTTPRequestHandler):
    server_state: SimpleNermanaServer

    def do_GET(self) -> None:
        path, query = self._parsed()
        if path == "/":
            self._send_file(STATIC_DIR / "index.html")
        elif path.startswith("/static/"):
            self._send_file(STATIC_DIR / path.removeprefix("/static/"))
        elif path == "/api/dashboard":
            self._json(self.server_state.dashboard_snapshot())
        elif path == "/api/status":
            self._json(self._status())
        elif path == "/api/proactive":
            self._json(self.agent.initiative_message(query.get("session_id", ["web"])[0]))
        elif path == "/api/settings":
            self._json(self.agent.settings_snapshot())
        elif path == "/api/settings/defaults":
            self._json({"ok": True, "defaults": default_public_config()})
        elif path == "/api/models":
            self._json(
                {
                    "models": [model.__dict__ for model in self.agent.models.scan()],
                    "health": self.agent.models.runtime_status(),
                    "llama_server": self.agent.models.llama_server_status(),
                }
            )
        elif path == "/api/models/health":
            self._json(self.agent.models.runtime_status(force=True))
        elif path == "/api/models/presets":
            self._json({"presets": list_presets()})
        elif path.startswith("/api/models/downloads/"):
            result = self.server_state.download_status(unquote(path.rsplit("/", 1)[1]))
            status = HTTPStatus.NOT_FOUND if result.get("error") == "download job not found" else HTTPStatus.OK
            self._json(result, status)
        elif path == "/api/models/llama":
            self._json(self.agent.models.llama_server_status())
        elif path == "/api/tools":
            self._json({"tools": self.agent.tools.list_metadata()})
        elif path == "/api/memory":
            self._json({"memories": self.agent.memory.list_memories(int(query.get("limit", ["100"])[0]))})
        elif path == "/api/memory/search":
            self._json({"results": [hit.__dict__ for hit in self.agent.memory.search(query.get("q", [""])[0], int(query.get("limit", ["8"])[0]))]})
        elif path.startswith("/api/memory/"):
            try:
                memory_id = int(path.rsplit("/", 1)[1])
            except ValueError:
                self._json({"ok": False, "error": "invalid memory id"}, HTTPStatus.BAD_REQUEST)
                return
            memory = self.agent.memory.get_memory(memory_id)
            self._json(memory or {"ok": False, "error": "memory not found"}, HTTPStatus.OK if memory else HTTPStatus.NOT_FOUND)
        elif path == "/api/sessions":
            self._json({"sessions": self.agent.memory.list_sessions()})
        elif path.startswith("/api/sessions/") and path.endswith("/messages"):
            session_id = unquote(path.split("/")[3])
            self._json({"messages": self.agent.memory.get_messages(session_id, int(query.get("limit", ["80"])[0]))})
        elif path == "/api/logs":
            self._json(
                {
                    "recent_sessions": self.agent.memory.list_sessions(),
                    "model_health": self.agent.models.runtime_status(),
                    "tools": self.agent.tools.list_metadata(),
                }
            )
        elif path == "/api/update/status":
            refresh = query.get("refresh", ["0"])[0].lower() in {"1", "true", "yes"}
            self._json(update_status(fetch=refresh))
        elif path == "/api/telegram/status":
            try:
                self._json(TelegramBot(self.agent).status())
            except Exception as exc:
                self._json({"ok": False, "error": str(exc)})
        elif path == "/api/files/download":
            self._download_file(query.get("path", [""])[0])
        else:
            self._json({"ok": False, "error": "not found"}, HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        path, _ = self._parsed()
        body = self._body()
        if path == "/api/chat":
            self._json(self.agent.chat(str(body.get("message", "")), str(body.get("session_id", "web"))))
        elif path == "/api/settings":
            cleaned = self._preserve_redacted_secrets(body)
            new_config = merge_config(self.agent.config, cleaned)
            save_config(new_config)
            self.agent.reload(new_config)
            self._json(self.agent.settings_snapshot())
        elif path == "/api/settings/reset":
            new_config = reset_config_defaults(
                self.agent.config,
                preserve_secrets=bool(body.get("preserve_secrets", True)),
                preserve_model_selection=bool(body.get("preserve_model_selection", True)),
            )
            save_config(new_config)
            self.agent.reload(new_config)
            self._json({"ok": True, "message": "Settings restored to defaults.", "config": self.agent.settings_snapshot()})
        elif path == "/api/models/select":
            result = self.agent.models.switch(str(body.get("model_name", "")))
            self._json(result, HTTPStatus.OK if result.get("ok") else HTTPStatus.BAD_REQUEST)
        elif path == "/api/models/check":
            result = self.agent.models.check_model(str(body.get("model_name", "")))
            self._json(result, HTTPStatus.OK if result.get("ok") else HTTPStatus.BAD_REQUEST)
        elif path == "/api/models/delete":
            result = self.agent.models.delete_model(str(body.get("model_name", "")), force=bool(body.get("force", False)))
            self._json(result, HTTPStatus.OK if result.get("ok") else HTTPStatus.BAD_REQUEST)
        elif path == "/api/models/restart":
            self._json(self.agent.models.restart_server())
        elif path == "/api/models/stop":
            self.agent.models.stop_server()
            killed = self.agent.models.stop_external_server()
            self._json({"ok": True, "message": "Model server stop requested.", "killed_external": killed, "health": self.agent.models.server_health()})
        elif path == "/api/models/test":
            self._json(self.agent.models.chat([{"role": "user", "content": str(body.get("message", ""))}], max_tokens=128))
        elif path == "/api/models/llama/use-detected":
            resolved = self.agent.models.resolve_llama_server()
            if not resolved:
                self._json({"ok": False, "error": "llama-server was not detected."})
            else:
                self.agent.config.model.llama_server_path = resolved
                save_config(self.agent.config)
                self._json({"ok": True, "llama_server_path": resolved})
        elif path == "/api/models/download/start":
            self._json(self.server_state.start_model_download(body))
        elif path == "/api/models/download":
            if body.get("preset_id"):
                result = download_preset(self.agent.config, str(body.get("preset_id")), select=bool(body.get("select", True)))
            else:
                result = download_model(
                    self.agent.config,
                    str(body.get("url", "")),
                    str(body.get("filename", "")),
                    select=bool(body.get("select", True)),
                )
            if result.get("ok"):
                save_config(self.agent.config)
            self._json(result)
        elif path == "/api/files/upload":
            self._json(self._upload_file(body))
        elif path.startswith("/api/tools/") and path.endswith("/enabled"):
            tool_name = path.split("/")[3]
            try:
                self.agent.tools.set_enabled(tool_name, bool(body.get("enabled", True)))
                save_config(self.agent.config)
                self._json({"ok": True, "tool": tool_name, "enabled": self.agent.tools.get(tool_name).enabled})
            except KeyError:
                self._json({"ok": False, "error": "tool not found"}, HTTPStatus.NOT_FOUND)
        elif path.startswith("/api/tools/") and path.endswith("/run"):
            tool_name = path.split("/")[3]
            self._json(self.agent.run_tool(tool_name, body.get("payload", {})))
        elif path == "/api/memory":
            memory_id = self.agent.memory.remember(str(body.get("content", "")), tags=str(body.get("tags", "")), source=str(body.get("source", "web")))
            self._json({"ok": True, "memory_id": memory_id})
        elif path.startswith("/api/memory/"):
            try:
                memory_id = int(path.rsplit("/", 1)[1])
            except ValueError:
                self._json({"ok": False, "error": "invalid memory id"}, HTTPStatus.BAD_REQUEST)
                return
            result = self.agent.memory.update_memory(memory_id, body)
            self._json(result, HTTPStatus.OK if result.get("ok") else HTTPStatus.NOT_FOUND)
        elif path == "/api/telegram/poll_once":
            try:
                self._json(TelegramBot(self.agent).poll_once(timeout=1))
            except Exception as exc:
                self._json({"ok": False, "error": str(exc)})
        elif path == "/api/telegram/start":
            self._json(self.server_state.start_telegram())
        elif path == "/api/telegram/clear_webhook":
            try:
                self._json(TelegramBot(self.agent).delete_webhook(drop_pending_updates=bool(body.get("drop_pending_updates", False))))
            except Exception as exc:
                self._json({"ok": False, "error": str(exc)})
        elif path == "/api/telegram/reset_offset":
            try:
                self._json(TelegramBot(self.agent).reset_offset(drop_pending_updates=bool(body.get("drop_pending_updates", False))))
            except Exception as exc:
                self._json({"ok": False, "error": str(exc)})
        elif path == "/api/update":
            self._json(update_system())
        else:
            self._json({"ok": False, "error": "not found"}, HTTPStatus.NOT_FOUND)

    def do_DELETE(self) -> None:
        path, _ = self._parsed()
        if path.startswith("/api/memory/"):
            self._json({"ok": self.agent.memory.forget(int(path.rsplit("/", 1)[1]))})
        else:
            self._json({"ok": False, "error": "not found"}, HTTPStatus.NOT_FOUND)

    @property
    def agent(self) -> AgentCore:
        return self.server_state.agent

    def _status(self) -> dict:
        agent_status = self.agent.status()
        return {
            "agent": agent_status,
            "capabilities": [
                cap.__dict__
                for cap in collect_capabilities(self.agent.config, self.agent.models, self.agent.tools, agent_status.get("model_health"))
            ],
        }

    def _parsed(self) -> tuple[str, dict[str, list[str]]]:
        parsed = urlparse(self.path)
        return parsed.path, parse_qs(parsed.query)

    def _body(self) -> dict:
        length = int(self.headers.get("content-length", "0") or "0")
        if not length:
            return {}
        raw = self.rfile.read(length).decode("utf-8", errors="replace")
        return json.loads(raw) if raw else {}

    def _json(self, payload: dict, status: HTTPStatus = HTTPStatus.OK) -> None:
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status.value)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_file(self, path: Path) -> None:
        root = STATIC_DIR.resolve()
        target = path.resolve()
        if not (target == root or root in target.parents) or not target.exists() or not target.is_file():
            self._json({"ok": False, "error": "not found"}, HTTPStatus.NOT_FOUND)
            return
        data = target.read_bytes()
        self.send_response(HTTPStatus.OK.value)
        self.send_header("Content-Type", mimetypes.guess_type(target.name)[0] or "application/octet-stream")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _upload_file(self, body: dict) -> dict:
        if not self.agent.config.files.enabled:
            return {"ok": False, "error": "file tools are disabled"}
        if not self.agent.config.files.allowed_dirs:
            return {"ok": False, "error": "no allowed upload folder is configured"}
        filename = Path(str(body.get("filename") or "upload.bin")).name.strip()
        if not filename:
            return {"ok": False, "error": "filename is required"}
        raw = str(body.get("content_base64") or "")
        max_bytes = int(self.agent.config.files.max_read_mb) * 1024 * 1024
        if len(raw) > max_bytes * 2:
            return {"ok": False, "error": f"file exceeds {self.agent.config.files.max_read_mb} MB limit"}
        try:
            data = base64.b64decode(raw, validate=True)
        except Exception:
            return {"ok": False, "error": "invalid base64 upload"}
        if len(data) > max_bytes:
            return {"ok": False, "error": f"file exceeds {self.agent.config.files.max_read_mb} MB limit"}
        root = _safe_path(self.agent.config, str(self.agent.config.files.allowed_dirs[0])).resolve()
        upload_dir = root / "uploads"
        upload_dir.mkdir(parents=True, exist_ok=True)
        try:
            target = _unique_upload_path(upload_dir, filename)
            target.write_bytes(data)
        except OSError as exc:
            return {"ok": False, "error": str(exc)}
        result = {
            "ok": True,
            "filename": target.name,
            "path": str(target),
            "size": len(data),
            "download_url": f"/api/files/download?path={quote(str(target), safe='')}",
        }
        if body.get("index"):
            result["index_result"] = self.agent.run_tool("index_file", {"path": str(target)})
        return result

    def _download_file(self, raw_path: str) -> None:
        try:
            target = _safe_path(self.agent.config, unquote(raw_path))
        except Exception as exc:
            self._json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        if not target.exists() or not target.is_file():
            self._json({"ok": False, "error": "file not found"}, HTTPStatus.NOT_FOUND)
            return
        data = target.read_bytes()
        self.send_response(HTTPStatus.OK.value)
        self.send_header("Content-Type", mimetypes.guess_type(target.name)[0] or "application/octet-stream")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Content-Disposition", f'attachment; filename="{target.name}"')
        self.end_headers()
        self.wfile.write(data)

    def _preserve_redacted_secrets(self, patch: dict) -> dict:
        providers = patch.get("providers")
        if isinstance(providers, dict):
            if providers.get("image_api_key") == "***":
                providers["image_api_key"] = self.agent.config.providers.image_api_key
            if providers.get("vision_api_key") == "***":
                providers["vision_api_key"] = self.agent.config.providers.vision_api_key
        telegram = patch.get("telegram")
        if isinstance(telegram, dict) and telegram.get("token") == "***":
            telegram["token"] = self.agent.config.telegram.token
        return patch

    def log_message(self, format: str, *args) -> None:
        return


def serve(host: str, port: int) -> None:
    SimpleNermanaServer().serve(host, port)


def _unique_upload_path(folder: Path, filename: str) -> Path:
    clean = "".join(char if char.isalnum() or char in "._- " else "_" for char in filename).strip()
    clean = clean or "upload.bin"
    root = folder.resolve()
    target = (folder / clean).resolve()
    if not (target == root or root in target.parents):
        raise PermissionError("upload path escaped upload folder")
    if not target.exists():
        return target
    stem = target.stem
    suffix = target.suffix
    for index in range(1, 1000):
        candidate = folder / f"{stem}-{index}{suffix}"
        if not candidate.exists():
            return candidate
    raise FileExistsError("too many uploads with the same name")
