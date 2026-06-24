from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from nermana.agent import AgentCore
from nermana.config import AppConfig, FileConfig, MemoryConfig, ModelConfig, SafetyConfig, SearchConfig, merge_config, save_config
from nermana.memory import MemoryStore
from nermana.models import ModelManager
from nermana.safety import DecisionGate
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


if __name__ == "__main__":
    unittest.main()
