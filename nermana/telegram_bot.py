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
        self._busy_lock = threading.Lock()
        self._busy_chats: dict[int, threading.Thread] = {}

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
            session_id = f"telegram-{chat_id}"
            if self._chat_busy(int(chat_id)):
                sent = self._send(chat_id, self._busy_wait_text())
                if not sent.get("ok"):
                    errors.append(sent.get("error", "send failed"))
                processed += 1
                continue
            if self._should_send_tool_wait(text, session_id):
                waiting = self._send(chat_id, self._tool_wait_text(text, session_id))
                if not waiting.get("ok"):
                    errors.append(waiting.get("error", "send failed"))
                self._start_chat_worker(int(chat_id), session_id, text)
                processed += 1
                continue
            stop_typing = threading.Event()
            typing_thread = threading.Thread(target=self._typing_loop, args=(chat_id, stop_typing), daemon=True)
            typing_thread.start()
            try:
                chat_result = self.agent.chat(text, session_id=session_id)
            except Exception as exc:
                chat_result = {"ok": False, "error": str(exc)}
            finally:
                stop_typing.set()
            for reply in self._reply_batches(chat_result):
                sent = self._send(chat_id, reply)
                if not sent.get("ok"):
                    errors.append(sent.get("error", "send failed"))
                    break
            processed += 1
        self._save_offset()
        return {"ok": not errors, "processed": processed, "errors": errors, "offset": self.offset}

    def _start_chat_worker(self, chat_id: int, session_id: str, text: str) -> None:
        thread = threading.Thread(target=self._run_chat_worker, args=(chat_id, session_id, text), daemon=True)
        with self._busy_lock:
            self._busy_chats[chat_id] = thread
        thread.start()

    def _run_chat_worker(self, chat_id: int, session_id: str, text: str) -> None:
        stop_typing = threading.Event()
        typing_thread = threading.Thread(target=self._typing_loop, args=(chat_id, stop_typing), daemon=True)
        typing_thread.start()
        try:
            try:
                chat_result = self.agent.chat(text, session_id=session_id)
            except Exception as exc:
                chat_result = {"ok": False, "error": str(exc)}
            for reply in self._reply_batches(chat_result):
                sent = self._send(chat_id, reply)
                if not sent.get("ok"):
                    print(f"telegram: {sent.get('error', 'send failed')}")
                    break
        finally:
            stop_typing.set()
            with self._busy_lock:
                current = self._busy_chats.get(chat_id)
                if current is threading.current_thread():
                    self._busy_chats.pop(chat_id, None)

    def _chat_busy(self, chat_id: int) -> bool:
        with self._busy_lock:
            thread = self._busy_chats.get(chat_id)
            if thread and thread.is_alive():
                return True
            self._busy_chats.pop(chat_id, None)
            return False

    def wait_for_idle(self, timeout: float = 5.0) -> None:
        deadline = time.time() + timeout
        while True:
            with self._busy_lock:
                threads = [thread for thread in self._busy_chats.values() if thread.is_alive()]
            if not threads or time.time() >= deadline:
                return
            for thread in threads:
                thread.join(max(0.01, min(0.1, deadline - time.time())))

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
        reply_context = self._reply_context(message)
        if text and reply_context:
            text = f"{text}\n\nTelegram reply context: {reply_context}"
        return message, text

    def _reply_context(self, message: dict[str, Any]) -> str:
        reply = message.get("reply_to_message") or {}
        if not isinstance(reply, dict):
            return ""
        text = str(reply.get("text") or reply.get("caption") or "").strip()
        if not text:
            return ""
        author = reply.get("from") or {}
        name = author.get("username") or author.get("first_name") or author.get("id") or "message"
        compact = " ".join(text.split())
        if len(compact) > 900:
            compact = compact[:897] + "..."
        return f"quoted from {name}: {compact}"

    def _should_send_tool_wait(self, text: str, session_id: str = "") -> bool:
        lower = text.lower()
        markers = [
            "/weather",
            "/search",
            "/read",
            "/phone",
            "/image",
            "/vision",
            "/termux",
            "weather",
            "forecast",
            "search",
            "look up",
            "latest",
            "latitude",
            "longitude",
            "coordinates",
            "telegram reply context",
        ]
        return any(marker in lower for marker in markers) or bool(self._confirmed_tool_target(text, session_id))

    def _tool_wait_text(self, text: str, session_id: str = "") -> str:
        lower = text.lower()
        target = self._confirmed_tool_target(text, session_id)
        if target == "current_weather" or "weather" in lower or "forecast" in lower or "latitude" in lower or "longitude" in lower or "coordinates" in lower:
            return "⏳ Checking the weather/location tool. I’ll summarize the result when it returns."
        if target == "web_search" or "/search" in lower or "search" in lower or "look up" in lower or "latest" in lower:
            return "⏳ Searching now. I’ll send the useful summary when the results return."
        return "⏳ Using the needed tool now. I’ll summarize the result when it returns."

    def _busy_wait_text(self) -> str:
        return "⏳ Still working on the current result. Please wait; I’ll send the summary as soon as it finishes."

    def _confirmed_tool_target(self, text: str, session_id: str) -> str:
        if not self._is_confirmation_text(text):
            return ""
        pending = self.agent.pending_actions.get(session_id)
        if pending:
            return str(pending.get("tool") or "")
        previous = self._previous_assistant_message(session_id).lower()
        if any(word in previous for word in ["weather", "forecast", "open-meteo", "latitude", "longitude", "coordinates"]):
            return "current_weather"
        if any(phrase in previous for phrase in ["search for", "look up", "search online", "find online"]):
            return "web_search"
        return ""

    def _is_confirmation_text(self, text: str) -> bool:
        lowered = " ".join(str(text or "").lower().strip().split())
        lowered = lowered.rstrip(".!?")
        return lowered in {"yes", "y", "yeah", "yep", "sure", "ok", "okay", "go", "go ahead", "do it", "please do", "run it"}

    def _previous_assistant_message(self, session_id: str) -> str:
        try:
            messages = self.agent.memory.get_messages(session_id, limit=8)
        except Exception:
            return ""
        for item in reversed(messages):
            if item.get("role") == "assistant":
                return str(item.get("content") or "")
        return ""

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

    def _reply_batches(self, result: dict[str, Any]) -> list[str]:
        if not isinstance(result, dict):
            return ["Nermana hit an internal chat error before a reply was prepared."]
        if not result.get("ok", True):
            return [self._short_chat_error(str(result.get("error") or "unknown error"))]
        model_error = str(result.get("model_error") or result.get("original_model_error") or "")
        if model_error and not result.get("model_ok", True) and not result.get("tool_results"):
            return [self._short_chat_error(model_error)]
        batches = result.get("reply_batches")
        if isinstance(batches, list) and batches:
            replies = [str(item).strip() for item in batches if str(item).strip()]
        else:
            replies = [str(result.get("reply") or "").strip()]
        return [self._safe_reply_text(reply) for reply in replies if reply] or ["(empty)"]

    def _safe_reply_text(self, text: str) -> str:
        if self._is_context_error(text):
            return self._short_chat_error(text)
        return text

    def _short_chat_error(self, message: str) -> str:
        lower = message.lower()
        if self._is_context_error(message):
            return (
                "The local model rejected this turn because llama.cpp is running with too small a context window. "
                "Open Models > Server and restart it with a larger context, or use the compact phone preset."
            )
        if any(part in lower for part in ["connection refused", "failed to establish", "not responding", "timed out", "timeout"]):
            return "The local model is not reachable right now. Web/core memory still works; start or restart llama.cpp from Models."
        if "loading model" in lower or ("http error 503" in lower and "service unavailable" in lower):
            return "The local model is still loading. I am online, but the GGUF voice engine is not ready yet. Open Web > Doctor and run Repair Local Model, or retry after it finishes loading."
        if "bad request" in lower or "http error 400" in lower:
            return "The local model rejected the request. Check the active model, server model id, and context size in Models."
        compact = " ".join(message.split())
        return f"Nermana hit a chat error: {compact[:220]}"

    def _is_context_error(self, message: str) -> bool:
        lower = message.lower()
        return "context" in lower and any(word in lower for word in ["exceed", "available", "tokens", "window"])

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
        if status == 0 and any(word in lower for word in ["timed out", "timeout", "network", "temporary failure", "name or service", "connection refused", "no route", "unreachable"]):
            return {
                "ok": False,
                "error": f"Telegram {action} is offline. Internet or Telegram is unreachable; local Nermana stays running.",
                "status": status,
                "offline": True,
            }
        if status == 404 or "not found" in lower:
            return {
                "ok": False,
                "error": f"Telegram {action} failed: bot not found. Check the BotFather token.",
                "status": status,
            }
        if status == 401 or "unauthorized" in lower:
            return {
                "ok": False,
                "error": f"Telegram {action} failed: token unauthorized. Recheck the BotFather token.",
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
