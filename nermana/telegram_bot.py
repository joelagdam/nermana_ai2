from __future__ import annotations

import time
import threading
from typing import Any

from .agent import AgentCore
from .config import resolve_path
from .http_client import get_json, post_json


class TelegramBot:
    def __init__(self, agent: AgentCore):
        self.agent = agent
        self.config = agent.config.telegram
        self.base_url = f"https://api.telegram.org/bot{self.config.token}"
        self.offset_path = resolve_path(self.config.offset_path)
        self.offset = self._load_offset()
        self.allowed_user_ids = {int(user_id) for user_id in self.config.allowed_user_ids if str(user_id).strip().isdigit()}

    def run_forever(self) -> None:
        if not self.config.enabled or not self.config.token:
            raise RuntimeError("Telegram is disabled or token is missing.")
        while True:
            result = self.poll_once(timeout=20)
            if not result.get("ok"):
                print(f"telegram: {result.get('error') or result.get('errors') or 'poll failed'}")
            time.sleep(self.config.poll_interval_seconds)

    def poll_once(self, timeout: int = 20) -> dict[str, Any]:
        response = get_json(f"{self.base_url}/getUpdates", {"timeout": timeout, "offset": self.offset}, timeout=timeout + 5)
        if not response.ok:
            return {"ok": False, "error": response.error}
        if response.data.get("ok") is False:
            return {"ok": False, "error": response.data.get("description", "Telegram getUpdates failed")}
        processed = 0
        errors = []
        for update in response.data.get("result", []):
            self.offset = max(self.offset, int(update["update_id"]) + 1)
            self._save_offset()
            message, text = self._message_text(update)
            chat = message.get("chat") or {}
            user = message.get("from") or {}
            chat_id = chat.get("id")
            user_id = user.get("id")
            if not text or chat_id is None:
                continue
            if self.allowed_user_ids and int(user_id or 0) not in self.allowed_user_ids:
                sent = self._send(chat_id, "This bot is private.")
                if not sent.get("ok"):
                    errors.append(sent.get("error", "send failed"))
                continue
            if self._is_start_command(text):
                sent = self._send(chat_id, "Nermana is online. Send a message, /weather, /search, /read, /phone, /image, or /vision.")
                if not sent.get("ok"):
                    errors.append(sent.get("error", "send failed"))
                processed += 1
                continue
            stop_typing = threading.Event()
            typing_thread = threading.Thread(target=self._typing_loop, args=(chat_id, stop_typing), daemon=True)
            typing_thread.start()
            try:
                reply = self.agent.chat(text, session_id=f"telegram-{chat_id}")["reply"]
            finally:
                stop_typing.set()
            sent = self._send(chat_id, reply)
            if not sent.get("ok"):
                errors.append(sent.get("error", "send failed"))
            processed += 1
        self._save_offset()
        return {"ok": not errors, "processed": processed, "errors": errors, "offset": self.offset}

    def reset_offset(self) -> dict[str, Any]:
        self.offset = 0
        try:
            self.offset_path.unlink(missing_ok=True)
        except OSError as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "offset": self.offset}

    def _message_text(self, update: dict[str, Any]) -> tuple[dict[str, Any], str]:
        if update.get("callback_query"):
            callback = update["callback_query"]
            message = callback.get("message") or {}
            message["from"] = callback.get("from") or message.get("from") or {}
            return message, str(callback.get("data") or "").strip()
        message = (
            update.get("message")
            or update.get("edited_message")
            or update.get("channel_post")
            or update.get("edited_channel_post")
            or {}
        )
        text = str(message.get("text") or message.get("caption") or "").strip()
        return message, text

    def _is_start_command(self, text: str) -> bool:
        lowered = text.strip().lower()
        return lowered == "start" or lowered == "/start" or lowered.startswith("/start@")

    def _load_offset(self) -> int:
        try:
            return int(self.offset_path.read_text(encoding="utf-8").strip() or "0")
        except (OSError, ValueError):
            return 0

    def _save_offset(self) -> None:
        self.offset_path.parent.mkdir(parents=True, exist_ok=True)
        self.offset_path.write_text(str(self.offset), encoding="utf-8")

    def _send(self, chat_id: int, text: str) -> dict[str, Any]:
        chunks = _telegram_chunks(text or "(empty)")
        for chunk in chunks:
            response = post_json(f"{self.base_url}/sendMessage", {"chat_id": chat_id, "text": chunk}, timeout=10)
            if not response.ok:
                return {"ok": False, "error": response.error}
            if response.data.get("ok") is False:
                return {"ok": False, "error": response.data.get("description", "Telegram sendMessage failed")}
        return {"ok": True, "chunks": len(chunks)}

    def _typing_loop(self, chat_id: int, stop: threading.Event) -> None:
        while not stop.is_set():
            post_json(f"{self.base_url}/sendChatAction", {"chat_id": chat_id, "action": "typing"}, timeout=5)
            stop.wait(4)


def _telegram_chunks(text: str, limit: int = 3900) -> list[str]:
    if len(text) <= limit:
        return [text]
    chunks = []
    current = text
    while current:
        cut = current.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = limit
        chunks.append(current[:cut].strip())
        current = current[cut:].strip()
    return chunks
