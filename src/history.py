"""Per-session message history in SQLite.

A session is a (chat_id, thread_id) pair. thread_id is the Telegram forum
topic id (message_thread_id); it's 0 for DMs and non-topic chats, so each
forum topic is an isolated conversation while normal chats are unaffected.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id    INTEGER NOT NULL,
    thread_id  INTEGER NOT NULL DEFAULT 0,
    role       TEXT    NOT NULL,
    author     TEXT,
    content    TEXT    NOT NULL,
    created_at REAL    NOT NULL DEFAULT (strftime('%s','now'))
);
"""


class History:
    def __init__(self, path: str):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_CREATE_TABLE)
        # Migrate pre-thread databases BEFORE building the thread-aware index.
        cols = [r[1] for r in self._conn.execute("PRAGMA table_info(messages)").fetchall()]
        if "thread_id" not in cols:
            self._conn.execute(
                "ALTER TABLE messages ADD COLUMN thread_id INTEGER NOT NULL DEFAULT 0"
            )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_messages_session "
            "ON messages(chat_id, thread_id, id)"
        )

    def add_user(self, chat_id: int, author: str, text: str, *, thread_id: int = 0) -> None:
        self._conn.execute(
            "INSERT INTO messages (chat_id, thread_id, role, author, content) "
            "VALUES (?, ?, 'user', ?, ?)",
            (chat_id, thread_id, author, text),
        )

    def add_assistant(self, chat_id: int, text: str, *, thread_id: int = 0) -> None:
        self._conn.execute(
            "INSERT INTO messages (chat_id, thread_id, role, author, content) "
            "VALUES (?, ?, 'assistant', NULL, ?)",
            (chat_id, thread_id, text),
        )

    def load_as_messages(self, chat_id: int, thread_id: int = 0) -> list[dict]:
        """Return one session as Anthropic-shaped turns, coalescing consecutive user rows."""
        rows = self._conn.execute(
            "SELECT role, author, content FROM messages "
            "WHERE chat_id = ? AND thread_id = ? ORDER BY id",
            (chat_id, thread_id),
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
