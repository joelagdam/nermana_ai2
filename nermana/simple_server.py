from __future__ import annotations

import errno
import base64
import json
import mimetypes
import shutil
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
from .model_downloads import delete_partial_download, download_model, download_preset, list_partial_downloads, list_presets
from .self_learning import append_self_learning_log, tail_self_learning_log
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
        self.telegram_stop = threading.Event()
        self.telegram_lock = threading.Lock()
        self.telegram_state: dict = {
            "ok": False,
            "running": False,
            "started_at": 0,
            "last_poll_at": 0,
            "last_error": "",
            "processed": 0,
            "offset": 0,
        }
        self.self_learning_thread: threading.Thread | None = None
        self.self_heal_thread: threading.Thread | None = None
        self.self_learning_stop = threading.Event()
        self.self_learning_lock = threading.Lock()
        self.self_learning_state: dict = {
            "ok": True,
            "running": False,
            "healing": False,
            "started_at": 0,
            "last_cycle_at": 0,
            "last_repair_at": 0,
            "last_heal_at": 0,
            "last_heal_source": "",
            "last_heal_error": "",
            "last_error": "",
            "cycles": 0,
            "repairs": 0,
            "heals": 0,
            "last_summary": "",
        }

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
            "cancel_requested": False,
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

        def cancelled() -> bool:
            with self.download_lock:
                job = self.downloads.get(job_id) or {}
                return bool(job.get("cancel_requested"))

        self._update_download(job_id, state="running", message="Starting")
        try:
            if body.get("preset_id"):
                result = download_preset(
                    self.agent.config,
                    str(body.get("preset_id")),
                    select=bool(body.get("select", True)),
                    progress=report,
                    cancelled=cancelled,
                )
            else:
                result = download_model(
                    self.agent.config,
                    str(body.get("url", "")),
                    str(body.get("filename", "")),
                    select=bool(body.get("select", True)),
                    progress=report,
                    cancelled=cancelled,
                )
            if result.get("ok"):
                save_config(self.agent.config)
            state = "complete" if result.get("ok") else "cancelled" if result.get("cancelled") else "error"
            updates = {
                "ok": bool(result.get("ok")),
                "state": state,
                "message": "Download complete" if result.get("ok") else "Download cancelled" if result.get("cancelled") else "Download failed",
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

    def cancel_model_download(self, job_id: str) -> dict:
        with self.download_lock:
            job = self.downloads.get(job_id)
            if job is None:
                return {"ok": False, "error": "download job not found"}
            if job.get("state") not in {"queued", "running"}:
                return {"ok": False, "error": f"download is already {job.get('state')}"}
            job["cancel_requested"] = True
            job["message"] = "Cancel requested"
            job["updated_at"] = time.time()
            return {"ok": True, "job": dict(job)}

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
            "telegram": self.telegram_worker_status(),
            "self_learning": self.self_learning_status(include_log=False),
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
                "partials_total": len(list_partial_downloads(self.agent.config)),
                "model": self.agent.config.model.active_model or "no model selected",
                "weather_city": self.agent.config.weather.location_name,
                "allowed_folders": len(self.agent.config.files.allowed_dirs),
                "telegram_users": len(self.agent.config.telegram.allowed_user_ids),
                "self_learning_cycles": int(self.self_learning_state.get("cycles", 0) or 0),
            },
            "recent_sessions": sessions[:6],
            "recent_memories": recent_memories,
            "downloads": downloads[:6],
        }

    def doctor_snapshot(self, force: bool = True) -> dict:
        model_health = self.agent.models.runtime_status(force=force)
        llama = self.agent.models.llama_server_status()
        models = [model.__dict__ for model in self.agent.models.scan()]
        active_model = self.agent.config.model.active_model
        active_found = any(model["name"] == active_model for model in models) if active_model else False
        loadable_count = sum(1 for model in models if model.get("loadable"))
        telegram_worker = self.telegram_worker_status()
        tools = self.agent.tools.list_metadata()
        issues: list[dict] = []
        actions: dict[str, dict] = {}

        def add_action(key: str, label: str, detail: str, risk: str = "safe") -> None:
            actions.setdefault(key, {"key": key, "label": label, "detail": detail, "risk": risk})

        def add_issue(severity: str, area: str, title: str, detail: str, action_keys: list[str] | None = None) -> None:
            issues.append({"severity": severity, "area": area, "title": title, "detail": detail, "actions": action_keys or []})
            for key in action_keys or []:
                if key == "auto":
                    add_action("auto", "Auto Repair", "Run safe model and Telegram repair steps in order.")
                elif key == "model":
                    add_action("model", "Repair Local Model", "Wait for llama.cpp readiness, then restart the model server if still stuck.")
                elif key == "llama_detect":
                    add_action("llama_detect", "Use Detected llama-server", "Save the detected llama-server path into settings.")
                elif key == "telegram":
                    add_action("telegram", "Repair Telegram", "Clear webhook conflicts and restart polling when Telegram is configured.")
                elif key == "telegram_clear":
                    add_action("telegram_clear", "Clear Telegram Webhook", "Remove webhook mode so polling can receive messages.")
                elif key == "telegram_drop_pending":
                    add_action("telegram_drop_pending", "Drop Pending Telegram Updates", "Advance offset past old messages to stop repeats.", "read")

        if not active_model:
            add_issue("error", "model", "No active model selected", "Choose or download a .gguf model before chat can use the local voice engine.", ["auto"])
        elif not active_found:
            add_issue("error", "model", "Active model file is missing", f"Configured model `{active_model}` is not in the models folder.", ["auto"])
        elif not model_health.get("ok"):
            error = model_health.get("chat_check", {}).get("error") or model_health.get("error") or model_health.get("state") or "model is not ready"
            if _model_loading_error(error):
                add_issue("warn", "model", "Model is still loading", "llama.cpp is reachable but not ready for chat yet. Wait first; restart only if it stays stuck.", ["model", "auto"])
            elif model_health.get("context_mismatch"):
                add_issue("warn", "model", "Context mismatch", model_health.get("context_warning", "Restart llama.cpp so the saved context applies."), ["model", "auto"])
            elif model_health.get("endpoint_ok"):
                add_issue("error", "model", "Model endpoint is up but chat failed", str(error), ["model", "auto"])
            else:
                add_issue("error", "model", "Model server is offline", str(error), ["model", "auto"])
        if not llama.get("available"):
            add_issue("error", "llama", "llama-server not detected", "Set the llama-server path or build llama.cpp in Termux home.", ["llama_detect"])
        elif self.agent.config.model.llama_server_path in {"", "auto"}:
            add_issue("info", "llama", "llama-server detected but not pinned", f"Detected `{llama.get('resolved')}`. Save it to avoid path drift.", ["llama_detect"])
        if self.agent.config.telegram.enabled and self.agent.config.telegram.token:
            last_error = str(telegram_worker.get("last_error") or "")
            if not telegram_worker.get("running"):
                add_issue("warn", "telegram", "Telegram polling is stopped", last_error or "Start polling so the bot can receive messages.", ["telegram", "auto"])
            elif last_error:
                keys = ["telegram", "auto"]
                if "webhook" in last_error.lower() or "conflict" in last_error.lower():
                    keys.append("telegram_clear")
                add_issue("warn", "telegram", "Telegram worker has an error", last_error, keys)
        else:
            add_issue("info", "telegram", "Telegram is not configured", "Paste a BotFather token and enable Telegram before polling.", [])

        if not issues:
            add_action("model", "Repair Local Model", "Run a model readiness check and restart only if needed.")
            add_action("telegram", "Repair Telegram", "Restart Telegram polling when configured.")

        summary = "No blocking issues detected." if not issues else f"{len(issues)} issue(s) detected. Start with Auto Repair for safe fixes."
        return {
            "ok": True,
            "generated_at": time.time(),
            "summary": summary,
            "issues": issues,
            "actions": list(actions.values()),
            "checks": {
                "model": model_health,
                "llama_server": llama,
                "models": {"active": active_model, "active_found": active_found, "count": len(models), "loadable_count": loadable_count},
                "telegram_worker": telegram_worker,
                "tools": {
                    "total": len(tools),
                    "enabled": sum(1 for tool in tools if tool.get("enabled")),
                    "available": sum(1 for tool in tools if tool.get("enabled") and tool.get("available")),
                },
                "llama_log": self.agent.models.server_log_tail(20),
            },
        }

    def repair(self, action: str = "auto") -> dict:
        action = (action or "auto").strip().lower()
        if action not in {"auto", "model", "llama_detect", "telegram", "telegram_clear", "telegram_drop_pending"}:
            return {"ok": False, "error": f"unknown repair action: {action}"}
        steps: list[dict] = []

        def add_step(name: str, result: dict) -> dict:
            steps.append({"name": name, "ok": bool(result.get("ok")), "result": result})
            return result

        if action in {"auto", "llama_detect"}:
            resolved = self.agent.models.resolve_llama_server()
            if resolved:
                self.agent.config.model.llama_server_path = resolved
                save_config(self.agent.config)
                add_step("llama_detect", {"ok": True, "llama_server_path": resolved})
            elif action == "llama_detect":
                add_step("llama_detect", {"ok": False, "error": "llama-server was not detected."})

        if action in {"auto", "model"}:
            add_step("model", self.repair_model_server())

        if action in {"auto", "telegram"}:
            add_step("telegram", self.repair_telegram())
        elif action == "telegram_clear":
            try:
                add_step("telegram_clear", TelegramBot(self.agent).delete_webhook(drop_pending_updates=False))
            except Exception as exc:
                add_step("telegram_clear", {"ok": False, "error": str(exc)})
        elif action == "telegram_drop_pending":
            try:
                add_step("telegram_drop_pending", TelegramBot(self.agent).reset_offset(drop_pending_updates=True))
            except Exception as exc:
                add_step("telegram_drop_pending", {"ok": False, "error": str(exc)})

        hard_failures = [step for step in steps if not step.get("ok") and not _repair_step_skippable(step)]
        return {
            "ok": not hard_failures,
            "action": action,
            "steps": steps,
            "summary": "Repair finished." if not hard_failures else "Repair finished with issues.",
            "diagnostics": self.doctor_snapshot(force=True),
        }

    def repair_model_server(self) -> dict:
        before = self.agent.models.runtime_status(force=True)
        if before.get("ok"):
            return {"ok": True, "message": "Local model is already ready.", "health": before}
        if self.agent.models.active_path() is None:
            models = [model for model in self.agent.models.scan() if model.loadable]
            if not models:
                return {"ok": False, "error": "No .gguf model is available. Download or copy a model into the models folder.", "health": before}
            selected = self.agent.models.switch(models[0].name)
            if not selected.get("ok"):
                return {"ok": False, "error": selected.get("error", "could not select a model"), "health": before}
        error = before.get("chat_check", {}).get("error") or before.get("error") or ""
        if _model_loading_error(error):
            for _ in range(3):
                time.sleep(2)
                waited = self.agent.models.runtime_status(force=True)
                if waited.get("ok"):
                    return {"ok": True, "message": "Local model finished loading.", "health": waited, "waited": True}
        restart = self.agent.models.restart_server()
        return {
            "ok": bool(restart.get("ok")),
            "message": "Model restart requested." if restart.get("ok") else "Model restart did not reach ready state.",
            "health": restart,
        }

    def repair_telegram(self) -> dict:
        cfg = self.agent.config.telegram
        if not cfg.enabled or not cfg.token:
            return {"ok": True, "skipped": True, "message": "Telegram is disabled or missing a token."}
        try:
            bot = TelegramBot(self.agent)
            clear = bot.delete_webhook(drop_pending_updates=False)
            if not clear.get("ok") and clear.get("offline"):
                return {"ok": True, "skipped": True, "message": clear.get("error", "Telegram is offline."), "webhook": clear}
            if not clear.get("ok"):
                return {"ok": False, "error": clear.get("error", "webhook clear failed"), "webhook": clear}
            started = self.start_telegram()
            return {"ok": bool(started.get("ok")), "webhook": clear, "start": started, "error": started.get("error", "")}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

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
        telegram = self.telegram_worker_status()
        telegram_cap = caps.get("telegram", {"available": False, "details": "not reported"})
        if telegram.get("running"):
            telegram_state = "working"
            telegram_details = f"processed {telegram.get('processed', 0)} update(s); offset {telegram.get('offset', 0)}"
        elif self.agent.config.telegram.enabled and self.agent.config.telegram.token:
            telegram_state = "ready" if telegram_cap.get("available") else "offline"
            telegram_details = telegram.get("last_error") or telegram_cap.get("details") or "configured"
        else:
            telegram_state = "disabled"
            telegram_details = "disabled or missing token"
        workers.append({"name": "Telegram", "state": telegram_state, "details": telegram_details})
        learning = self.self_learning_status(include_log=False)
        learning_worker = learning.get("worker", {})
        if not learning.get("enabled"):
            learning_state = "disabled"
            learning_details = "disabled"
        elif learning_worker.get("running") or learning_worker.get("thread_alive"):
            learning_state = "working"
            learning_details = learning_worker.get("last_summary") or f"{learning_worker.get('cycles', 0)} cycle(s)"
        elif learning_worker.get("last_error"):
            learning_state = "offline"
            learning_details = learning_worker.get("last_error")
        else:
            learning_state = "ready"
            learning_details = "ready to run diagnostics"
        workers.append({"name": "Self learning", "state": learning_state, "details": learning_details})
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
            self._set_telegram_state(ok=False, running=False, last_error="Telegram is disabled or token is missing.")
            return {"ok": False, "error": "Telegram is disabled or token is missing."}
        if self.telegram_thread and self.telegram_thread.is_alive():
            return {"ok": True, "message": "Telegram polling already running.", "worker": self.telegram_worker_status()}
        bot = TelegramBot(self.agent, error_callback=self.trigger_self_heal)
        status = bot.status()
        if not status.get("ok"):
            self._set_telegram_state(ok=False, running=False, last_error=status.get("error", "Telegram is not ready."))
            return status
        clear = bot.delete_webhook(drop_pending_updates=False)
        if not clear.get("ok"):
            self._set_telegram_state(ok=False, running=False, last_error=clear.get("error", "webhook clear failed"))
            return clear
        self.telegram_stop.clear()
        self._set_telegram_state(ok=True, running=True, started_at=time.time(), last_error="", offset=bot.offset)
        self.telegram_thread = threading.Thread(target=self._telegram_loop, args=(bot,), name="nermana-telegram-web", daemon=True)
        self.telegram_thread.start()
        return {"ok": True, "message": "Telegram polling started.", "offset": bot.offset, "bot": status.get("bot"), "webhook": clear, "worker": self.telegram_worker_status()}

    def telegram_worker_status(self) -> dict:
        with self.telegram_lock:
            state = dict(self.telegram_state)
        state["thread_alive"] = bool(self.telegram_thread and self.telegram_thread.is_alive())
        if not state["thread_alive"] and state.get("running"):
            state["running"] = False
            state["ok"] = False
            state["last_error"] = state.get("last_error") or "Telegram worker stopped."
        return state

    def stop_workers(self) -> None:
        self.telegram_stop.set()
        self.self_learning_stop.set()

    def start_self_learning(self) -> dict:
        cfg = self.agent.config.self_learning
        if not cfg.enabled:
            self._set_self_learning_state(ok=True, running=False, last_error="Self learning is disabled.")
            return {"ok": False, "error": "Self learning is disabled."}
        if self.self_learning_thread and self.self_learning_thread.is_alive():
            return {"ok": True, "message": "Self learning already running.", "worker": self.self_learning_status()}
        self.self_learning_stop.clear()
        self._set_self_learning_state(ok=True, running=True, started_at=time.time(), last_error="")
        append_self_learning_log(self.agent.config, "worker", "self learning started")
        self.self_learning_thread = threading.Thread(target=self._self_learning_loop, name="nermana-self-learning", daemon=True)
        self.self_learning_thread.start()
        return {"ok": True, "message": "Self learning started.", "worker": self.self_learning_status()}

    def stop_self_learning(self) -> dict:
        self.self_learning_stop.set()
        self._set_self_learning_state(running=False)
        append_self_learning_log(self.agent.config, "worker", "self learning stopped")
        return {"ok": True, "message": "Self learning stop requested.", "worker": self.self_learning_status()}

    def self_learning_status(self, include_log: bool = True, lines: int | None = None) -> dict:
        with self.self_learning_lock:
            state = dict(self.self_learning_state)
        state["thread_alive"] = bool(self.self_learning_thread and self.self_learning_thread.is_alive())
        state["heal_thread_alive"] = bool(self.self_heal_thread and self.self_heal_thread.is_alive())
        state["healing"] = bool(state.get("healing") or state["heal_thread_alive"])
        if not state["thread_alive"] and state.get("running"):
            state["running"] = False
            state["ok"] = False
            state["last_error"] = state.get("last_error") or "Self learning worker stopped."
        result = {"ok": True, "enabled": self.agent.config.self_learning.enabled, "auto_repair": self.agent.config.self_learning.auto_repair, "worker": state}
        if include_log:
            result["log"] = tail_self_learning_log(self.agent.config, lines)
        return result

    def run_self_learning_cycle(self, auto_repair: bool | None = None, repair_cooldown_seconds: float | None = None) -> dict:
        cfg = self.agent.config.self_learning
        repair_enabled = cfg.auto_repair if auto_repair is None else bool(auto_repair)
        started = time.time()
        try:
            diagnostics = self.doctor_snapshot(force=True)
            issues = diagnostics.get("issues", [])
            serious = [issue for issue in issues if issue.get("severity") in {"warn", "error"}]
            message = diagnostics.get("summary") or f"{len(issues)} issue(s) detected."
            append_self_learning_log(
                self.agent.config,
                "diagnosis",
                message,
                {"issues": [issue.get("title", "issue") for issue in issues[:8]], "serious": len(serious)},
            )
            repair = None
            cooldown_source = cfg.repair_cooldown_seconds if repair_cooldown_seconds is None else repair_cooldown_seconds
            cooldown = max(5.0, float(cooldown_source or 600))
            with self.self_learning_lock:
                last_repair_at = float(self.self_learning_state.get("last_repair_at") or 0)
            if serious and repair_enabled and time.time() - last_repair_at >= cooldown:
                append_self_learning_log(self.agent.config, "repair", "auto repair started", {"serious": len(serious)})
                repair = self.repair("auto")
                append_self_learning_log(
                    self.agent.config,
                    "repair",
                    repair.get("summary", "auto repair finished"),
                    {"ok": bool(repair.get("ok")), "steps": [step.get("name") for step in repair.get("steps", [])]},
                )
                self._set_self_learning_state(last_repair_at=time.time(), repairs=int(self.self_learning_state.get("repairs", 0) or 0) + 1)
            elif serious and repair_enabled:
                append_self_learning_log(self.agent.config, "repair", "auto repair skipped by cooldown", {"serious": len(serious)})
            duration_ms = int((time.time() - started) * 1000)
            self._set_self_learning_state(
                ok=True,
                running=bool(self.self_learning_thread and self.self_learning_thread.is_alive()),
                last_cycle_at=time.time(),
                last_error="",
                cycles=int(self.self_learning_state.get("cycles", 0) or 0) + 1,
                last_summary=message,
            )
            return {"ok": True, "duration_ms": duration_ms, "diagnostics": diagnostics, "repair": repair, "log": tail_self_learning_log(self.agent.config)}
        except Exception as exc:
            append_self_learning_log(self.agent.config, "error", str(exc))
            self._set_self_learning_state(ok=False, last_cycle_at=time.time(), last_error=str(exc))
            return {"ok": False, "error": str(exc), "log": tail_self_learning_log(self.agent.config)}

    def trigger_self_heal(self, source: str, details: dict | None = None) -> dict:
        cfg = self.agent.config.self_learning
        details = details or {}
        error = _error_summary(details)
        append_self_learning_log(self.agent.config, "error", f"{source}: {error}", _compact_heal_details(details))
        if not _repairable_error(error):
            append_self_learning_log(self.agent.config, "heal", "auto heal not needed for non-repairable error", {"source": source, "error": error})
            return {"ok": True, "started": False, "reason": "not repairable"}
        if not cfg.enabled:
            return {"ok": False, "started": False, "reason": "self learning disabled"}
        if not cfg.auto_repair:
            append_self_learning_log(self.agent.config, "heal", "auto heal skipped because auto repair is disabled", {"source": source})
            return {"ok": False, "started": False, "reason": "auto repair disabled"}
        if not cfg.heal_on_error:
            append_self_learning_log(self.agent.config, "heal", "auto heal skipped because heal-on-error is disabled", {"source": source})
            return {"ok": False, "started": False, "reason": "heal on error disabled"}
        cooldown = max(5.0, float(cfg.heal_on_error_cooldown_seconds or 30))
        now = time.time()
        with self.self_learning_lock:
            thread_alive = bool(self.self_heal_thread and self.self_heal_thread.is_alive())
            last_heal_at = float(self.self_learning_state.get("last_heal_at") or 0)
            if thread_alive:
                append_self_learning_log(self.agent.config, "heal", "auto heal already running", {"source": source})
                return {"ok": True, "started": False, "reason": "already running"}
            if now - last_heal_at < cooldown:
                append_self_learning_log(
                    self.agent.config,
                    "heal",
                    "auto heal skipped by cooldown",
                    {"source": source, "cooldown_seconds": cooldown},
                )
                return {"ok": True, "started": False, "reason": "cooldown"}
            self.self_learning_state.update(
                {
                    "healing": True,
                    "last_heal_at": now,
                    "last_heal_source": source,
                    "last_heal_error": error,
                }
            )
            self.self_heal_thread = threading.Thread(
                target=self._run_error_self_heal,
                args=(source, error),
                name="nermana-error-self-heal",
                daemon=True,
            )
            self.self_heal_thread.start()
        append_self_learning_log(self.agent.config, "heal", "auto heal started", {"source": source, "error": error})
        return {"ok": True, "started": True, "source": source}

    def trigger_self_heal_from_result(self, source: str, result: dict) -> dict | None:
        if not _result_has_repair_signal(result):
            return None
        return self.trigger_self_heal(source, result)

    def _run_error_self_heal(self, source: str, error: str) -> None:
        try:
            result = self.run_self_learning_cycle(
                auto_repair=True,
                repair_cooldown_seconds=self.agent.config.self_learning.heal_on_error_cooldown_seconds,
            )
            append_self_learning_log(
                self.agent.config,
                "heal",
                "auto heal finished",
                {
                    "source": source,
                    "ok": bool(result.get("ok")),
                    "repaired": bool(result.get("repair")),
                    "error": result.get("error", ""),
                },
            )
            with self.self_learning_lock:
                self.self_learning_state["heals"] = int(self.self_learning_state.get("heals", 0) or 0) + 1
                self.self_learning_state["last_error"] = "" if result.get("ok") else str(result.get("error", error))
        except Exception as exc:
            append_self_learning_log(self.agent.config, "heal", f"auto heal failed: {exc}", {"source": source})
            with self.self_learning_lock:
                self.self_learning_state["last_error"] = str(exc)
        finally:
            with self.self_learning_lock:
                self.self_learning_state["healing"] = False

    def _self_learning_loop(self) -> None:
        interval = max(60.0, float(self.agent.config.self_learning.interval_seconds or 300))
        try:
            while not self.self_learning_stop.is_set():
                self.run_self_learning_cycle()
                self.self_learning_stop.wait(interval)
        finally:
            self._set_self_learning_state(running=False)
            append_self_learning_log(self.agent.config, "worker", "self learning stopped")

    def _telegram_loop(self, bot: TelegramBot) -> None:
        interval = max(1.0, float(self.agent.config.telegram.poll_interval_seconds))
        try:
            while not self.telegram_stop.is_set():
                try:
                    result = bot.poll_once(timeout=20)
                    processed = int(result.get("processed", 0) or 0)
                    last_error = "" if result.get("ok") else str(result.get("error") or result.get("errors") or "poll failed")
                    if last_error:
                        self.trigger_self_heal("telegram_poll", result)
                    with self.telegram_lock:
                        self.telegram_state["ok"] = bool(result.get("ok"))
                        self.telegram_state["running"] = True
                        self.telegram_state["last_poll_at"] = time.time()
                        self.telegram_state["last_error"] = last_error
                        self.telegram_state["processed"] = int(self.telegram_state.get("processed", 0)) + processed
                        self.telegram_state["offset"] = int(result.get("offset", bot.offset) or 0)
                except Exception as exc:
                    self.trigger_self_heal("telegram_poll_exception", {"ok": False, "error": str(exc)})
                    self._set_telegram_state(ok=False, running=True, last_poll_at=time.time(), last_error=str(exc), offset=bot.offset)
                self.telegram_stop.wait(interval)
        finally:
            self._set_telegram_state(running=False)

    def _set_telegram_state(self, **updates) -> None:
        with self.telegram_lock:
            self.telegram_state.update(updates)

    def _set_self_learning_state(self, **updates) -> None:
        with self.self_learning_lock:
            self.self_learning_state.update(updates)


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
        elif path == "/api/doctor":
            force = query.get("force", ["1"])[0].lower() in {"1", "true", "yes"}
            self._json(self.server_state.doctor_snapshot(force=force))
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
        elif path == "/api/models/logs":
            self._json(self.agent.models.server_log_tail(_query_int(query, "lines", 80, 1, 500)))
        elif path == "/api/models/presets":
            self._json({"presets": list_presets()})
        elif path.startswith("/api/models/downloads/"):
            result = self.server_state.download_status(unquote(path.rsplit("/", 1)[1]))
            status = HTTPStatus.NOT_FOUND if result.get("error") == "download job not found" else HTTPStatus.OK
            self._json(result, status)
        elif path == "/api/models/download/partials":
            self._json({"ok": True, "partials": list_partial_downloads(self.agent.config)})
        elif path == "/api/models/llama":
            self._json(self.agent.models.llama_server_status())
        elif path == "/api/tools":
            self._json({"tools": self.agent.tools.list_metadata()})
        elif path == "/api/memory":
            self._json({"memories": self.agent.memory.list_memories(_query_int(query, "limit", 100, 1, 500))})
        elif path == "/api/memory/search":
            self._json({"results": [hit.__dict__ for hit in self.agent.memory.search(query.get("q", [""])[0], _query_int(query, "limit", 8, 1, 50))]})
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
            self._json({"messages": self.agent.memory.get_messages(session_id, _query_int(query, "limit", 80, 1, 500))})
        elif path == "/api/logs":
            self._json(
                {
                    "recent_sessions": self.agent.memory.list_sessions(),
                    "model_health": self.agent.models.runtime_status(),
                    "llama_log": self.agent.models.server_log_tail(80),
                    "telegram": self.server_state.telegram_worker_status(),
                    "self_learning": self.server_state.self_learning_status(include_log=True, lines=50),
                    "tools": self.agent.tools.list_metadata(),
                }
            )
        elif path == "/api/self-learning":
            self._json(self.server_state.self_learning_status(include_log=True, lines=_query_int(query, "lines", 50, 1, 500)))
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
        body, body_error = self._body()
        if body_error:
            self._json({"ok": False, "error": body_error}, HTTPStatus.BAD_REQUEST)
            return
        if path == "/api/chat":
            result = self.agent.chat(str(body.get("message", "")), str(body.get("session_id", "web")))
            heal = self.server_state.trigger_self_heal_from_result("web_chat", result)
            if heal:
                result["self_heal"] = heal
            self._json(result)
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
            result = self.agent.models.restart_server()
            heal = self.server_state.trigger_self_heal_from_result("model_restart", result)
            if heal:
                result["self_heal"] = heal
            self._json(result)
        elif path == "/api/models/stop":
            self.agent.models.stop_server()
            killed = self.agent.models.stop_external_server()
            self._json({"ok": True, "message": "Model server stop requested.", "killed_external": killed, "health": self.agent.models.server_health()})
        elif path == "/api/models/test":
            result = self.agent.models.chat([{"role": "user", "content": str(body.get("message", ""))}], max_tokens=128)
            heal = self.server_state.trigger_self_heal_from_result("model_test", result)
            if heal:
                result["self_heal"] = heal
            self._json(result)
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
        elif path.startswith("/api/models/downloads/") and path.endswith("/cancel"):
            job_id = unquote(path.split("/")[-2])
            result = self.server_state.cancel_model_download(job_id)
            self._json(result, HTTPStatus.OK if result.get("ok") else HTTPStatus.BAD_REQUEST)
        elif path == "/api/models/download/partials/delete":
            result = delete_partial_download(self.agent.config, str(body.get("filename", "")))
            self._json(result, HTTPStatus.OK if result.get("ok") else HTTPStatus.BAD_REQUEST)
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
        elif path == "/api/doctor/repair":
            self._json(self.server_state.repair(str(body.get("action", "auto"))))
        elif path == "/api/self-learning/run":
            self._json(self.server_state.run_self_learning_cycle(auto_repair=body.get("auto_repair")))
        elif path == "/api/self-learning/start":
            self._json(self.server_state.start_self_learning())
        elif path == "/api/self-learning/stop":
            self._json(self.server_state.stop_self_learning())
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
            payload = body.get("payload", {})
            if not isinstance(payload, dict):
                self._json({"ok": False, "error": "tool payload must be a JSON object"}, HTTPStatus.BAD_REQUEST)
                return
            result = self.agent.run_tool(tool_name, payload)
            heal = self.server_state.trigger_self_heal_from_result(f"tool:{tool_name}", result)
            if heal:
                result["self_heal"] = heal
            self._json(result)
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
                result = TelegramBot(self.agent, error_callback=self.server_state.trigger_self_heal).poll_once(timeout=1)
                heal = self.server_state.trigger_self_heal_from_result("telegram_poll_once", result)
                if heal:
                    result["self_heal"] = heal
                self._json(result)
            except Exception as exc:
                result = {"ok": False, "error": str(exc)}
                result["self_heal"] = self.server_state.trigger_self_heal("telegram_poll_once_exception", result)
                self._json(result)
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
            try:
                memory_id = int(path.rsplit("/", 1)[1])
            except ValueError:
                self._json({"ok": False, "error": "invalid memory id"}, HTTPStatus.BAD_REQUEST)
                return
            self._json({"ok": self.agent.memory.forget(memory_id)})
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

    def _body(self) -> tuple[dict, str]:
        try:
            length = int(self.headers.get("content-length", "0") or "0")
        except ValueError:
            return {}, "invalid content-length"
        if not length:
            return {}, ""
        raw = self.rfile.read(length).decode("utf-8", errors="replace")
        return _decode_json_body(raw)

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
        self._stream_file(target, mimetypes.guess_type(target.name)[0] or "application/octet-stream")

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
        size = target.stat().st_size
        self.send_response(HTTPStatus.OK.value)
        self.send_header("Content-Type", mimetypes.guess_type(target.name)[0] or "application/octet-stream")
        self.send_header("Content-Length", str(size))
        self.send_header("Content-Disposition", f'attachment; filename="{_download_name(target.name)}"')
        self.end_headers()
        with target.open("rb") as handle:
            shutil.copyfileobj(handle, self.wfile, 1024 * 1024)

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

    def _stream_file(self, target: Path, content_type: str) -> None:
        size = target.stat().st_size
        self.send_response(HTTPStatus.OK.value)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(size))
        self.end_headers()
        with target.open("rb") as handle:
            shutil.copyfileobj(handle, self.wfile, 1024 * 1024)

    def log_message(self, format: str, *args) -> None:
        return


def serve(host: str, port: int) -> None:
    SimpleNermanaServer().serve(host, port)


def _decode_json_body(raw: str) -> tuple[dict, str]:
    if not raw:
        return {}, ""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        return {}, f"invalid JSON body: {exc.msg}"
    if not isinstance(data, dict):
        return {}, "JSON body must be an object"
    return data, ""


def _download_name(name: str) -> str:
    return Path(name).name.replace('"', "_").replace("\r", "_").replace("\n", "_")


def _query_int(query: dict[str, list[str]], key: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(query.get(key, [str(default)])[0])
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


def _model_loading_error(message: str) -> bool:
    lower = str(message or "").lower()
    return "loading model" in lower or ("503" in lower and "service unavailable" in lower)


def _repair_step_skippable(step: dict) -> bool:
    result = step.get("result") or {}
    return bool(result.get("skipped"))


def _result_has_repair_signal(result: dict) -> bool:
    if not isinstance(result, dict):
        return False
    if result.get("ok") is False and _repairable_error(result.get("error", "")):
        return True
    for key in ["model_error", "original_model_error", "error"]:
        if _repairable_error(result.get(key, "")):
            return True
    for tool_result in result.get("tool_results", []) or []:
        if isinstance(tool_result, dict) and tool_result.get("ok") is False and _repairable_error(tool_result.get("error", "")):
            return True
    errors = result.get("errors")
    if isinstance(errors, list) and any(_repairable_error(item) for item in errors):
        return True
    return False


def _repairable_error(error: object) -> bool:
    text = str(error or "").lower()
    if not text:
        return False
    ignored = [
        "message is required",
        "invalid json",
        "json body",
        "file not found",
        "folder not found",
        "location not found",
        "prompt is required",
        "tool payload",
        "unknown tool",
        "not in allowed_termux_commands",
        "metacharacters",
        "disabled or missing token",
        "token is missing",
    ]
    if any(item in text for item in ignored):
        return False
    signals = [
        "connection refused",
        "failed to establish",
        "timed out",
        "timeout",
        "loading model",
        "service unavailable",
        "http error 400",
        "bad request",
        "context",
        "llama",
        "model",
        "telegram",
        "webhook",
        "poll",
        "bot not found",
        "unauthorized",
        "unavailable",
        "offline",
        "not reachable",
        "not responding",
    ]
    return any(signal in text for signal in signals)


def _error_summary(details: dict | None) -> str:
    if not isinstance(details, dict):
        return str(details or "unknown error")
    for key in ["error", "model_error", "original_model_error", "message"]:
        value = details.get(key)
        if value:
            return str(value)
    errors = details.get("errors")
    if errors:
        return str(errors)
    for tool_result in details.get("tool_results", []) or []:
        if isinstance(tool_result, dict) and tool_result.get("ok") is False:
            return str(tool_result.get("error") or "tool failed")
    return "error detected"


def _compact_heal_details(details: dict | None) -> dict:
    if not isinstance(details, dict):
        return {"detail": str(details or "")}
    compact = {
        "ok": details.get("ok"),
        "error": _error_summary(details),
    }
    if details.get("tool"):
        compact["tool"] = details.get("tool")
    if details.get("model"):
        compact["model"] = details.get("model")
    if details.get("tool_results"):
        compact["tools"] = [
            {"tool": item.get("tool"), "ok": item.get("ok"), "error": item.get("error", "")}
            for item in details.get("tool_results", [])[:4]
            if isinstance(item, dict)
        ]
    return compact


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
