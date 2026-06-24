from __future__ import annotations

import errno
import os
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from nermana.agent import AgentCore
from nermana.capabilities import Capability
from nermana.config import AppConfig, FileConfig, MemoryConfig, ModelConfig, SafetyConfig, SearchConfig, TelegramConfig, merge_config, save_config
from nermana.http_client import HttpResponse
from nermana.model_downloads import download_model, list_presets
from nermana.memory import MemoryStore
from nermana.models import ModelManager
from nermana.safety import DecisionGate
from nermana.simple_server import SimpleNermanaServer
from nermana.startup import StartupManager
from nermana.telegram_bot import TelegramBot
from nermana.tooling import Tool, ToolRegistry
from nermana.tools.files import register_file_tools
from nermana.tools.search import register_search_tools


class FakeUrlResponse:
    def __init__(self, chunks: list[bytes], headers: dict[str, str] | None = None):
        self.chunks = chunks
        self.headers = headers or {}
        self.status = 200

    def __enter__(self):
        return self

    def __exit__(self, _exc_type, _exc, _tb):
        return False

    def read(self, _size: int = -1) -> bytes:
        if not self.chunks:
            return b""
        return self.chunks.pop(0)


class ConfigTests(unittest.TestCase):
    def test_merge_config_updates_nested_values(self) -> None:
        cfg = AppConfig()
        updated = merge_config(cfg, {"model": {"temperature": 0.25}, "search": {"enabled": False}})
        self.assertEqual(updated.model.temperature, 0.25)
        self.assertFalse(updated.search.enabled)
        self.assertIsInstance(updated.model, ModelConfig)


class ModelTests(unittest.TestCase):
    def test_scan_and_switch_gguf(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            model_dir = Path(tmp) / "models"
            model_dir.mkdir()
            (model_dir / "tiny.gguf").write_bytes(b"model")
            (model_dir / "typo.guff").write_bytes(b"typo")
            cfg = AppConfig(model=ModelConfig(models_dir=str(model_dir)))
            manager = ModelManager(cfg, persist=False)
            models = manager.scan()
            self.assertEqual({model.name for model in models}, {"tiny.gguf", "typo.guff"})
            self.assertTrue(next(model for model in models if model.name == "tiny.gguf").loadable)
            self.assertFalse(next(model for model in models if model.name == "typo.guff").loadable)
            self.assertTrue(manager.switch("tiny.gguf")["ok"])
            self.assertEqual(cfg.model.active_model, "tiny.gguf")
            self.assertFalse(manager.switch("typo.guff")["ok"])

    def test_check_and_delete_idle_model(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            model_dir = Path(tmp) / "models"
            model_dir.mkdir()
            model = model_dir / "idle.gguf"
            model.write_bytes(b"model")
            cfg = AppConfig(model=ModelConfig(models_dir=str(model_dir), active_model=""))
            manager = ModelManager(cfg, persist=False)
            check = manager.check_model("idle.gguf")
            self.assertTrue(check["ok"])
            self.assertEqual(check["status"], "idle")
            deleted = manager.delete_model("idle.gguf")
            self.assertTrue(deleted["ok"])
            self.assertFalse(model.exists())

    def test_detects_llama_server_in_home_build(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            binary = home / "llama.cpp" / "build" / "bin" / "llama-server"
            binary.parent.mkdir(parents=True)
            binary.write_text("#!/bin/sh\n", encoding="utf-8")
            cfg = AppConfig(model=ModelConfig(llama_server_path="auto"))
            manager = ModelManager(cfg, persist=False)
            with patch("pathlib.Path.home", return_value=home), patch("shutil.which", return_value=None):
                self.assertEqual(manager.resolve_llama_server(), str(binary))
                self.assertTrue(manager.llama_server_status()["available"])

    def test_model_download_rejects_non_gguf_and_non_http(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = AppConfig(model=ModelConfig(models_dir=str(Path(tmp) / "models")))
            self.assertFalse(download_model(cfg, "file:///tmp/model.gguf")["ok"])
            self.assertFalse(download_model(cfg, "https://example.com/model.bin")["ok"])
            self.assertTrue(any(preset["id"] == "qwen25_15b_instruct_q4" for preset in list_presets()))

    def test_model_download_reports_progress(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = AppConfig(model=ModelConfig(models_dir=str(Path(tmp) / "models")))
            updates = []
            response = FakeUrlResponse([b"ab", b"cde", b""], {"Content-Length": "5"})
            with patch("urllib.request.urlopen", return_value=response):
                result = download_model(cfg, "https://example.com/model.gguf", progress=updates.append)
            self.assertTrue(result["ok"])
            self.assertEqual(result["size_bytes"], 5)
            self.assertEqual(updates[-1]["bytes_read"], 5)
            self.assertEqual(updates[-1]["percent"], 100)

    def test_llama_command_uses_fast_phone_settings(self) -> None:
        cfg = AppConfig(model=ModelConfig(threads=0, batch_size=256, ubatch_size=64, parallel_slots=1, mlock=True, no_mmap=True))
        manager = ModelManager(cfg, persist=False)
        command = manager._server_command("llama-server", Path("models/test.gguf"), 8080, fast=True)
        self.assertIn("--mlock", command)
        self.assertIn("--no-mmap", command)
        self.assertIn("-b", command)
        self.assertIn("256", command)


class MemoryTests(unittest.TestCase):
    def test_memory_retains_and_searches(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = MemoryStore(MemoryConfig(db_path=str(Path(tmp) / "memory.sqlite3")))
            memory_id = store.remember("The preferred nickname is Kent.", tags="profile", source="test")
            hits = store.search("nickname")
            self.assertTrue(any(hit.id == memory_id for hit in hits))
            self.assertEqual(store.count_memories(), 1)
            self.assertTrue(store.forget(memory_id))

    def test_memory_update_keeps_search_index_current(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = MemoryStore(MemoryConfig(db_path=str(Path(tmp) / "memory.sqlite3")))
            memory_id = store.remember("old phrase", tags="old", source="test")
            result = store.update_memory(memory_id, {"content": "new searchable phrase", "tags": "new", "importance": 0.9})
            self.assertTrue(result["ok"])
            hits = store.search("searchable")
            self.assertTrue(any(hit.id == memory_id for hit in hits))

    def test_memory_consolidates_structured_records(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = MemoryStore(MemoryConfig(db_path=str(Path(tmp) / "memory.sqlite3")))
            store.remember("Nermana should answer weather as a short summary.", tags="nermana,weather", source="test")
            store.remember("Nermana needs search results summarized instead of raw JSON.", tags="nermana,search", source="test")
            result = store.consolidate(limit=4)
            self.assertTrue(result["consolidated"])
            self.assertEqual(store.count_unconsolidated(), 0)
            self.assertTrue(store.list_consolidations())

    def test_relative_memory_path_uses_project_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            other = Path(tmp) / "other"
            root.mkdir()
            other.mkdir()
            previous = Path.cwd()
            try:
                os.chdir(other)
                with patch("nermana.config.PROJECT_ROOT", root):
                    store = MemoryStore(MemoryConfig(db_path="data/test.sqlite3"))
                    self.assertEqual(store.path, root / "data" / "test.sqlite3")
                    store.remember("relative path works", tags="test")
                    self.assertTrue(store.path.exists())
                    self.assertFalse((other / "data" / "test.sqlite3").exists())
            finally:
                os.chdir(previous)


class SafetyTests(unittest.TestCase):
    def test_gate_blocks_dangerous_and_allows_power(self) -> None:
        gate = DecisionGate(SafetyConfig(max_tool_risk="power"))
        self.assertTrue(gate.evaluate("settings_put", "power").allowed)
        self.assertFalse(gate.evaluate("phone_shell", "dangerous").allowed)


class ToolTests(unittest.TestCase):
    def test_registry_respects_availability_and_safety(self) -> None:
        cfg = AppConfig(search=SearchConfig(enabled=False))
        registry = ToolRegistry(cfg)
        register_search_tools(registry, cfg)
        result = registry.run("web_search", {"query": "hello"})
        self.assertFalse(result["ok"])
        self.assertIn("unavailable", result["error"])

    def test_duckduckgo_search_provider_parses_results(self) -> None:
        html = b"""
        <a class="result__a" href="https://duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fpage">Example result</a>
        <div class="result__snippet">Useful snippet text.</div>
        """
        cfg = AppConfig(search=SearchConfig(enabled=True, provider="duckduckgo", max_results=3))
        registry = ToolRegistry(cfg)
        register_search_tools(registry, cfg)
        with patch("urllib.request.urlopen", return_value=FakeUrlResponse([html])):
            result = registry.run("web_search", {"query": "hello"})
        self.assertTrue(result["ok"])
        self.assertEqual(result["provider"], "duckduckgo")
        self.assertEqual(result["results"][0]["url"], "https://example.com/page")
        self.assertIn("Useful snippet", result["results"][0]["content"])

    def test_duckduckgo_lite_fallback_parses_results(self) -> None:
        html = b"""
        <a rel="nofollow" href="https://duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Flite">Lite result</a>
        <td class="result-snippet">Lite snippet text.</td>
        """
        cfg = AppConfig(search=SearchConfig(enabled=True, provider="duckduckgo", max_results=3))
        registry = ToolRegistry(cfg)
        register_search_tools(registry, cfg)
        with patch("urllib.request.urlopen", return_value=FakeUrlResponse([html])):
            result = registry.run("web_search", {"query": "hello"})
        self.assertTrue(result["ok"])
        self.assertEqual(result["results"][0]["url"], "https://example.com/lite")

    def test_search_falls_back_to_wikipedia_when_duckduckgo_empty(self) -> None:
        cfg = AppConfig(search=SearchConfig(enabled=True, provider="auto", max_results=3))
        registry = ToolRegistry(cfg)
        register_search_tools(registry, cfg)
        wiki = HttpResponse(
            True,
            200,
            {
                "query": {
                    "pages": {
                        "1": {
                            "title": "Nermana",
                            "extract": "Nermana is a test result.",
                            "fullurl": "https://example.com/wiki/Nermana",
                        }
                    }
                }
            },
        )
        with patch("urllib.request.urlopen", side_effect=OSError("blocked")), patch(
            "nermana.tools.search.get_json",
            side_effect=[
                HttpResponse(True, 200, {"RelatedTopics": []}),
                wiki,
            ],
        ):
            result = registry.run("web_search", {"query": "nermana"})
        self.assertTrue(result["ok"])
        self.assertEqual(result["provider"], "wikipedia")
        self.assertEqual(result["results"][0]["title"], "Nermana")

    def test_search_falls_back_to_hackernews_after_empty_providers(self) -> None:
        cfg = AppConfig(search=SearchConfig(enabled=True, provider="auto", max_results=3))
        registry = ToolRegistry(cfg)
        register_search_tools(registry, cfg)
        hn = HttpResponse(
            True,
            200,
            {
                "hits": [
                    {
                        "title": "Nermana search fallback",
                        "url": "https://example.com/fallback",
                        "points": 42,
                        "num_comments": 3,
                        "author": "kent",
                    }
                ]
            },
        )
        with patch("urllib.request.urlopen", side_effect=OSError("blocked")), patch(
            "nermana.tools.search.get_json",
            side_effect=[
                HttpResponse(True, 200, {"RelatedTopics": []}),
                HttpResponse(True, 200, {"query": {"pages": {}}}),
                hn,
            ],
        ):
            result = registry.run("web_search", {"query": "nermana"})
        self.assertTrue(result["ok"])
        self.assertEqual(result["provider"], "hackernews")
        self.assertEqual(result["results"][0]["title"], "Nermana search fallback")

    def test_file_allowlist(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            allowed = root / "allowed"
            outside = root / "outside.txt"
            allowed.mkdir()
            inside = allowed / "note.txt"
            inside.write_text("hello", encoding="utf-8")
            outside.write_text("no", encoding="utf-8")
            cfg = AppConfig(files=FileConfig(allowed_dirs=[str(allowed)]), memory=MemoryConfig(db_path=str(root / "m.sqlite3")))
            registry = ToolRegistry(cfg)
            memory = MemoryStore(cfg.memory)
            register_file_tools(registry, cfg, memory)
            self.assertTrue(registry.run("read_file", {"path": str(inside)})["ok"])
            self.assertFalse(registry.run("read_file", {"path": str(outside)})["ok"])


class AgentTests(unittest.TestCase):
    def test_agent_direct_tool_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            note = root / "note.txt"
            note.write_text("offline file", encoding="utf-8")
            cfg = AppConfig(
                files=FileConfig(allowed_dirs=[str(root)]),
                memory=MemoryConfig(db_path=str(root / "m.sqlite3")),
            )
            agent = AgentCore(cfg)
            result = agent.chat(f"/read {note}", session_id="test")
            self.assertTrue(result["ok"])
            self.assertIn("offline file", result["reply"])

    def test_agent_confirms_semi_auto_tool_before_running(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = AppConfig(memory=MemoryConfig(db_path=str(Path(tmp) / "m.sqlite3")))
            agent = AgentCore(cfg)
            result = agent.chat("what is the weather today?", session_id="confirm")
            self.assertIn("Reply `yes`", result["reply"])
            self.assertIn("confirm", agent.pending_actions)
            canceled = agent.chat("cancel", session_id="confirm")
            self.assertEqual(canceled["reply"], "Canceled.")

    def test_agent_can_confirm_memory_action(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = AppConfig(memory=MemoryConfig(db_path=str(Path(tmp) / "m.sqlite3")))
            agent = AgentCore(cfg)
            result = agent.chat("remember this: I prefer short replies", session_id="mem")
            self.assertIn("save that to memory", result["reply"].lower())
            saved = agent.chat("yes", session_id="mem")
            self.assertIn("Saved to memory", saved["reply"])

    def test_agent_summarizes_weather_tool_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = AppConfig(memory=MemoryConfig(db_path=str(Path(tmp) / "m.sqlite3")))
            agent = AgentCore(cfg)
            text = agent._tool_result_to_text(
                {
                    "ok": True,
                    "location": "Tagum City",
                    "weather": {
                        "current": {"temperature_2m": 30, "apparent_temperature": 34, "relative_humidity_2m": 72, "wind_speed_10m": 8, "weather_code": 61},
                        "current_units": {"temperature_2m": "C", "wind_speed_10m": "km/h"},
                        "daily": {"time": ["2026-06-25"], "temperature_2m_max": [32], "temperature_2m_min": [25], "precipitation_probability_max": [80]},
                    },
                }
            )
            self.assertIn("Weather for Tagum City", text)
            self.assertIn("rain", text.lower())
            self.assertNotIn("{", text)

    def test_direct_weather_passes_tool_context_to_model(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = AppConfig(memory=MemoryConfig(db_path=str(Path(tmp) / "m.sqlite3")))
            agent = AgentCore(cfg)
            weather = {
                "ok": True,
                "tool": "current_weather",
                "location": "Tagum City",
                "weather": {"current": {"temperature_2m": 30, "weather_code": 1}, "current_units": {"temperature_2m": "C"}},
            }
            with patch.object(agent.tools, "run", return_value=weather), patch.object(agent.models, "chat", return_value={"ok": True, "content": "Tagum City is warm and mostly clear."}):
                result = agent.chat("/weather Tagum City", session_id="weather-model")
            self.assertEqual(result["reply"], "Tagum City is warm and mostly clear.")

    def test_agent_offline_core_has_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = AppConfig(memory=MemoryConfig(db_path=str(Path(tmp) / "m.sqlite3")))
            agent = AgentCore(cfg)
            with patch.object(agent.models, "chat", return_value={"ok": False, "error": "offline"}):
                result = agent.chat("who are you?", session_id="identity")
            self.assertIn("Nermana", result["reply"])
            self.assertIn("local-first", result["reply"])

    def test_agent_initiates_learning_message(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = AppConfig(memory=MemoryConfig(db_path=str(Path(tmp) / "m.sqlite3")))
            agent = AgentCore(cfg)
            with patch.object(agent.models, "server_health", return_value={"ok": False}):
                result = agent.initiative_message("fresh")
            self.assertTrue(result["message"])
            self.assertIn("Teach me", result["message"])


class TelegramTests(unittest.TestCase):
    def test_telegram_poll_accepts_text_and_sends_reply(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = AppConfig(
                telegram=TelegramConfig(enabled=True, token="token", allowed_user_ids=[7], offset_path=str(Path(tmp) / "offset.txt")),
                memory=MemoryConfig(db_path=str(Path(tmp) / "m.sqlite3")),
            )
            agent = AgentCore(cfg)
            update = {"update_id": 5, "message": {"chat": {"id": 123}, "from": {"id": 7}, "text": "/start"}}
            with patch("nermana.telegram_bot.get_json", return_value=HttpResponse(True, 200, {"ok": True, "result": [update]})), patch(
                "nermana.telegram_bot.post_json", return_value=HttpResponse(True, 200, {"ok": True})
            ) as send:
                result = TelegramBot(agent).poll_once()
            self.assertTrue(result["ok"])
            self.assertEqual(result["processed"], 1)
            self.assertTrue(send.called)

    def test_telegram_persists_offset_between_instances(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = AppConfig(
                telegram=TelegramConfig(enabled=True, token="token", allowed_user_ids=[], offset_path=str(Path(tmp) / "offset.txt")),
                memory=MemoryConfig(db_path=str(Path(tmp) / "m.sqlite3")),
            )
            agent = AgentCore(cfg)
            update = {"update_id": 11, "message": {"chat": {"id": 123}, "from": {"id": 7}, "text": "/start@NermanaBot"}}
            with patch("nermana.telegram_bot.get_json", return_value=HttpResponse(True, 200, {"ok": True, "result": [update]})), patch(
                "nermana.telegram_bot.post_json", return_value=HttpResponse(True, 200, {"ok": True})
            ):
                first = TelegramBot(agent).poll_once(timeout=1)
            self.assertEqual(first["offset"], 12)
            self.assertEqual(TelegramBot(agent).offset, 12)

    def test_telegram_status_reports_invalid_token(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = AppConfig(
                telegram=TelegramConfig(enabled=True, token="bad-token", offset_path=str(Path(tmp) / "offset.txt")),
                memory=MemoryConfig(db_path=str(Path(tmp) / "m.sqlite3")),
            )
            agent = AgentCore(cfg)
            with patch("nermana.telegram_bot.get_json", return_value=HttpResponse(False, 404, None, "HTTP Error 404: Not Found")):
                result = TelegramBot(agent).status()
        self.assertFalse(result["ok"])
        self.assertIn("BotFather", result["error"])

    def test_telegram_poll_clears_webhook_conflict_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = AppConfig(
                telegram=TelegramConfig(enabled=True, token="token", offset_path=str(Path(tmp) / "offset.txt")),
                memory=MemoryConfig(db_path=str(Path(tmp) / "m.sqlite3")),
            )
            agent = AgentCore(cfg)
            with patch(
                "nermana.telegram_bot.get_json",
                side_effect=[
                    HttpResponse(False, 409, None, "Conflict: can't use getUpdates method while webhook is active"),
                    HttpResponse(True, 200, {"ok": True, "result": []}),
                ],
            ), patch("nermana.telegram_bot.post_json", return_value=HttpResponse(True, 200, {"ok": True, "description": "Webhook was deleted"})) as post:
                result = TelegramBot(agent).poll_once(timeout=1)
        self.assertTrue(result["ok"])
        self.assertEqual(result["processed"], 0)
        self.assertTrue(post.called)

    def test_telegram_drop_pending_updates_advances_offset(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = AppConfig(
                telegram=TelegramConfig(enabled=True, token="token", offset_path=str(Path(tmp) / "offset.txt")),
                memory=MemoryConfig(db_path=str(Path(tmp) / "m.sqlite3")),
            )
            agent = AgentCore(cfg)
            with patch("nermana.telegram_bot.post_json", return_value=HttpResponse(True, 200, {"ok": True})), patch(
                "nermana.telegram_bot.get_json",
                return_value=HttpResponse(True, 200, {"ok": True, "result": [{"update_id": 21}]}),
            ):
                result = TelegramBot(agent).reset_offset(drop_pending_updates=True)
            self.assertTrue(result["ok"])
            self.assertEqual(result["offset"], 22)
            self.assertEqual(TelegramBot(agent).offset, 22)


class StartupTests(unittest.TestCase):
    def test_startup_reads_server_env_overrides(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = AppConfig(memory=MemoryConfig(db_path=str(Path(tmp) / "m.sqlite3")))
            env = {"NERMANA_HOST": "0.0.0.0", "NERMANA_PORT": "8766"}
            with patch.dict(os.environ, env), patch("nermana.startup.load_config", return_value=cfg):
                manager = StartupManager()
            self.assertEqual(manager.web_host, "0.0.0.0")
            self.assertEqual(manager.web_port, 8766)
            self.assertEqual(manager.agent.config.server.host, "127.0.0.1")
            self.assertEqual(manager.agent.config.server.port, 8765)

    def test_simple_server_reports_occupied_port_without_traceback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = AppConfig(memory=MemoryConfig(db_path=str(Path(tmp) / "m.sqlite3")))
            server = SimpleNermanaServer(AgentCore(cfg))
            error = OSError(errno.EADDRINUSE, "Address already in use")
            with patch("nermana.simple_server.ThreadingHTTPServer", side_effect=error):
                with self.assertRaises(SystemExit) as raised:
                    server.serve("127.0.0.1", 8765)
            self.assertEqual(raised.exception.code, 98)


class DashboardTests(unittest.TestCase):
    def test_dashboard_snapshot_reports_workers_and_stats(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = AppConfig(memory=MemoryConfig(db_path=str(Path(tmp) / "m.sqlite3")))
            agent = AgentCore(cfg)
            agent.memory.remember("Dashboard memory", tags="test", source="unit")
            server = SimpleNermanaServer(agent)
            caps = [
                Capability("internet", True, "network probe"),
                Capability("local_model", False, "not running"),
                Capability("llama_server_binary", True, "llama-server"),
                Capability("termux_api", False, "missing"),
                Capability("shizuku_rish", False, "missing"),
                Capability("image_provider", False, "not configured"),
                Capability("vision_provider", False, "not configured"),
                Capability("telegram", False, "missing token"),
            ]
            with patch("nermana.simple_server.collect_capabilities", return_value=caps):
                snapshot = server.dashboard_snapshot()
            self.assertTrue(snapshot["ok"])
            self.assertGreaterEqual(snapshot["stats"]["workers_total"], 1)
            self.assertEqual(snapshot["stats"]["memories"], 1)
            self.assertTrue(any(worker["name"] == "Internet" for worker in snapshot["workers"]))


if __name__ == "__main__":
    unittest.main()
