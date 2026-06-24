from __future__ import annotations

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

    def remember(self, content: str, tags: Iterable[str] | str = "", source: str = "manual") -> int:
        tag_text = ",".join(tags) if not isinstance(tags, str) else tags
        now = time.time()
        with self.connection() as conn:
            cursor = conn.execute(
                "INSERT INTO memories(content, tags, source, created_at) VALUES(?, ?, ?, ?)",
                (content, tag_text, source, now),
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
        with self.connection() as conn:
            try:
                rows = conn.execute(
                    """
                    SELECT memories.* FROM memory_fts
                    JOIN memories ON memories.id = memory_fts.rowid
                    WHERE memory_fts MATCH ?
                    ORDER BY rank LIMIT ?
                    """,
                    (query, limit),
                ).fetchall()
            except sqlite3.OperationalError:
                like = f"%{query}%"
                rows = conn.execute(
                    """
                    SELECT * FROM memories
                    WHERE content LIKE ? OR tags LIKE ? OR source LIKE ?
                    ORDER BY created_at DESC LIMIT ?
                    """,
                    (like, like, like, limit),
                ).fetchall()
        return [MemoryHit(**dict(row)) for row in rows]

    def list_memories(self, limit: int = 100) -> list[dict]:
        with self.connection() as conn:
            rows = conn.execute("SELECT * FROM memories ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return [dict(row) for row in rows]

    def count_memories(self) -> int:
        with self.connection() as conn:
            row = conn.execute("SELECT COUNT(*) AS total FROM memories").fetchone()
        return int(row["total"] if row else 0)

    def forget(self, memory_id: int) -> bool:
        with self.connection() as conn:
            cursor = conn.execute("DELETE FROM memories WHERE id=?", (memory_id,))
            conn.execute("DELETE FROM memory_fts WHERE rowid=?", (memory_id,))
            return cursor.rowcount > 0

    def maybe_remember(self, message: str, reply: str) -> None:
        if not self.config.auto_remember:
            return
        lowered = message.lower()
        markers = ["remember ", "my name is ", "i prefer ", "i like ", "i live in "]
        if any(marker in lowered for marker in markers):
            self.remember(f"User said: {message}\nAssistant replied: {reply}", tags="conversation,user", source="chat")

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
