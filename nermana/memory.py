from __future__ import annotations

import json
import re
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .config import MemoryConfig, resolve_path


@dataclass
class MemoryHit:
    id: int
    content: str
    tags: str
    source: str
    created_at: float
    summary: str = ""
    entities: str = "[]"
    topics: str = "[]"
    importance: float = 0.5
    consolidated: int = 0
    connections: str = "[]"


class MemoryStore:
    def __init__(self, config: MemoryConfig):
        self.path = resolve_path(config.db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.config = config
        self._init_db()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    @contextmanager
    def connection(self):
        conn = self.connect()
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self.connection() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS memories (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    content TEXT NOT NULL,
                    tags TEXT NOT NULL DEFAULT '',
                    source TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    summary TEXT NOT NULL DEFAULT '',
                    entities TEXT NOT NULL DEFAULT '[]',
                    topics TEXT NOT NULL DEFAULT '[]',
                    importance REAL NOT NULL DEFAULT 0.5,
                    consolidated INTEGER NOT NULL DEFAULT 0,
                    connections TEXT NOT NULL DEFAULT '[]'
                )
                """
            )
            self._ensure_memory_columns(conn)
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS consolidations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_ids TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    insight TEXT NOT NULL,
                    connections TEXT NOT NULL DEFAULT '[]',
                    created_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts
                USING fts5(content, tags, source)
                """
            )

    def _ensure_memory_columns(self, conn: sqlite3.Connection) -> None:
        existing = {row["name"] for row in conn.execute("PRAGMA table_info(memories)").fetchall()}
        columns = {
            "summary": "TEXT NOT NULL DEFAULT ''",
            "entities": "TEXT NOT NULL DEFAULT '[]'",
            "topics": "TEXT NOT NULL DEFAULT '[]'",
            "importance": "REAL NOT NULL DEFAULT 0.5",
            "consolidated": "INTEGER NOT NULL DEFAULT 0",
            "connections": "TEXT NOT NULL DEFAULT '[]'",
        }
        for name, declaration in columns.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE memories ADD COLUMN {name} {declaration}")

    def ensure_session(self, session_id: str, title: str = "") -> None:
        now = time.time()
        with self.connection() as conn:
            conn.execute(
                """
                INSERT INTO sessions(id, title, created_at, updated_at)
                VALUES(?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET updated_at=excluded.updated_at
                """,
                (session_id, title, now, now),
            )

    def add_message(self, session_id: str, role: str, content: str) -> None:
        self.ensure_session(session_id)
        with self.connection() as conn:
            conn.execute(
                "INSERT INTO messages(session_id, role, content, created_at) VALUES(?, ?, ?, ?)",
                (session_id, role, content, time.time()),
            )
            conn.execute("UPDATE sessions SET updated_at=? WHERE id=?", (time.time(), session_id))
            self._trim_session(conn, session_id)

    def list_sessions(self) -> list[dict]:
        with self.connection() as conn:
            rows = conn.execute("SELECT * FROM sessions ORDER BY updated_at DESC").fetchall()
        return [dict(row) for row in rows]

    def get_messages(self, session_id: str, limit: int = 40) -> list[dict]:
        with self.connection() as conn:
            rows = conn.execute(
                """
                SELECT role, content, created_at FROM messages
                WHERE session_id=?
                ORDER BY id DESC LIMIT ?
                """,
                (session_id, limit),
            ).fetchall()
        return [dict(row) for row in reversed(rows)]

    def remember(
        self,
        content: str,
        tags: Iterable[str] | str = "",
        source: str = "manual",
        summary: str = "",
        entities: Iterable[str] | str | None = None,
        topics: Iterable[str] | str | None = None,
        importance: float | None = None,
    ) -> int:
        tag_text = ",".join(tags) if not isinstance(tags, str) else tags
        now = time.time()
        structured = self._structure_memory(content, tag_text, source)
        summary = summary or structured["summary"]
        entity_text = _json_list(entities if entities is not None else structured["entities"])
        topic_text = _json_list(topics if topics is not None else structured["topics"])
        score = float(structured["importance"] if importance is None else importance)
        with self.connection() as conn:
            cursor = conn.execute(
                """
                INSERT INTO memories(content, tags, source, created_at, summary, entities, topics, importance)
                VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (content, tag_text, source, now, summary, entity_text, topic_text, score),
            )
            memory_id = int(cursor.lastrowid)
            conn.execute(
                "INSERT INTO memory_fts(rowid, content, tags, source) VALUES(?, ?, ?, ?)",
                (memory_id, content, tag_text, source),
            )
            return memory_id

    def search(self, query: str, limit: int = 8) -> list[MemoryHit]:
        query = query.strip()
        if not query:
            return []
        tokens = _query_tokens(query)
        with self.connection() as conn:
            rows = []
            fts_query = _fts_query(tokens)
            if fts_query:
                try:
                    rows = conn.execute(
                        """
                        SELECT memories.* FROM memory_fts
                        JOIN memories ON memories.id = memory_fts.rowid
                        WHERE memory_fts MATCH ?
                        ORDER BY rank LIMIT ?
                        """,
                        (fts_query, limit),
                    ).fetchall()
                except sqlite3.OperationalError:
                    rows = []
            if rows:
                return [MemoryHit(**dict(row)) for row in rows]
            return self._ranked_fallback_search(conn, query, tokens, limit)

    def _ranked_fallback_search(self, conn: sqlite3.Connection, query: str, tokens: list[str], limit: int) -> list[MemoryHit]:
        if not tokens:
            return []
        clauses = []
        params: list[str | int] = []
        for token in tokens[:8]:
            like = f"%{token}%"
            clauses.append(
                """
                lower(content) LIKE ? OR lower(tags) LIKE ? OR lower(source) LIKE ?
                OR lower(summary) LIKE ? OR lower(entities) LIKE ? OR lower(topics) LIKE ?
                """
            )
            params.extend([like, like, like, like, like, like])
        rows = conn.execute(
            f"""
            SELECT * FROM memories
            WHERE {' OR '.join(f'({clause})' for clause in clauses)}
            ORDER BY importance DESC, created_at DESC
            LIMIT ?
            """,
            (*params, max(limit * 8, 32)),
        ).fetchall()
        ranked = []
        query_lower = query.lower()
        for row in rows:
            item = dict(row)
            haystack = " ".join(
                str(item.get(field, "")).lower()
                for field in ["content", "tags", "source", "summary", "entities", "topics"]
            )
            matched = sum(1 for token in tokens if token in haystack)
            if not matched:
                continue
            phrase_bonus = 2 if query_lower in haystack else 0
            density = matched / max(1, len(tokens))
            score = phrase_bonus + matched + density + float(item.get("importance", 0.5))
            ranked.append((score, float(item.get("created_at", 0)), item))
        ranked.sort(key=lambda item: (-item[0], -item[1]))
        return [MemoryHit(**item) for _score, _created, item in ranked[:limit]]

    def list_memories(self, limit: int = 100) -> list[dict]:
        with self.connection() as conn:
            rows = conn.execute("SELECT * FROM memories ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return [dict(row) for row in rows]

    def get_memory(self, memory_id: int) -> dict | None:
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM memories WHERE id=?", (memory_id,)).fetchone()
        return dict(row) if row else None

    def update_memory(self, memory_id: int, patch: dict) -> dict:
        current = self.get_memory(memory_id)
        if current is None:
            return {"ok": False, "error": "memory not found"}
        content = str(patch.get("content", current["content"]))
        tags = str(patch.get("tags", current["tags"]))
        source = str(patch.get("source", current["source"]))
        summary = str(patch.get("summary", current["summary"]))
        entities = _json_list(patch.get("entities", current["entities"]))
        topics = _json_list(patch.get("topics", current["topics"]))
        try:
            importance = float(patch.get("importance", current["importance"]))
        except (TypeError, ValueError):
            importance = float(current["importance"])
        consolidated = 1 if bool(patch.get("consolidated", current["consolidated"])) else 0
        with self.connection() as conn:
            conn.execute(
                """
                UPDATE memories
                SET content=?, tags=?, source=?, summary=?, entities=?, topics=?, importance=?, consolidated=?
                WHERE id=?
                """,
                (content, tags, source, summary, entities, topics, importance, consolidated, memory_id),
            )
            conn.execute("DELETE FROM memory_fts WHERE rowid=?", (memory_id,))
            conn.execute(
                "INSERT INTO memory_fts(rowid, content, tags, source) VALUES(?, ?, ?, ?)",
                (memory_id, content, tags, source),
            )
        updated = self.get_memory(memory_id) or {}
        updated["ok"] = True
        return updated

    def count_memories(self) -> int:
        with self.connection() as conn:
            row = conn.execute("SELECT COUNT(*) AS total FROM memories").fetchone()
        return int(row["total"] if row else 0)

    def count_unconsolidated(self) -> int:
        with self.connection() as conn:
            row = conn.execute("SELECT COUNT(*) AS total FROM memories WHERE consolidated = 0").fetchone()
        return int(row["total"] if row else 0)

    def list_consolidations(self, limit: int = 10) -> list[dict]:
        with self.connection() as conn:
            rows = conn.execute("SELECT * FROM consolidations ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return [dict(row) for row in rows]

    def consolidate_due(self, min_items: int = 4) -> dict:
        if self.count_unconsolidated() < min_items:
            return {"ok": True, "consolidated": False, "reason": "not enough new memories"}
        return self.consolidate(limit=max(min_items, 8))

    def consolidate(self, limit: int = 8) -> dict:
        with self.connection() as conn:
            rows = conn.execute(
                """
                SELECT * FROM memories
                WHERE consolidated = 0
                ORDER BY importance DESC, created_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            memories = [dict(row) for row in rows]
            if len(memories) < 2:
                return {"ok": True, "consolidated": False, "reason": "not enough memories"}
            source_ids = [int(memory["id"]) for memory in memories]
            connections = _memory_connections(memories)
            summary = _consolidation_summary(memories)
            insight = _consolidation_insight(memories, connections)
            now = time.time()
            conn.execute(
                """
                INSERT INTO consolidations(source_ids, summary, insight, connections, created_at)
                VALUES(?, ?, ?, ?, ?)
                """,
                (json.dumps(source_ids), summary, insight, json.dumps(connections), now),
            )
            for connection in connections:
                for current, linked in [(connection["from_id"], connection["to_id"]), (connection["to_id"], connection["from_id"])]:
                    row = conn.execute("SELECT connections FROM memories WHERE id=?", (current,)).fetchone()
                    existing = _safe_json(row["connections"] if row else "[]", [])
                    existing.append({"linked_to": linked, "relationship": connection["relationship"]})
                    conn.execute("UPDATE memories SET connections=? WHERE id=?", (json.dumps(existing), current))
            conn.execute(
                f"UPDATE memories SET consolidated = 1 WHERE id IN ({','.join('?' for _ in source_ids)})",
                source_ids,
            )
        return {"ok": True, "consolidated": True, "source_ids": source_ids, "summary": summary, "insight": insight, "connections": connections}

    def forget(self, memory_id: int) -> bool:
        with self.connection() as conn:
            cursor = conn.execute("DELETE FROM memories WHERE id=?", (memory_id,))
            conn.execute("DELETE FROM memory_fts WHERE rowid=?", (memory_id,))
            return cursor.rowcount > 0

    def maybe_remember(self, message: str, reply: str) -> None:
        if not self.config.auto_remember:
            return
        lowered = message.lower()
        markers = [
            "remember ",
            "my name is ",
            "call me ",
            "i prefer ",
            "i like ",
            "i live in ",
            "i use ",
            "my phone ",
            "my model ",
            "nermana should ",
            "nermana needs ",
            "fix ",
        ]
        if not any(marker in lowered for marker in markers):
            return
        content = f"User said: {message}\nNermana replied: {reply[:600]}"
        self.remember(content, tags="conversation,user,auto", source="chat")

    def _trim_session(self, conn: sqlite3.Connection, session_id: str) -> None:
        keep = max(20, int(self.config.retain_messages))
        conn.execute(
            """
            DELETE FROM messages
            WHERE session_id=? AND id NOT IN (
                SELECT id FROM messages WHERE session_id=? ORDER BY id DESC LIMIT ?
            )
            """,
            (session_id, session_id, keep),
        )

    def _structure_memory(self, content: str, tags: str, source: str) -> dict:
        text = " ".join(content.split())
        return {
            "summary": _summarize_text(text),
            "entities": _extract_entities(text),
            "topics": _extract_topics(" ".join([text, tags, source])),
            "importance": _importance_score(text, tags),
        }


STOPWORDS = {
    "about",
    "after",
    "again",
    "assistant",
    "because",
    "before",
    "could",
    "from",
    "have",
    "into",
    "just",
    "like",
    "more",
    "nermana",
    "reply",
    "said",
    "should",
    "that",
    "their",
    "there",
    "this",
    "user",
    "with",
    "would",
}


def _summarize_text(text: str, limit: int = 220) -> str:
    if len(text) <= limit:
        return text
    sentence = re.split(r"(?<=[.!?])\s+", text)[0]
    if 20 <= len(sentence) <= limit:
        return sentence
    return text[: limit - 1].rstrip() + "."


def _query_tokens(query: str) -> list[str]:
    raw_tokens = re.findall(r"[A-Za-z0-9_]+", query.lower())
    tokens: list[str] = []
    seen: set[str] = set()
    for token in raw_tokens:
        if len(token) < 2 and not token.isdigit():
            continue
        if token in STOPWORDS or token in seen:
            continue
        seen.add(token)
        tokens.append(token)
        if token.endswith("s") and len(token) > 4:
            singular = token[:-1]
            if singular not in STOPWORDS and singular not in seen:
                seen.add(singular)
                tokens.append(singular)
        if len(tokens) >= 12:
            break
    return tokens


def _fts_query(tokens: list[str]) -> str:
    if not tokens:
        return ""
    return " AND ".join(f'"{token}"' for token in tokens[:8])


def _extract_entities(text: str) -> list[str]:
    candidates = re.findall(r"\b(?:[A-Z][A-Za-z0-9_.-]+(?:\s+[A-Z][A-Za-z0-9_.-]+){0,3}|[A-Z0-9]{2,})\b", text)
    seen: set[str] = set()
    entities: list[str] = []
    for item in candidates:
        clean = item.strip(" ,.")
        if len(clean) < 2 or clean.lower() in STOPWORDS or clean in seen:
            continue
        seen.add(clean)
        entities.append(clean)
        if len(entities) >= 8:
            break
    return entities


def _extract_topics(text: str) -> list[str]:
    words = re.findall(r"[a-zA-Z][a-zA-Z0-9_-]{3,}", text.lower())
    counts: dict[str, int] = {}
    for word in words:
        if word in STOPWORDS:
            continue
        counts[word] = counts.get(word, 0) + 1
    return [word for word, _count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:6]]


def _importance_score(text: str, tags: str) -> float:
    lowered = f"{text} {tags}".lower()
    score = 0.45
    if any(word in lowered for word in ["prefer", "remember", "important", "always", "never", "name is", "live in"]):
        score += 0.25
    if any(word in lowered for word in ["nermana", "model", "termux", "telegram", "search", "weather"]):
        score += 0.15
    if len(text) > 300:
        score += 0.1
    return min(1.0, round(score, 2))


def _json_list(value: Iterable[str] | str) -> str:
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            if isinstance(parsed, list):
                return json.dumps([str(item) for item in parsed])
        except json.JSONDecodeError:
            return json.dumps([part.strip() for part in value.split(",") if part.strip()])
    return json.dumps([str(item) for item in value])


def _safe_json(value: str, fallback):
    try:
        return json.loads(value or "")
    except (json.JSONDecodeError, TypeError):
        return fallback


def _memory_connections(memories: list[dict]) -> list[dict]:
    connections: list[dict] = []
    for index, left in enumerate(memories):
        left_topics = set(_safe_json(left.get("topics", "[]"), []))
        left_entities = set(_safe_json(left.get("entities", "[]"), []))
        for right in memories[index + 1 :]:
            overlap = left_topics.intersection(_safe_json(right.get("topics", "[]"), []))
            entity_overlap = left_entities.intersection(_safe_json(right.get("entities", "[]"), []))
            if not overlap and not entity_overlap:
                continue
            shared = sorted(overlap or entity_overlap)
            connections.append(
                {
                    "from_id": int(left["id"]),
                    "to_id": int(right["id"]),
                    "relationship": f"shared {', '.join(shared[:3])}",
                }
            )
            if len(connections) >= 12:
                return connections
    return connections


def _consolidation_summary(memories: list[dict]) -> str:
    summaries = [memory.get("summary") or _summarize_text(memory.get("content", "")) for memory in memories[:5]]
    return " | ".join(summary for summary in summaries if summary)[:700]


def _consolidation_insight(memories: list[dict], connections: list[dict]) -> str:
    topics: dict[str, int] = {}
    for memory in memories:
        for topic in _safe_json(memory.get("topics", "[]"), []):
            topics[topic] = topics.get(topic, 0) + 1
    top = [topic for topic, _count in sorted(topics.items(), key=lambda item: (-item[1], item[0]))[:4]]
    if connections:
        return f"Repeated pattern across memory: {', '.join(top) or 'related user priorities'}; {len(connections)} links were found."
    return f"Recent memories mostly cluster around: {', '.join(top) or 'general user context'}."
