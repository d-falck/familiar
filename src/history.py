"""Per-chat message history in SQLite."""

from __future__ import annotations

import sqlite3
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id    INTEGER NOT NULL,
    role       TEXT    NOT NULL,
    author     TEXT,
    content    TEXT    NOT NULL,
    created_at REAL    NOT NULL DEFAULT (strftime('%s','now'))
);
CREATE INDEX IF NOT EXISTS idx_messages_chat ON messages(chat_id, id);
"""


class History:
    def __init__(self, path: str):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)

    def add_user(self, chat_id: int, author: str, text: str) -> None:
        self._conn.execute(
            "INSERT INTO messages (chat_id, role, author, content) VALUES (?, 'user', ?, ?)",
            (chat_id, author, text),
        )

    def add_assistant(self, chat_id: int, text: str) -> None:
        self._conn.execute(
            "INSERT INTO messages (chat_id, role, author, content) VALUES (?, 'assistant', NULL, ?)",
            (chat_id, text),
        )

    def load_as_messages(self, chat_id: int) -> list[dict]:
        """Return the full chat as Anthropic-shaped turns, coalescing consecutive user rows."""
        rows = self._conn.execute(
            "SELECT role, author, content FROM messages WHERE chat_id = ? ORDER BY id",
            (chat_id,),
        ).fetchall()

        messages: list[dict] = []
        pending: list[str] = []
        for role, author, content in rows:
            if role == "user":
                pending.append(f"{author}: {content}" if author else content)
                continue
            if pending:
                messages.append({"role": "user", "content": "\n".join(pending)})
                pending.clear()
            messages.append({"role": "assistant", "content": content})
        if pending:
            messages.append({"role": "user", "content": "\n".join(pending)})
        return messages
