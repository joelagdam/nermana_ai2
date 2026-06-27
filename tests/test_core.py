from __future__ import annotations

import errno
import os
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from nermana.agent import AgentCore
from nermana.capabilities import Capability
from nermana.config import AppConfig, FileConfig, MemoryConfig, ModelConfig, SafetyConfig, SearchConfig, SelfLearningConfig, TelegramConfig, merge_config, reset_config_defaults, save_config
from nermana.core_knowledge import knowledge_status, search_core_knowledge
from nermana.http_client import HttpResponse
from nermana.model_downloads import delete_partial_download, download_model, list_partial_downloads, list_presets
from nermana.memory import MemoryStore
from nermana.models import ModelManager
from nermana.safety import DecisionGate
from nermana.simple_server import SimpleNermanaServer, _decode_json_body, _query_int
from nermana.startup import StartupManager
from nermana.telegram_bot import TelegramBot
from nermana.tooling import Tool, ToolRegistry
from nermana.tools.files import register_file_tools
from nermana.tools.phone import register_phone_tools
from nermana.tools.search import register_search_tools
from nermana.updater import update_status, update_system


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


class FakeCompleted:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class ConfigTests(unittest.TestCase):
    def test_merge_config_updates_nested_values(self) -> None:
        cfg = AppConfig()
        updated = merge_config(cfg, {"model": {"temperature": 0.25}, "search": {"enabled": False}})
        self.assertEqual(updated.model.temperature, 0.25)
        self.assertFalse(updated.search.enabled)
        self.assertIsInstance(updated.model, ModelConfig)

    def test_reset_config_defaults_preserves_model_and_secrets(self) -> None:
        cfg = AppConfig(
            model=ModelConfig(active_model="phone.gguf", context_size=512),
            telegram=TelegramConfig(enabled=True, token="token", allowed_user_ids=[7]),
        )
        reset = reset_config_defaults(cfg)
        self.assertEqual(reset.model.context_size, 4096)
        self.assertEqual(reset.model.active_model, "phone.gguf")
        self.assertEqual(reset.telegram.token, "token")
        self.assertTrue(reset.telegram.enabled)

    def test_reset_config_defaults_can_clear_model_and_secrets(self) -> None:
        cfg = AppConfig(
            model=ModelConfig(active_model="phone.gguf", fallback_model="old.gguf", models_dir="custom-models"),
            telegram=TelegramConfig(enabled=True, token="token", allowed_user_ids=[7]),
        )
        reset = reset_config_defaults(cfg, preserve_secrets=False, preserve_model_selection=False)
        self.assertEqual(reset.model.active_model, "")
        self.assertEqual(reset.model.fallback_model, "")
        self.assertEqual(reset.model.models_dir, "models")
        self.assertEqual(reset.telegram.token, "")
        self.assertFalse(reset.telegram.enabled)


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

    def test_switch_invalidates_runtime_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            model_dir = Path(tmp) / "models"
            model_dir.mkdir()
            (model_dir / "tiny.gguf").write_bytes(b"model")
            cfg = AppConfig(model=ModelConfig(models_dir=str(model_dir)))
            manager = ModelManager(cfg, persist=False)
            manager._runtime_cache = {"ok": True}
            manager._runtime_cache_at = 1.0
            self.assertTrue(manager.switch("tiny.gguf")["ok"])
            self.assertIsNone(manager._runtime_cache)

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

    def test_model_download_resumes_partial_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            model_dir = Path(tmp) / "models"
            model_dir.mkdir()
            partial = model_dir / "resume.gguf.part"
            partial.write_bytes(b"ab")
            cfg = AppConfig(model=ModelConfig(models_dir=str(model_dir)))
            response = FakeUrlResponse([b"cd", b""], {"Content-Range": "bytes 2-3/4"})
            response.status = 206
            captured = {}

            def fake_urlopen(request, timeout=30):
                captured["range"] = request.get_header("Range")
                return response

            with patch("urllib.request.urlopen", side_effect=fake_urlopen):
                result = download_model(cfg, "https://example.com/resume.gguf")
            self.assertTrue(result["ok"])
            self.assertEqual(captured["range"], "bytes=2-")
            self.assertEqual((model_dir / "resume.gguf").read_bytes(), b"abcd")

    def test_model_download_cancel_keeps_partial(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            model_dir = Path(tmp) / "models"
            cfg = AppConfig(model=ModelConfig(models_dir=str(model_dir)))
            response = FakeUrlResponse([b"ab", b"cd", b""], {"Content-Length": "4"})
            calls = {"count": 0}

            def cancel_after_first_check() -> bool:
                calls["count"] += 1
                return calls["count"] > 1

            with patch("urllib.request.urlopen", return_value=response):
                result = download_model(cfg, "https://example.com/cancel.gguf", cancelled=cancel_after_first_check)
            self.assertFalse(result["ok"])
            self.assertTrue(result["cancelled"])
            self.assertTrue((model_dir / "cancel.gguf.part").exists())
            self.assertEqual(list_partial_downloads(cfg)[0]["filename"], "cancel.gguf")

    def test_delete_partial_download(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            model_dir = Path(tmp) / "models"
            model_dir.mkdir()
            partial = model_dir / "old.gguf.part"
            partial.write_bytes(b"partial")
            cfg = AppConfig(model=ModelConfig(models_dir=str(model_dir)))
            result = delete_partial_download(cfg, "old.gguf")
            self.assertTrue(result["ok"])
            self.assertFalse(partial.exists())

    def test_llama_command_uses_fast_phone_settings(self) -> None:
        cfg = AppConfig(model=ModelConfig(threads=0, batch_size=256, ubatch_size=64, parallel_slots=1, mlock=True, no_mmap=True))
        manager = ModelManager(cfg, persist=False)
        command = manager._server_command("llama-server", Path("models/test.gguf"), 8080, fast=True)
        self.assertIn("--mlock", command)
        self.assertIn("--no-mmap", command)
        self.assertIn("-b", command)
        self.assertIn("256", command)

    def test_chat_retries_server_advertised_model_after_bad_request(self) -> None:
        cfg = AppConfig(model=ModelConfig(active_model="wrong.gguf"))
        manager = ModelManager(cfg, persist=False)
        models = HttpResponse(True, 200, {"data": [{"id": "server-model"}]})
        bad = HttpResponse(False, 400, None, "HTTP Error 400: model not found")
        good = HttpResponse(True, 200, {"choices": [{"message": {"content": "OK"}}]})
        with patch("nermana.models.get_json", return_value=models), patch("nermana.models.post_json", side_effect=[bad, good]):
            result = manager.chat([{"role": "user", "content": "hi"}])
        self.assertTrue(result["ok"])
        self.assertEqual(result["model"], "server-model")

    def test_runtime_status_requires_chat_completions(self) -> None:
        cfg = AppConfig(model=ModelConfig(active_model="active.gguf"))
        manager = ModelManager(cfg, persist=False)
        models = HttpResponse(True, 200, {"data": [{"id": "active.gguf"}]})
        bad = HttpResponse(False, 400, None, "HTTP Error 400: Bad Request")
        with patch("nermana.models.get_json", return_value=models), patch("nermana.models.post_json", return_value=bad):
            result = manager.runtime_status()
        self.assertFalse(result["ok"])
        self.assertTrue(result["endpoint_ok"])
        self.assertFalse(result["ready"])
        self.assertIn("chat failed", result["state"])

    def test_runtime_status_reports_context_mismatch(self) -> None:
        cfg = AppConfig(model=ModelConfig(active_model="active.gguf", context_size=4096))
        manager = ModelManager(cfg, persist=False)
        models = HttpResponse(True, 200, {"data": [{"id": "active.gguf"}]})
        error = "HTTP Error 400: Bad Request: request (1092 tokens) exceeds the available context size (512 tokens), try increasing it"
        bad = HttpResponse(False, 400, None, error)
        with patch("nermana.models.get_json", return_value=models), patch("nermana.models.post_json", return_value=bad):
            result = manager.runtime_status(force=True)
        self.assertFalse(result["ok"])
        self.assertTrue(result["context_mismatch"])
        self.assertEqual(result["server_context_size"], 512)
        self.assertEqual(result["configured_context_size"], 4096)
        self.assertIn("Restart llama.cpp", result["context_warning"])

    def test_runtime_status_reports_advertised_context_mismatch(self) -> None:
        cfg = AppConfig(model=ModelConfig(active_model="active.gguf", context_size=4096))
        manager = ModelManager(cfg, persist=False)
        models = HttpResponse(True, 200, {"data": [{"id": "active.gguf", "meta": {"n_ctx": 1024}}]})
        good = HttpResponse(True, 200, {"choices": [{"message": {"content": "OK"}}]})
        with patch("nermana.models.get_json", return_value=models), patch("nermana.models.post_json", return_value=good):
            result = manager.runtime_status(force=True)
        self.assertTrue(result["ok"])
        self.assertTrue(result["context_mismatch"])
        self.assertEqual(result["server_context_size"], 1024)
        self.assertEqual(result["configured_context_size"], 4096)
        self.assertIn("Restart llama.cpp", result["context_warning"])

    def test_server_log_tail_reads_recent_llama_lines(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = AppConfig()
            manager = ModelManager(cfg, persist=False)
            with patch("nermana.models.DATA_DIR", Path(tmp)):
                log_path = manager.server_log_path()
                log_path.parent.mkdir(parents=True)
                log_path.write_text("one\ntwo\nthree\n", encoding="utf-8")
                result = manager.server_log_tail(2)
            self.assertTrue(result["ok"])
            self.assertEqual(result["lines"], ["two", "three"])


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

    def test_memory_search_handles_hyphenated_multi_token_query(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = MemoryStore(MemoryConfig(db_path=str(Path(tmp) / "memory.sqlite3")))
            expected = store.remember(
                "Anchor NB-042: user preference for weather is short summaries.",
                tags="benchmark,target,weather,NB-042",
                source="test",
            )
            store.remember("Distractor NB-999: unrelated storage note.", tags="benchmark,distractor,storage", source="test")
            hits = store.search("weather preference NB-042", limit=3)
            self.assertTrue(hits)
            self.assertEqual(hits[0].id, expected)

    def test_memory_search_uses_structured_fields_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = MemoryStore(MemoryConfig(db_path=str(Path(tmp) / "memory.sqlite3")))
            expected = store.remember(
                "The owner wants concise answers.",
                tags="profile",
                source="test",
                topics=["telegram_status"],
            )
            hits = store.search("telegram statuses", limit=3)
            self.assertTrue(any(hit.id == expected for hit in hits))

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

    def test_termux_command_runs_allowlisted_direct_command(self) -> None:
        cfg = AppConfig()
        registry = ToolRegistry(cfg)
        register_phone_tools(registry, cfg)
        with patch("nermana.tools.phone.shutil.which", return_value="/data/data/com.termux/files/usr/bin/date"), patch(
            "nermana.tools.phone.subprocess.run", return_value=FakeCompleted(stdout="Sat Jun 27")
        ) as run:
            result = registry.run("termux_command", {"command": "date"})
        self.assertTrue(result["ok"])
        self.assertEqual(result["stdout"], "Sat Jun 27")
        run.assert_called_once()

    def test_termux_command_rejects_shell_metacharacters(self) -> None:
        cfg = AppConfig()
        registry = ToolRegistry(cfg)
        register_phone_tools(registry, cfg)
        with patch("nermana.tools.phone.shutil.which", return_value="/bin/date"):
            result = registry.run("termux_command", {"command": "date; rm -rf data"})
        self.assertFalse(result["ok"])
        self.assertIn("metacharacters", result["error"])

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

    def test_file_reader_redacts_json_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_file = root / "config.json"
            config_file.write_text('{"telegram": {"token": "secret-token"}, "model": {"active_model": "ok.gguf"}}', encoding="utf-8")
            cfg = AppConfig(files=FileConfig(allowed_dirs=[str(root)]), memory=MemoryConfig(db_path=str(root / "m.sqlite3")))
            registry = ToolRegistry(cfg)
            memory = MemoryStore(cfg.memory)
            register_file_tools(registry, cfg, memory)
            result = registry.run("read_file", {"path": str(config_file)})
            self.assertTrue(result["ok"])
            self.assertIn('"token": "***"', result["content"])
            self.assertNotIn("secret-token", result["content"])


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

    def test_agent_prefers_successful_tool_result_over_model_denial(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            note = root / "note.txt"
            note.write_text("offline file", encoding="utf-8")
            cfg = AppConfig(
                files=FileConfig(allowed_dirs=[str(root)]),
                memory=MemoryConfig(db_path=str(root / "m.sqlite3")),
            )
            agent = AgentCore(cfg)
            with patch.object(agent.models, "chat", return_value={"ok": True, "content": "I cannot access files on this device."}):
                result = agent.chat(f"/read {note}", session_id="test")
            self.assertTrue(result["ok"])
            self.assertIn("offline file", result["reply"])

    def test_agent_prefers_successful_search_over_model_denial(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = AppConfig(memory=MemoryConfig(db_path=str(Path(tmp) / "m.sqlite3")))
            agent = AgentCore(cfg)
            search_result = {
                "ok": True,
                "tool": "web_search",
                "provider": "duckduckgo",
                "query": "Nermana AI Termux",
                "results": [{"title": "Nermana", "url": "https://example.test/nermana", "content": "Offline-first Termux phone AI."}],
            }
            denial = "I'm not able to perform the search operation. Let me know if there's anything else I can assist you with!"
            with patch.object(agent.tools, "run", return_value=search_result), patch.object(agent.models, "chat", return_value={"ok": True, "content": denial}):
                result = agent.chat("/search Nermana AI Termux", session_id="search-denial")
            self.assertTrue(result["ok"])
            self.assertIn("I found 1 duckduckgo result", result["reply"])
            self.assertNotIn("not able to perform", result["reply"].lower())

    def test_agent_filters_diagnostic_fallback_from_prompt_history(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = AppConfig(memory=MemoryConfig(db_path=str(Path(tmp) / "m.sqlite3")), model=ModelConfig(context_size=4096))
            agent = AgentCore(cfg)
            agent.memory.add_message("web", "user", "hello")
            agent.memory.add_message(
                "web",
                "assistant",
                "I am here. My core is awake even when the larger voice engine stumbles. I can still remember, check tools, and help bring the GGUF model back online.",
            )
            captured = []

            def fake_chat(messages, max_tokens=512):
                captured.append(messages)
                return {"ok": True, "content": "Fresh answer."}

            with patch.object(agent.models, "chat", side_effect=fake_chat):
                result = agent.chat("hello again", session_id="web")
            prompt_text = str(captured[0])
            self.assertEqual(result["reply"], "Fresh answer.")
            self.assertNotIn("larger voice engine stumbles", prompt_text)
            self.assertNotIn("GGUF model back online", prompt_text)

    def test_agent_filters_capability_report_from_prompt_history(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = AppConfig(memory=MemoryConfig(db_path=str(Path(tmp) / "m.sqlite3")), model=ModelConfig(context_size=4096))
            agent = AgentCore(cfg)
            agent.memory.add_message("web", "user", "are you conscious of tools?")
            agent.memory.add_message(
                "web",
                "assistant",
                "Operational awareness: I do not have human consciousness; I keep a live capability self-model.\nCommands I recognize: /tools.",
            )
            captured = []

            def fake_chat(messages, max_tokens=512):
                captured.append(messages)
                return {"ok": True, "content": "Basketball is a court sport."}

            with patch.object(agent.models, "chat", side_effect=fake_chat):
                result = agent.chat("New topic: what is basketball?", session_id="web")
            prompt_text = str(captured[0])
            self.assertEqual(result["reply"], "Basketball is a court sport.")
            self.assertNotIn("Operational awareness", prompt_text)
            self.assertNotIn("Commands I recognize", prompt_text)

    def test_agent_explicit_new_topic_drops_stale_history(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = AppConfig(memory=MemoryConfig(db_path=str(Path(tmp) / "m.sqlite3")), model=ModelConfig(context_size=4096))
            agent = AgentCore(cfg)
            agent.memory.add_message("topic", "user", "Tell me about volcanoes.")
            agent.memory.add_message("topic", "assistant", "Volcanoes involve magma and eruptions.")
            captured = []

            def fake_chat(messages, max_tokens=512):
                captured.append(messages)
                return {"ok": True, "content": "Photosynthesis feeds plants."}

            with patch.object(agent.models, "chat", side_effect=fake_chat):
                agent.chat("New topic: explain photosynthesis.", session_id="topic")
            prompt_text = str(captured[0])
            self.assertNotIn("Volcanoes involve magma", prompt_text)
            self.assertIn("explain photosynthesis", prompt_text)

    def test_agent_unrelated_topic_shift_drops_stale_history_but_followup_keeps_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = AppConfig(memory=MemoryConfig(db_path=str(Path(tmp) / "m.sqlite3")), model=ModelConfig(context_size=4096))
            agent = AgentCore(cfg)
            agent.memory.add_message("shift", "user", "Tell me about volcano magma eruptions.")
            agent.memory.add_message("shift", "assistant", "Volcanoes erupt when pressure moves magma upward.")
            captured = []

            def fake_chat(messages, max_tokens=512):
                captured.append(messages)
                return {"ok": True, "content": "ok"}

            with patch.object(agent.models, "chat", side_effect=fake_chat):
                agent.chat("Explain photosynthesis in plants.", session_id="shift")
            self.assertNotIn("Volcanoes erupt", str(captured[-1]))

            agent.memory.add_message("follow", "user", "Tell me about volcano magma eruptions.")
            agent.memory.add_message("follow", "assistant", "Volcanoes erupt when pressure moves magma upward.")
            with patch.object(agent.models, "chat", side_effect=fake_chat):
                agent.chat("Why do they erupt?", session_id="follow")
            self.assertIn("Volcanoes erupt", str(captured[-1]))

    def test_agent_retries_fresh_when_model_repeats_old_answer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = AppConfig(memory=MemoryConfig(db_path=str(Path(tmp) / "m.sqlite3")), model=ModelConfig(context_size=4096))
            agent = AgentCore(cfg)
            old = "Volcanoes are openings in Earth's crust where magma, ash, and gas can escape during eruptions."
            agent.memory.add_message("repeat", "user", "What is a volcano?")
            agent.memory.add_message("repeat", "assistant", old)
            responses = [
                {"ok": True, "content": old},
                {"ok": True, "content": "Photosynthesis is how plants turn light, water, and carbon dioxide into food."},
            ]

            with patch.object(agent.models, "chat", side_effect=responses) as chat:
                result = agent.chat("Explain photosynthesis in one sentence.", session_id="repeat")
            self.assertEqual(result["reply"], "Photosynthesis is how plants turn light, water, and carbon dioxide into food.")
            self.assertTrue(result["repeated_answer_retry"])
            self.assertEqual(chat.call_count, 2)

    def test_agent_retries_common_knowledge_over_refusal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = AppConfig(memory=MemoryConfig(db_path=str(Path(tmp) / "m.sqlite3")), model=ModelConfig(context_size=4096))
            agent = AgentCore(cfg)
            responses = [
                {"ok": True, "content": "I don't have access to sleep-related tips. Let me know if there's anything else I can assist you with!"},
                {"ok": True, "content": "Keep a consistent sleep schedule, including weekends."},
            ]

            with patch.object(agent.models, "chat", side_effect=responses) as chat:
                result = agent.chat("Give one useful tip for sleep.", session_id="over-refusal")
            self.assertEqual(result["reply"], "Keep a consistent sleep schedule, including weekends.")
            self.assertEqual(chat.call_count, 2)

    def test_agent_uses_smaller_token_budget_for_short_prompts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = AppConfig(memory=MemoryConfig(db_path=str(Path(tmp) / "m.sqlite3")), model=ModelConfig(context_size=4096))
            agent = AgentCore(cfg)
            short_messages = [{"role": "user", "content": "In one short sentence, what is a volcano?"}]
            long_messages = [{"role": "user", "content": "Give a detailed step by step plan."}]
            self.assertEqual(agent._max_response_tokens(messages=short_messages), 128)
            self.assertEqual(agent._max_response_tokens(messages=long_messages), 512)

    def test_agent_polishes_generic_assistant_closers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = AppConfig(memory=MemoryConfig(db_path=str(Path(tmp) / "m.sqlite3")), model=ModelConfig(context_size=4096))
            agent = AgentCore(cfg)
            answer = "A simple egg breakfast is scrambled eggs with toast. Let me know if you'd like more ideas!"
            self.assertEqual(agent._polish_model_answer(answer), "A simple egg breakfast is scrambled eggs with toast.")

    def test_agent_reports_capability_self_model_without_llm(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = AppConfig(memory=MemoryConfig(db_path=str(Path(tmp) / "m.sqlite3")))
            agent = AgentCore(cfg)
            health = {"ok": True, "state": "chat ready", "server_context_size": 4096, "configured_context_size": 4096}
            with patch.object(agent.models, "runtime_status", return_value=health), patch.object(agent.models, "chat", side_effect=AssertionError("LLM should not run")):
                result = agent.chat("/tools", session_id="capabilities")
            self.assertTrue(result["core_answer"])
            self.assertIn("Operational awareness", result["reply"])
            self.assertIn("human consciousness", result["reply"])
            self.assertIn("Active tools", result["reply"])
            self.assertIn("current_weather", result["reply"])
            self.assertIn("Decision policy", result["reply"])

    def test_core_knowledge_search_finds_repair_and_performance(self) -> None:
        repair = search_core_knowledge("self repair doctor loading model", limit=2)
        performance = search_core_knowledge("reply faster performance tokens", limit=2)
        self.assertTrue(any(card.key == "self_repair_doctor" for card in repair))
        self.assertTrue(any(card.key == "performance_fast_reply" for card in performance))
        self.assertGreaterEqual(knowledge_status()["cards"], 6)

    def test_agent_reports_core_knowledge_without_llm(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = AppConfig(memory=MemoryConfig(db_path=str(Path(tmp) / "m.sqlite3")))
            agent = AgentCore(cfg)
            health = {"ok": True, "state": "chat ready"}
            with patch.object(agent.models, "runtime_status", return_value=health), patch.object(agent.models, "chat", side_effect=AssertionError("LLM should not run")):
                result = agent.chat("how do you repair yourself and use commands?", session_id="knowledge")
            self.assertTrue(result["core_answer"])
            self.assertIn("Self Repair", result["reply"])
            self.assertIn("/search", result["reply"])
            self.assertIn("Knowledge cards", result["reply"])

    def test_agent_reports_loading_model_repair_from_core_knowledge_without_llm(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = AppConfig(memory=MemoryConfig(db_path=str(Path(tmp) / "m.sqlite3")))
            agent = AgentCore(cfg)
            health = {"ok": False, "state": "loading"}
            with patch.object(agent.models, "runtime_status", return_value=health), patch.object(agent.models, "chat", side_effect=AssertionError("LLM should not run")):
                result = agent.chat("what should I do about loading model 503?", session_id="knowledge-repair")
            self.assertTrue(result["core_answer"])
            self.assertIn("Repair Local Model", result["reply"])
            self.assertIn("Doctor", result["reply"])

    def test_agent_reports_phone_llm_performance_from_core_knowledge_without_llm(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = AppConfig(memory=MemoryConfig(db_path=str(Path(tmp) / "m.sqlite3")))
            agent = AgentCore(cfg)
            health = {"ok": True, "state": "chat ready"}
            with patch.object(agent.models, "runtime_status", return_value=health), patch.object(agent.models, "chat", side_effect=AssertionError("LLM should not run")):
                result = agent.chat("give one practical tip to make phone LLM replies faster", session_id="knowledge-performance")
            self.assertTrue(result["core_answer"])
            self.assertIn("Performance And Fast Reply", result["reply"])
            self.assertIn("under a second", result["reply"])

    def test_agent_injects_relevant_core_knowledge_into_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = AppConfig(memory=MemoryConfig(db_path=str(Path(tmp) / "m.sqlite3")), model=ModelConfig(context_size=4096))
            agent = AgentCore(cfg)
            captured = []

            def fake_chat(messages, max_tokens=512):
                captured.append(messages)
                return {"ok": True, "content": "Use Doctor first."}

            with patch.object(agent.models, "chat", side_effect=fake_chat):
                result = agent.chat("Give runtime diagnostic guidance for doctor model readiness.", session_id="knowledge-prompt")
            prompt_text = str(captured[0])
            self.assertEqual(result["reply"], "Use Doctor first.")
            self.assertIn("Built-in Nermana knowledge", prompt_text)
            self.assertIn("Doctor", prompt_text)

    def test_agent_status_reports_core_knowledge_pack(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = AppConfig(memory=MemoryConfig(db_path=str(Path(tmp) / "m.sqlite3")))
            agent = AgentCore(cfg)
            with patch.object(agent.models, "runtime_status", return_value={"ok": True}):
                status = agent.status()
            self.assertIn("core_knowledge", status)
            self.assertGreaterEqual(status["core_knowledge"]["cards"], 6)

    def test_agent_reports_unavailable_focused_capabilities(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = AppConfig(memory=MemoryConfig(db_path=str(Path(tmp) / "m.sqlite3")))
            agent = AgentCore(cfg)
            health = {"ok": True, "state": "chat ready"}
            with patch.object(agent.models, "runtime_status", return_value=health), patch.object(agent.models, "chat", side_effect=AssertionError("LLM should not run")):
                result = agent.chat("can you generate image and use vision?", session_id="capability-focus")
            self.assertIn("generate_image", result["reply"])
            self.assertIn("vision_analyze", result["reply"])
            self.assertIn("Unavailable now", result["reply"])
            self.assertNotIn("current_weather", result["reply"])

    def test_agent_reports_phone_tool_awareness_without_llm(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = AppConfig(memory=MemoryConfig(db_path=str(Path(tmp) / "m.sqlite3")))
            agent = AgentCore(cfg)
            health = {"ok": True, "state": "chat ready"}
            with patch.object(agent.models, "runtime_status", return_value=health), patch.object(agent.models, "chat", side_effect=AssertionError("LLM should not run")):
                result = agent.chat("are you aware of your phone and shizuku tools?", session_id="phone-awareness")
            self.assertTrue(result["core_answer"])
            self.assertIn("phone_status", result["reply"])
            self.assertIn("shizuku", result["reply"].lower())
            self.assertIn("Decision policy", result["reply"])

    def test_agent_reports_consciousness_as_operational_self_model_without_llm(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = AppConfig(memory=MemoryConfig(db_path=str(Path(tmp) / "m.sqlite3")))
            agent = AgentCore(cfg)
            health = {"ok": True, "state": "chat ready"}
            with patch.object(agent.models, "runtime_status", return_value=health), patch.object(agent.models, "chat", side_effect=AssertionError("LLM should not run")):
                result = agent.chat("are you conscious?", session_id="consciousness")
            self.assertTrue(result["core_answer"])
            self.assertIn("do not have human consciousness", result["reply"])
            self.assertIn("live capability self-model", result["reply"])
            self.assertIn("Decision policy", result["reply"])

    def test_agent_capability_phrasings_use_core_accuracy_path(self) -> None:
        prompts = [
            "what can you do?",
            "which capabilities are active?",
            "what tools are unavailable?",
            "provider status",
            "can you control my phone?",
            "do you have access to shizuku?",
            "can you use vision tools?",
            "do you know yourself?",
        ]
        with tempfile.TemporaryDirectory() as tmp:
            cfg = AppConfig(memory=MemoryConfig(db_path=str(Path(tmp) / "m.sqlite3")))
            agent = AgentCore(cfg)
            health = {"ok": True, "state": "chat ready"}
            with patch.object(agent.models, "runtime_status", return_value=health), patch.object(agent.models, "chat", side_effect=AssertionError("LLM should not run")):
                for prompt in prompts:
                    with self.subTest(prompt=prompt):
                        result = agent.chat(prompt, session_id="capability-phrasing")
                        self.assertTrue(result["core_answer"])
                        self.assertIn("Operational awareness", result["reply"])
                        self.assertIn("Decision policy", result["reply"])

    def test_capability_context_includes_decision_policy_and_unavailable_tools(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = AppConfig(memory=MemoryConfig(db_path=str(Path(tmp) / "m.sqlite3")))
            agent = AgentCore(cfg)
            context = agent._capability_context()
            self.assertIn("decision_policy", context)
            self.assertIn("active_tool_count", context)
            self.assertIn("Unavailable tools", context)
            self.assertIn("generate_image", context)

    def test_agent_runs_safe_semi_auto_tool_without_confirmation(self) -> None:
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
                result = agent.chat("what is the weather today?", session_id="auto-tool")
            self.assertNotIn("Reply `yes`", result["reply"])
            self.assertEqual(result["tool_results"][0]["tool"], "current_weather")
            self.assertEqual(result["reply"], "Tagum City is warm and mostly clear.")

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

    def test_agent_shortens_loading_model_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = AppConfig(memory=MemoryConfig(db_path=str(Path(tmp) / "m.sqlite3")))
            agent = AgentCore(cfg)
            message = agent._friendly_model_error("HTTP Error 503: Service Unavailable: Loading model")
            self.assertIn("still loading", message)
            self.assertIn("Doctor", message)

    def test_agent_initiates_learning_message(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = AppConfig(memory=MemoryConfig(db_path=str(Path(tmp) / "m.sqlite3")))
            agent = AgentCore(cfg)
            with patch.object(agent.models, "runtime_status", return_value={"ok": False}):
                result = agent.initiative_message("fresh")
            self.assertTrue(result["message"])
            self.assertIn("Teach me", result["message"])

    def test_agent_splits_long_replies_into_batches(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = AppConfig(memory=MemoryConfig(db_path=str(Path(tmp) / "m.sqlite3")))
            agent = AgentCore(cfg)
            answer = "A" * 1700 + ". " + "B" * 1700
            with patch.object(agent.models, "chat", return_value={"ok": True, "content": answer}):
                result = agent.chat("tell me a long thing", session_id="long")
            self.assertGreater(len(result["reply_batches"]), 1)
            self.assertEqual(result["reply"], answer)

    def test_agent_retries_with_compact_prompt_on_context_overflow(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = AppConfig(memory=MemoryConfig(db_path=str(Path(tmp) / "m.sqlite3")), model=ModelConfig(context_size=4096))
            agent = AgentCore(cfg)
            bad = {
                "ok": False,
                "error": "HTTP Error 400: Bad Request: request (1092 tokens) exceeds the available context size (512 tokens), try increasing it",
            }
            good = {"ok": True, "content": "Compact path works."}
            captured = []

            def fake_chat(messages, max_tokens=512):
                captured.append((messages, max_tokens))
                return bad if len(captured) == 1 else good

            with patch.object(agent.models, "chat", side_effect=fake_chat):
                result = agent.chat("Arise", session_id="compact")
            self.assertEqual(result["reply"], "Compact path works.")
            self.assertTrue(result["compacted_prompt"])
            self.assertEqual(captured[1][1], 96)
            self.assertLess(len(str(captured[1][0])), len(str(captured[0][0])))


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

    def test_telegram_sends_reply_batches(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = AppConfig(
                telegram=TelegramConfig(enabled=True, token="token", allowed_user_ids=[7], offset_path=str(Path(tmp) / "offset.txt")),
                memory=MemoryConfig(db_path=str(Path(tmp) / "m.sqlite3")),
            )
            agent = AgentCore(cfg)
            update = {"update_id": 5, "message": {"chat": {"id": 123}, "from": {"id": 7}, "text": "tell me"}}
            chat_result = {"ok": True, "reply": "first\n\nsecond", "reply_batches": ["first", "second"], "model_ok": True}
            with patch.object(agent, "chat", return_value=chat_result), patch.object(TelegramBot, "_typing_loop", return_value=None), patch(
                "nermana.telegram_bot.get_json", return_value=HttpResponse(True, 200, {"ok": True, "result": [update]})
            ), patch("nermana.telegram_bot.post_json", return_value=HttpResponse(True, 200, {"ok": True})) as post:
                result = TelegramBot(agent).poll_once(timeout=1)
            sent_texts = [call.args[1]["text"] for call in post.call_args_list if call.args[0].endswith("/sendMessage")]
            self.assertTrue(result["ok"])
            self.assertEqual(sent_texts, ["first", "second"])

    def test_telegram_shortens_model_context_errors(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = AppConfig(
                telegram=TelegramConfig(enabled=True, token="token", allowed_user_ids=[7], offset_path=str(Path(tmp) / "offset.txt")),
                memory=MemoryConfig(db_path=str(Path(tmp) / "m.sqlite3")),
            )
            agent = AgentCore(cfg)
            update = {"update_id": 5, "message": {"chat": {"id": 123}, "from": {"id": 7}, "text": "hello"}}
            error = "HTTP Error 400: Bad Request: request exceeds the available context size (512 tokens)"
            chat_result = {"ok": True, "reply": error, "reply_batches": [error], "model_ok": False, "model_error": error}
            with patch.object(agent, "chat", return_value=chat_result), patch.object(TelegramBot, "_typing_loop", return_value=None), patch(
                "nermana.telegram_bot.get_json", return_value=HttpResponse(True, 200, {"ok": True, "result": [update]})
            ), patch("nermana.telegram_bot.post_json", return_value=HttpResponse(True, 200, {"ok": True})) as post:
                result = TelegramBot(agent).poll_once(timeout=1)
            sent_texts = [call.args[1]["text"] for call in post.call_args_list if call.args[0].endswith("/sendMessage")]
            self.assertTrue(result["ok"])
            self.assertEqual(len(sent_texts), 1)
            self.assertIn("context window", sent_texts[0])
            self.assertNotIn("HTTP Error 400", sent_texts[0])

    def test_telegram_shortens_loading_model_errors(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = AppConfig(
                telegram=TelegramConfig(enabled=True, token="token", offset_path=str(Path(tmp) / "offset.txt")),
                memory=MemoryConfig(db_path=str(Path(tmp) / "m.sqlite3")),
            )
            reply = TelegramBot(AgentCore(cfg))._short_chat_error("HTTP Error 503: Service Unavailable: Loading model")
            self.assertIn("still loading", reply)
            self.assertIn("Doctor", reply)

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

    def test_telegram_network_error_is_marked_offline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = AppConfig(
                telegram=TelegramConfig(enabled=True, token="token", offset_path=str(Path(tmp) / "offset.txt")),
                memory=MemoryConfig(db_path=str(Path(tmp) / "m.sqlite3")),
            )
            agent = AgentCore(cfg)
            with patch("nermana.telegram_bot.get_json", return_value=HttpResponse(False, 0, None, "timed out")):
                result = TelegramBot(agent).poll_once(timeout=1)
        self.assertFalse(result["ok"])
        self.assertTrue(result["offline"])

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
    def test_decode_json_body_rejects_invalid_payloads(self) -> None:
        body, error = _decode_json_body("{bad")
        self.assertEqual(body, {})
        self.assertIn("invalid JSON", error)
        body, error = _decode_json_body("[]")
        self.assertEqual(body, {})
        self.assertIn("object", error)
        body, error = _decode_json_body('{"ok": true}')
        self.assertEqual(body, {"ok": True})
        self.assertEqual(error, "")

    def test_query_int_uses_default_and_clamps(self) -> None:
        self.assertEqual(_query_int({"limit": ["bad"]}, "limit", 10, 1, 20), 10)
        self.assertEqual(_query_int({"limit": ["999"]}, "limit", 10, 1, 20), 20)
        self.assertEqual(_query_int({"limit": ["0"]}, "limit", 10, 1, 20), 1)

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

    def test_server_tracks_telegram_worker_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = AppConfig(
                telegram=TelegramConfig(enabled=True, token="token", offset_path=str(Path(tmp) / "offset.txt"), poll_interval_seconds=1),
                memory=MemoryConfig(db_path=str(Path(tmp) / "m.sqlite3")),
            )
            server = SimpleNermanaServer(AgentCore(cfg))

            def poll_once(bot, timeout=20):
                server.telegram_stop.set()
                return {"ok": True, "processed": 2, "offset": 9}

            with patch.object(TelegramBot, "status", return_value={"ok": True, "bot": {"username": "nermana"}}), patch.object(
                TelegramBot, "delete_webhook", return_value={"ok": True}
            ), patch.object(TelegramBot, "poll_once", poll_once):
                result = server.start_telegram()
                server.telegram_thread.join(timeout=3)
            state = server.telegram_worker_status()
            self.assertTrue(result["ok"])
            self.assertEqual(state["processed"], 2)
            self.assertEqual(state["offset"], 9)


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

    def test_doctor_snapshot_reports_loading_model_repair(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            models_dir = Path(tmp) / "models"
            models_dir.mkdir()
            (models_dir / "phone.gguf").write_bytes(b"gguf")
            cfg = AppConfig(
                model=ModelConfig(models_dir=str(models_dir), active_model="phone.gguf"),
                memory=MemoryConfig(db_path=str(Path(tmp) / "m.sqlite3")),
            )
            agent = AgentCore(cfg)
            server = SimpleNermanaServer(agent)
            health = {
                "ok": False,
                "endpoint_ok": True,
                "state": "endpoint reachable, chat failed",
                "chat_check": {"ok": False, "error": "HTTP Error 503: Service Unavailable: Loading model"},
            }
            with patch.object(agent.models, "runtime_status", return_value=health), patch.object(
                agent.models, "llama_server_status", return_value={"available": True, "resolved": "/data/llama-server", "configured": "auto"}
            ), patch.object(agent.models, "server_log_tail", return_value={"ok": True, "lines": []}):
                snapshot = server.doctor_snapshot(force=True)
            self.assertIn("Model is still loading", [issue["title"] for issue in snapshot["issues"]])
            actions = {action["key"] for action in snapshot["actions"]}
            self.assertIn("model", actions)
            self.assertIn("auto", actions)

    def test_doctor_repair_waits_for_loading_model_before_restart(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            models_dir = Path(tmp) / "models"
            models_dir.mkdir()
            (models_dir / "phone.gguf").write_bytes(b"gguf")
            cfg = AppConfig(
                model=ModelConfig(models_dir=str(models_dir), active_model="phone.gguf"),
                memory=MemoryConfig(db_path=str(Path(tmp) / "m.sqlite3")),
            )
            agent = AgentCore(cfg)
            server = SimpleNermanaServer(agent)
            loading = {"ok": False, "endpoint_ok": True, "chat_check": {"error": "HTTP Error 503: Service Unavailable: Loading model"}}
            ready = {"ok": True, "endpoint_ok": True, "state": "chat ready"}
            with patch.object(agent.models, "runtime_status", side_effect=[loading, ready]), patch.object(
                agent.models, "restart_server", side_effect=AssertionError("restart should wait first")
            ), patch("nermana.simple_server.time.sleep", return_value=None):
                result = server.repair_model_server()
            self.assertTrue(result["ok"])
            self.assertTrue(result["waited"])

    def test_self_learning_cycle_logs_diagnostics(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = AppConfig(
                memory=MemoryConfig(db_path=str(Path(tmp) / "m.sqlite3")),
                self_learning=SelfLearningConfig(auto_repair=False, log_path=str(Path(tmp) / "self.log")),
            )
            server = SimpleNermanaServer(AgentCore(cfg))
            with patch.object(server, "doctor_snapshot", return_value={"ok": True, "summary": "No blocking issues detected.", "issues": []}):
                result = server.run_self_learning_cycle()
            self.assertTrue(result["ok"])
            status = server.self_learning_status()
            self.assertEqual(status["worker"]["cycles"], 1)
            self.assertTrue(any("diagnosis" in line for line in status["log"]["lines"]))

    def test_self_learning_cycle_runs_auto_repair_for_serious_issues(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = AppConfig(
                memory=MemoryConfig(db_path=str(Path(tmp) / "m.sqlite3")),
                self_learning=SelfLearningConfig(auto_repair=True, repair_cooldown_seconds=30, log_path=str(Path(tmp) / "self.log")),
            )
            server = SimpleNermanaServer(AgentCore(cfg))
            diagnostics = {"ok": True, "summary": "1 issue(s) detected.", "issues": [{"severity": "error", "title": "Model server is offline"}]}
            with patch.object(server, "doctor_snapshot", return_value=diagnostics), patch.object(
                server, "repair", return_value={"ok": True, "summary": "Repair finished.", "steps": [{"name": "model", "ok": True}]}
            ) as repair:
                result = server.run_self_learning_cycle()
            self.assertTrue(result["ok"])
            repair.assert_called_once_with("auto")
            self.assertEqual(server.self_learning_status()["worker"]["repairs"], 1)


class UpdaterTests(unittest.TestCase):
    def test_update_status_falls_back_to_origin_main_without_upstream(self) -> None:
        def fake_git(args):
            command = tuple(args)
            responses = {
                ("rev-parse", "--short", "HEAD"): {"ok": True, "stdout": "aaa111"},
                ("rev-parse", "--abbrev-ref", "HEAD"): {"ok": True, "stdout": "main"},
                ("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"): {"ok": False, "stderr": "no upstream"},
                ("rev-parse", "--verify", "origin/main"): {"ok": True, "stdout": "bbb222"},
                ("rev-parse", "--short", "origin/main"): {"ok": True, "stdout": "bbb222"},
                ("rev-parse", "HEAD"): {"ok": True, "stdout": "aaa111full"},
                ("rev-parse", "origin/main"): {"ok": True, "stdout": "bbb222full"},
                ("merge-base", "HEAD", "origin/main"): {"ok": True, "stdout": "aaa111full"},
                ("status", "--porcelain"): {"ok": True, "stdout": ""},
            }
            return responses.get(command, {"ok": False, "stderr": f"unexpected {args}"})

        with patch("nermana.updater._git", side_effect=fake_git):
            result = update_status(fetch=False)
        self.assertTrue(result["ok"])
        self.assertTrue(result["update_available"])
        self.assertEqual(result["upstream"], "origin/main")
        self.assertEqual(result["target"]["source"], "origin fallback")

    def test_update_status_ignores_runtime_data_and_models_dirty_entries(self) -> None:
        def fake_git(args):
            command = tuple(args)
            responses = {
                ("rev-parse", "--short", "HEAD"): {"ok": True, "stdout": "aaa111"},
                ("rev-parse", "--abbrev-ref", "HEAD"): {"ok": True, "stdout": "main"},
                ("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"): {"ok": True, "stdout": "origin/main"},
                ("rev-parse", "--short", "origin/main"): {"ok": True, "stdout": "aaa111"},
                ("rev-parse", "HEAD"): {"ok": True, "stdout": "aaa111full"},
                ("rev-parse", "origin/main"): {"ok": True, "stdout": "aaa111full"},
                ("merge-base", "HEAD", "origin/main"): {"ok": True, "stdout": "aaa111full"},
                ("status", "--porcelain"): {"ok": True, "stdout": "?? data/\n?? models/Qwen.gguf"},
            }
            return responses.get(command, {"ok": False, "stderr": f"unexpected {args}"})

        with patch("nermana.updater._git", side_effect=fake_git):
            result = update_status(fetch=False)
        self.assertTrue(result["ok"])
        self.assertFalse(result["dirty"])
        self.assertEqual(result["dirty_files"], [])

    def test_update_system_merges_origin_fallback_without_upstream(self) -> None:
        state = {"updated": False}

        def fake_git(args):
            command = tuple(args)
            if command == ("rev-parse", "--short", "HEAD"):
                return {"ok": True, "stdout": "bbb222" if state["updated"] else "aaa111"}
            if command == ("fetch", "--all", "--prune"):
                return {"ok": True, "stdout": "fetched"}
            if command == ("status", "--porcelain"):
                return {"ok": True, "stdout": ""}
            if command == ("rev-parse", "--abbrev-ref", "HEAD"):
                return {"ok": True, "stdout": "main"}
            if command == ("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"):
                return {"ok": False, "stderr": "no upstream"}
            if command == ("rev-parse", "--verify", "origin/main"):
                return {"ok": True, "stdout": "bbb222full"}
            if command == ("merge", "--ff-only", "origin/main"):
                state["updated"] = True
                return {"ok": True, "stdout": "Fast-forward"}
            if command == ("rev-parse", "--short", "origin/main"):
                return {"ok": True, "stdout": "bbb222"}
            if command == ("rev-parse", "HEAD"):
                return {"ok": True, "stdout": "bbb222full" if state["updated"] else "aaa111full"}
            if command == ("rev-parse", "origin/main"):
                return {"ok": True, "stdout": "bbb222full"}
            if command == ("merge-base", "HEAD", "origin/main"):
                return {"ok": True, "stdout": "bbb222full" if state["updated"] else "aaa111full"}
            return {"ok": False, "stderr": f"unexpected {args}"}

        with patch("nermana.updater._git", side_effect=fake_git), patch("nermana.updater._backup_config", return_value=None), patch(
            "nermana.updater._restore_config_if_missing"
        ), patch("nermana.updater._ensure_persistent_dirs"):
            result = update_system()
        self.assertTrue(result["ok"])
        self.assertEqual(result["before"], "aaa111")
        self.assertEqual(result["after"], "bbb222")
        self.assertEqual(result["target"]["target"], "origin/main")
        self.assertEqual(result["pull"]["stdout"], "Fast-forward")

    def test_update_system_stashes_dirty_source_before_merge(self) -> None:
        state = {"updated": False, "stashed": False}

        def fake_git(args):
            command = tuple(args)
            if command == ("rev-parse", "--short", "HEAD"):
                return {"ok": True, "stdout": "bbb222" if state["updated"] else "aaa111"}
            if command == ("fetch", "--all", "--prune"):
                return {"ok": True, "stdout": "fetched"}
            if command == ("status", "--porcelain"):
                return {"ok": True, "stdout": "" if state["stashed"] else " M nermana/agent.py\n?? nermana/core_knowledge.py"}
            if command[:4] == ("stash", "push", "--include-untracked", "-m"):
                state["stashed"] = True
                return {"ok": True, "stdout": "Saved working directory and index state"}
            if command == ("rev-parse", "--abbrev-ref", "HEAD"):
                return {"ok": True, "stdout": "main"}
            if command == ("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"):
                return {"ok": True, "stdout": "origin/main"}
            if command == ("merge", "--ff-only", "origin/main"):
                self.assertTrue(state["stashed"])
                state["updated"] = True
                return {"ok": True, "stdout": "Fast-forward"}
            if command == ("rev-parse", "--short", "origin/main"):
                return {"ok": True, "stdout": "bbb222"}
            if command == ("rev-parse", "HEAD"):
                return {"ok": True, "stdout": "bbb222full" if state["updated"] else "aaa111full"}
            if command == ("rev-parse", "origin/main"):
                return {"ok": True, "stdout": "bbb222full"}
            if command == ("merge-base", "HEAD", "origin/main"):
                return {"ok": True, "stdout": "bbb222full" if state["updated"] else "aaa111full"}
            return {"ok": False, "stderr": f"unexpected {args}"}

        with patch("nermana.updater._git", side_effect=fake_git), patch("nermana.updater._backup_config", return_value=None), patch(
            "nermana.updater._restore_config_if_missing"
        ), patch("nermana.updater._ensure_persistent_dirs"):
            result = update_system()
        self.assertTrue(result["ok"])
        self.assertTrue(result["dirty"])
        self.assertTrue(result["stash"]["ok"])
        self.assertEqual(result["after"], "bbb222")


if __name__ == "__main__":
    unittest.main()
