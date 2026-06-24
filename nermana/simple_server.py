from __future__ import annotations

import errno
import json
import mimetypes
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from .agent import AgentCore
from .capabilities import collect_capabilities
from .config import merge_config, save_config
from .model_downloads import download_model, download_preset, list_presets
from .telegram_bot import TelegramBot
from .updater import update_status, update_system


STATIC_DIR = Path(__file__).resolve().parent / "web" / "static"


class SimpleNermanaServer:
    def __init__(self, agent: AgentCore | None = None):
        self.agent = agent or AgentCore()

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


class NermanaHandler(BaseHTTPRequestHandler):
    server_state: SimpleNermanaServer

    def do_GET(self) -> None:
        path, query = self._parsed()
        if path == "/":
            self._send_file(STATIC_DIR / "index.html")
        elif path.startswith("/static/"):
            self._send_file(STATIC_DIR / path.removeprefix("/static/"))
        elif path == "/api/status":
            self._json(self._status())
        elif path == "/api/settings":
            self._json(self.agent.settings_snapshot())
        elif path == "/api/models":
            self._json(
                {
                    "models": [model.__dict__ for model in self.agent.models.scan()],
                    "health": self.agent.models.server_health(),
                    "llama_server": self.agent.models.llama_server_status(),
                }
            )
        elif path == "/api/models/presets":
            self._json({"presets": list_presets()})
        elif path == "/api/models/llama":
            self._json(self.agent.models.llama_server_status())
        elif path == "/api/tools":
            self._json({"tools": self.agent.tools.list_metadata()})
        elif path == "/api/memory":
            self._json({"memories": self.agent.memory.list_memories(int(query.get("limit", ["100"])[0]))})
        elif path == "/api/memory/search":
            self._json({"results": [hit.__dict__ for hit in self.agent.memory.search(query.get("q", [""])[0], int(query.get("limit", ["8"])[0]))]})
        elif path == "/api/sessions":
            self._json({"sessions": self.agent.memory.list_sessions()})
        elif path.startswith("/api/sessions/") and path.endswith("/messages"):
            session_id = unquote(path.split("/")[3])
            self._json({"messages": self.agent.memory.get_messages(session_id, int(query.get("limit", ["80"])[0]))})
        elif path == "/api/logs":
            self._json(
                {
                    "recent_sessions": self.agent.memory.list_sessions(),
                    "model_health": self.agent.models.server_health(),
                    "tools": self.agent.tools.list_metadata(),
                }
            )
        elif path == "/api/update/status":
            refresh = query.get("refresh", ["0"])[0].lower() in {"1", "true", "yes"}
            self._json(update_status(fetch=refresh))
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
        elif path == "/api/models/select":
            result = self.agent.models.switch(str(body.get("model_name", "")))
            self._json(result, HTTPStatus.OK if result.get("ok") else HTTPStatus.BAD_REQUEST)
        elif path == "/api/models/restart":
            self._json(self.agent.models.restart_server())
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
        elif path == "/api/telegram/poll_once":
            try:
                self._json(TelegramBot(self.agent).poll_once())
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
        return {
            "agent": self.agent.status(),
            "capabilities": [cap.__dict__ for cap in collect_capabilities(self.agent.config, self.agent.models, self.agent.tools)],
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
