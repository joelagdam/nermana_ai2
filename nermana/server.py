from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, WebSocket
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .agent import AgentCore
from .capabilities import collect_capabilities
from .config import load_config, merge_config, save_config
from .model_downloads import download_model, download_preset, list_presets
from .telegram_bot import TelegramBot


STATIC_DIR = Path(__file__).resolve().parent / "web" / "static"


class ChatRequest(BaseModel):
    message: str
    session_id: str = "web"


class ToolRunRequest(BaseModel):
    payload: dict[str, Any] = {}


class ModelSwitchRequest(BaseModel):
    model_name: str


class ModelDownloadRequest(BaseModel):
    url: str = ""
    preset_id: str = ""
    filename: str = ""
    select: bool = True


class MemoryAddRequest(BaseModel):
    content: str
    tags: str = ""
    source: str = "web"


agent = AgentCore()
app = FastAPI(title="Nermana", version="0.1.0")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/status")
def status() -> dict[str, Any]:
    return {
        "agent": agent.status(),
        "capabilities": [cap.__dict__ for cap in collect_capabilities(agent.config, agent.models, agent.tools)],
    }


@app.post("/api/chat")
def chat(request: ChatRequest) -> dict[str, Any]:
    return agent.chat(request.message, request.session_id)


@app.websocket("/ws/chat")
async def chat_socket(socket: WebSocket) -> None:
    await socket.accept()
    while True:
        payload = await socket.receive_json()
        result = agent.chat(str(payload.get("message", "")), str(payload.get("session_id", "web")))
        await socket.send_json(result)


@app.get("/api/settings")
def get_settings() -> dict[str, Any]:
    return agent.settings_snapshot()


@app.post("/api/settings")
def update_settings(patch: dict[str, Any]) -> dict[str, Any]:
    cleaned = _preserve_redacted_secrets(patch, agent.config)
    new_config = merge_config(agent.config, cleaned)
    save_config(new_config)
    agent.reload(new_config)
    return agent.settings_snapshot()


@app.get("/api/models")
def models() -> dict[str, Any]:
    return {
        "models": [model.__dict__ for model in agent.models.scan()],
        "health": agent.models.server_health(),
        "llama_server": agent.models.llama_server_status(),
    }


@app.get("/api/models/presets")
def model_presets() -> dict[str, Any]:
    return {"presets": list_presets()}


@app.get("/api/models/llama")
def llama_server() -> dict[str, Any]:
    return agent.models.llama_server_status()


@app.post("/api/models/llama/use-detected")
def use_detected_llama_server() -> dict[str, Any]:
    resolved = agent.models.resolve_llama_server()
    if not resolved:
        return {"ok": False, "error": "llama-server was not detected."}
    agent.config.model.llama_server_path = resolved
    save_config(agent.config)
    return {"ok": True, "llama_server_path": resolved}


@app.post("/api/models/download")
def download_model_endpoint(request: ModelDownloadRequest) -> dict[str, Any]:
    if request.preset_id:
        result = download_preset(agent.config, request.preset_id, select=request.select)
    else:
        result = download_model(agent.config, request.url, request.filename, select=request.select)
    if result.get("ok"):
        save_config(agent.config)
    return result


@app.post("/api/models/select")
def select_model(request: ModelSwitchRequest) -> dict[str, Any]:
    result = agent.models.switch(request.model_name)
    if not result.get("ok"):
        raise HTTPException(400, result.get("error"))
    return result


@app.post("/api/models/restart")
def restart_model() -> dict[str, Any]:
    return agent.models.restart_server()


@app.post("/api/models/test")
def test_model(request: ChatRequest) -> dict[str, Any]:
    result = agent.models.chat([{"role": "user", "content": request.message}], max_tokens=128)
    return result


@app.get("/api/tools")
def tools() -> dict[str, Any]:
    return {"tools": agent.tools.list_metadata()}


@app.post("/api/tools/{tool_name}/enabled")
def set_tool_enabled(tool_name: str, payload: dict[str, bool]) -> dict[str, Any]:
    try:
        agent.tools.set_enabled(tool_name, bool(payload.get("enabled", True)))
    except KeyError:
        raise HTTPException(404, "tool not found") from None
    save_config(agent.config)
    return {"ok": True, "tool": tool_name, "enabled": agent.tools.get(tool_name).enabled}


@app.post("/api/tools/{tool_name}/run")
def run_tool(tool_name: str, request: ToolRunRequest) -> dict[str, Any]:
    return agent.run_tool(tool_name, request.payload)


@app.get("/api/memory")
def list_memory(limit: int = 100) -> dict[str, Any]:
    return {"memories": agent.memory.list_memories(limit)}


@app.post("/api/memory")
def add_memory(request: MemoryAddRequest) -> dict[str, Any]:
    memory_id = agent.memory.remember(request.content, tags=request.tags, source=request.source)
    return {"ok": True, "memory_id": memory_id}


@app.get("/api/memory/search")
def search_memory(q: str, limit: int = 8) -> dict[str, Any]:
    return {"results": [hit.__dict__ for hit in agent.memory.search(q, limit)]}


@app.delete("/api/memory/{memory_id}")
def forget_memory(memory_id: int) -> dict[str, Any]:
    return {"ok": agent.memory.forget(memory_id)}


@app.get("/api/sessions")
def sessions() -> dict[str, Any]:
    return {"sessions": agent.memory.list_sessions()}


@app.get("/api/sessions/{session_id}/messages")
def messages(session_id: str, limit: int = 80) -> dict[str, Any]:
    return {"messages": agent.memory.get_messages(session_id, limit)}


@app.get("/api/logs")
def logs() -> dict[str, Any]:
    return {
        "recent_sessions": agent.memory.list_sessions(),
        "model_health": agent.models.server_health(),
        "tools": agent.tools.list_metadata(),
    }


@app.post("/api/telegram/poll_once")
def telegram_poll_once() -> dict[str, Any]:
    try:
        return TelegramBot(agent).poll_once()
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def _preserve_redacted_secrets(patch: dict[str, Any], current) -> dict[str, Any]:
    providers = patch.get("providers")
    if isinstance(providers, dict):
        if providers.get("image_api_key") == "***":
            providers["image_api_key"] = current.providers.image_api_key
        if providers.get("vision_api_key") == "***":
            providers["vision_api_key"] = current.providers.vision_api_key
    telegram = patch.get("telegram")
    if isinstance(telegram, dict) and telegram.get("token") == "***":
        telegram["token"] = current.telegram.token
    return patch
