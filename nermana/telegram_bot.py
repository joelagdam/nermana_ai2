from __future__ import annotations

import time
import threading
from typing import Any

from .agent import AgentCore
from .http_client import get_json, post_json


class TelegramBot:
    def __init__(self, agent: AgentCore):
        self.agent = agent
        self.config = agent.config.telegram
        self.base_url = f"https://api.telegram.org/bot{self.config.token}"
        self.offset = 0

    def run_forever(self) -> None:
        if not self.config.enabled or not self.config.token:
            raise RuntimeError("Telegram is disabled or token is missing.")
        while True:
            self.poll_once()
            time.sleep(self.config.poll_interval_seconds)

    def poll_once(self) -> dict[str, Any]:
        response = get_json(f"{self.base_url}/getUpdates", {"timeout": 20, "offset": self.offset}, timeout=25)
        if not response.ok:
            return {"ok": False, "error": response.error}
        processed = 0
        for update in response.data.get("result", []):
            self.offset = max(self.offset, int(update["update_id"]) + 1)
            message = update.get("message") or update.get("edited_message") or {}
            chat = message.get("chat") or {}
            user = message.get("from") or {}
            text = message.get("text", "")
            chat_id = chat.get("id")
            user_id = user.get("id")
            if not text or chat_id is None:
                continue
            if self.config.allowed_user_ids and user_id not in self.config.allowed_user_ids:
                self._send(chat_id, "This bot is private.")
                continue
            stop_typing = threading.Event()
            typing_thread = threading.Thread(target=self._typing_loop, args=(chat_id, stop_typing), daemon=True)
            typing_thread.start()
            try:
                reply = self.agent.chat(text, session_id=f"telegram-{chat_id}")["reply"]
            finally:
                stop_typing.set()
            self._send(chat_id, reply[:3900])
            processed += 1
        return {"ok": True, "processed": processed}

    def _send(self, chat_id: int, text: str) -> None:
        post_json(f"{self.base_url}/sendMessage", {"chat_id": chat_id, "text": text}, timeout=10)

    def _typing_loop(self, chat_id: int, stop: threading.Event) -> None:
        while not stop.is_set():
            post_json(f"{self.base_url}/sendChatAction", {"chat_id": chat_id, "action": "typing"}, timeout=5)
            stop.wait(4)
