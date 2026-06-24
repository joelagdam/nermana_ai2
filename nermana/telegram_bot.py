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
        ready = self.status()
        if not ready.get("ok"):
            raise RuntimeError(ready.get("error", "Telegram is not ready."))
        clear = self.delete_webhook(drop_pending_updates=False)
        if not clear.get("ok"):
            print(f"telegram: {clear.get('error', 'webhook clear failed')}")
        while True:
            result = self.poll_once(timeout=20)
            if not result.get("ok"):
                print(f"telegram: {result.get('error') or result.get('errors') or 'poll failed'}")
            time.sleep(self.config.poll_interval_seconds)

    def status(self) -> dict[str, Any]:
        if not self.config.enabled:
            return {"ok": False, "enabled": False, "error": "Telegram is disabled in settings."}
        if not self.config.token or self.config.token == "***":
            return {"ok": False, "enabled": True, "error": "Telegram token is missing. Paste a real BotFather token first."}
        me = self.get_me()
        if not me.get("ok"):
            me["enabled"] = True
            me["offset"] = self.offset
            return me
        return {
            "ok": True,
            "enabled": True,
            "bot": me.get("bot", {}),
            "offset": self.offset,
            "allowed_user_ids": sorted(self.allowed_user_ids),
            "poll_interval_seconds": self.config.poll_interval_seconds,
        }

    def get_me(self) -> dict[str, Any]:
        response = get_json(f"{self.base_url}/getMe", timeout=10)
        if not response.ok:
            return self._api_error(response, "getMe")
        if response.data.get("ok") is False:
            return self._api_payload_error(response.data, "getMe")
        bot = response.data.get("result") or {}
        return {
            "ok": True,
            "bot": {
                "id": bot.get("id"),
                "username": bot.get("username"),
                "first_name": bot.get("first_name"),
                "can_join_groups": bot.get("can_join_groups"),
                "can_read_all_group_messages": bot.get("can_read_all_group_messages"),
            },
        }

    def delete_webhook(self, drop_pending_updates: bool = False) -> dict[str, Any]:
        response = post_json(
            f"{self.base_url}/deleteWebhook",
            {"drop_pending_updates": bool(drop_pending_updates)},
            timeout=10,
        )
        if not response.ok:
            return self._api_error(response, "deleteWebhook")
        if response.data.get("ok") is False:
            return self._api_payload_error(response.data, "deleteWebhook")
        return {"ok": True, "description": response.data.get("description", "Webhook cleared."), "drop_pending_updates": bool(drop_pending_updates)}

    def poll_once(self, timeout: int = 20) -> dict[str, Any]:
        response = get_json(f"{self.base_url}/getUpdates", {"timeout": timeout, "offset": self.offset}, timeout=timeout + 5)
        if not response.ok:
            error = self._api_error(response, "getUpdates")
            if error.get("needs_webhook_clear"):
                clear = self.delete_webhook(drop_pending_updates=False)
                if not clear.get("ok"):
                    return {**error, "webhook_clear": clear}
                response = get_json(f"{self.base_url}/getUpdates", {"timeout": timeout, "offset": self.offset}, timeout=timeout + 5)
                if not response.ok:
                    return {**self._api_error(response, "getUpdates"), "webhook_clear": clear}
            else:
                return error
        if response.data.get("ok") is False:
            error = self._api_payload_error(response.data, "getUpdates")
            if error.get("needs_webhook_clear"):
                clear = self.delete_webhook(drop_pending_updates=False)
                if not clear.get("ok"):
                    return {**error, "webhook_clear": clear}
                response = get_json(f"{self.base_url}/getUpdates", {"timeout": timeout, "offset": self.offset}, timeout=timeout + 5)
                if response.ok and response.data.get("ok") is not False:
                    error = {"ok": True}
                elif response.ok:
                    return {**self._api_payload_error(response.data, "getUpdates"), "webhook_clear": clear}
                else:
                    return {**self._api_error(response, "getUpdates"), "webhook_clear": clear}
            if not error.get("ok"):
                return error
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

    def reset_offset(self, drop_pending_updates: bool = False) -> dict[str, Any]:
        self.offset = 0
        try:
            self.offset_path.unlink(missing_ok=True)
        except OSError as exc:
            return {"ok": False, "error": str(exc)}
        if drop_pending_updates:
            dropped = self.drop_pending_updates()
            if not dropped.get("ok"):
                return dropped
        return {"ok": True, "offset": self.offset, "drop_pending_updates": bool(drop_pending_updates)}

    def drop_pending_updates(self) -> dict[str, Any]:
        clear = self.delete_webhook(drop_pending_updates=True)
        if not clear.get("ok"):
            return clear
        response = get_json(f"{self.base_url}/getUpdates", {"timeout": 0, "offset": -1}, timeout=5)
        if not response.ok:
            return self._api_error(response, "dropPendingUpdates")
        if response.data.get("ok") is False:
            return self._api_payload_error(response.data, "dropPendingUpdates")
        updates = response.data.get("result") or []
        if updates:
            self.offset = max(int(update.get("update_id", -1)) + 1 for update in updates)
            self._save_offset()
        return {"ok": True, "offset": self.offset, "webhook": clear}

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
                return self._api_error(response, "sendMessage")
            if response.data.get("ok") is False:
                return self._api_payload_error(response.data, "sendMessage")
        return {"ok": True, "chunks": len(chunks)}

    def _typing_loop(self, chat_id: int, stop: threading.Event) -> None:
        while not stop.is_set():
            post_json(f"{self.base_url}/sendChatAction", {"chat_id": chat_id, "action": "typing"}, timeout=5)
            stop.wait(4)

    def _api_payload_error(self, data: dict[str, Any], action: str) -> dict[str, Any]:
        description = str(data.get("description") or f"Telegram {action} failed")
        return self._telegram_error(description, int(data.get("error_code") or 0), action)

    def _api_error(self, response, action: str) -> dict[str, Any]:
        return self._telegram_error(response.error or f"Telegram {action} failed", response.status, action)

    def _telegram_error(self, message: str, status: int, action: str) -> dict[str, Any]:
        lower = message.lower()
        if status == 404 or "not found" in lower:
            return {
                "ok": False,
                "error": f"Telegram {action} failed: bot not found. Check that the token is the exact BotFather token.",
                "status": status,
            }
        if status == 401 or "unauthorized" in lower:
            return {
                "ok": False,
                "error": f"Telegram {action} failed: token is unauthorized. Recheck the BotFather token.",
                "status": status,
            }
        if "conflict" in lower and ("webhook" in lower or "getupdates" in lower):
            return {
                "ok": False,
                "error": "Telegram polling is blocked because a webhook is active. Clear webhook, then start polling again.",
                "status": status,
                "needs_webhook_clear": True,
            }
        return {"ok": False, "error": f"Telegram {action} failed: {message}", "status": status}


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
