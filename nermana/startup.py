from __future__ import annotations

import os
import signal
import threading
from typing import Any

from .agent import AgentCore
from .config import load_config, save_config
from .simple_server import SimpleNermanaServer


class StartupManager:
    def __init__(self):
        self.agent = AgentCore(load_config())
        self.web_server = SimpleNermanaServer(self.agent)
        self.web_host, self.web_port = self._server_binding()
        self.telegram_thread: threading.Thread | None = None
        self.memory_thread: threading.Thread | None = None
        self.stop_event = threading.Event()
        self.model_started = False

    def _server_binding(self) -> tuple[str, int]:
        host = os.environ.get("NERMANA_HOST") or self.agent.config.server.host
        port_text = os.environ.get("NERMANA_PORT")
        port = self.agent.config.server.port
        if port_text:
            try:
                port = int(port_text)
            except ValueError as exc:
                raise SystemExit(f"invalid NERMANA_PORT: {port_text}") from exc
        return host, port

    def start(self) -> None:
        self._select_first_model_if_needed()
        self._start_model_if_available()
        self._start_memory_if_available()
        self._start_telegram_if_available()
        self._start_self_learning_if_available()
        self._serve_web()

    def _select_first_model_if_needed(self) -> None:
        if self.agent.config.model.active_model:
            return
        first = next((model for model in self.agent.models.scan() if model.loadable), None)
        if not first:
            return
        self.agent.config.model.active_model = first.name
        save_config(self.agent.config)

    def _start_model_if_available(self) -> None:
        if not self.agent.config.model.active_model:
            print("model: no .gguf selected or available")
            return
        health = self.agent.models.runtime_status()
        if health.get("ok"):
            print("model: llama-server already responding to chat")
            return
        if health.get("endpoint_ok"):
            print(f"model: endpoint is up but chat is not ready ({health.get('chat_check', {}).get('error') or health.get('state')})")
        result = self.agent.models.restart_server()
        self.model_started = bool(result.get("started_process"))
        if result.get("ok"):
            print("model: started and responding")
        elif self.model_started:
            print("model: started, still warming up")
        else:
            print(f"model: {result.get('error', 'not started')}")

    def _start_telegram_if_available(self) -> None:
        cfg = self.agent.config.telegram
        if not cfg.enabled or not cfg.token:
            print("telegram: disabled")
            return
        result = self.web_server.start_telegram()
        if not result.get("ok"):
            print(f"telegram: {result.get('error', 'not ready')}")
            return
        print("telegram: polling")

    def _start_memory_if_available(self) -> None:
        cfg = self.agent.config.memory
        if not cfg.auto_remember:
            print("memory: auto memory disabled")
            return
        self.memory_thread = threading.Thread(target=self._memory_loop, name="nermana-memory", daemon=True)
        self.memory_thread.start()
        print("memory: always-on consolidation")

    def _memory_loop(self) -> None:
        cfg = self.agent.config.memory
        interval = max(60.0, float(cfg.consolidate_every_seconds))
        while not self.stop_event.wait(interval):
            try:
                result = self.agent.memory.consolidate_due(min_items=max(2, int(cfg.min_consolidate_items)))
                if result.get("consolidated"):
                    print(f"memory: consolidated {len(result.get('source_ids', []))} memories")
            except Exception as exc:
                print(f"memory: consolidation error: {exc}")

    def _start_self_learning_if_available(self) -> None:
        if not self.agent.config.self_learning.enabled:
            print("self-learning: disabled")
            return
        result = self.web_server.start_self_learning()
        if result.get("ok"):
            print("self-learning: always-on diagnostics")
        else:
            print(f"self-learning: {result.get('error', 'not started')}")

    def _serve_web(self) -> None:
        host = self.web_host
        port = self.web_port
        self._install_signal_handlers()
        print("web: starting")
        try:
            self.web_server.serve(host, port)
        finally:
            self.shutdown()

    def _install_signal_handlers(self) -> None:
        def handler(signum: int, _frame: Any) -> None:
            print(f"shutdown: signal {signum}")
            self.shutdown()
            raise SystemExit(0)

        signal.signal(signal.SIGINT, handler)
        signal.signal(signal.SIGTERM, handler)

    def shutdown(self) -> None:
        self.stop_event.set()
        self.web_server.stop_workers()
        if self.model_started:
            self.agent.models.stop_server()


def main() -> None:
    StartupManager().start()


if __name__ == "__main__":
    main()
