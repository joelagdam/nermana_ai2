from __future__ import annotations

import errno
import os
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from nermana.agent import AgentCore
from nermana.config import AppConfig, FileConfig, MemoryConfig, ModelConfig, SafetyConfig, SearchConfig, merge_config, save_config
from nermana.model_downloads import download_model, list_presets
from nermana.memory import MemoryStore
from nermana.models import ModelManager
from nermana.safety import DecisionGate
from nermana.simple_server import SimpleNermanaServer
from nermana.startup import StartupManager
from nermana.tooling import Tool, ToolRegistry
from nermana.tools.files import register_file_tools
from nermana.tools.search import register_search_tools


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
            self.assertTrue(store.forget(memory_id))


class SafetyTests(unittest.TestCase):
    def test_gate_blocks_dangerous_and_allows_power(self) -> None:
        gate = DecisionGate(SafetyConfig(max_tool_risk="power"))
        self.assertTrue(gate.evaluate("settings_put", "power").allowed)
        self.assertFalse(gate.evaluate("phone_shell", "dangerous").allowed)


class ToolTests(unittest.TestCase):
    def test_registry_respects_availability_and_safety(self) -> None:
        cfg = AppConfig(search=SearchConfig(enabled=True, searxng_url=""))
        registry = ToolRegistry(cfg)
        register_search_tools(registry, cfg)
        result = registry.run("web_search", {"query": "hello"})
        self.assertFalse(result["ok"])
        self.assertIn("unavailable", result["error"])

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


if __name__ == "__main__":
    unittest.main()
