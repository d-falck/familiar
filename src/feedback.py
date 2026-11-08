"""One-tap ratings on proactive messages — the missing precision loop.

Every message Iris sends without being asked (scheduler ticks, the spend
digest, Gmail-trigger nudges) carries two inline buttons: 👍 Useful and
👎 Noise. A tap is stored here, echoed into that session's transcript so the
next turn sees it, and summarised back into the scheduler prompt so the bar
for breaking silence is tuned by Damon's actual ratings rather than by prose
rules accumulated from him getting annoyed.

Stored in the same SQLite file as the chat history (its own table).
"""

from __future__ import annotations

import logging
import sqlite3
import time
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from telegram_md import send_markdown

log = logging.getLogger("feedback")

_CREATE = """
CREATE TABLE IF NOT EXISTS proactive (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id    INTEGER NOT NULL,
    thread_id  INTEGER NOT NULL DEFAULT 0,
    message_id INTEGER,
    kind       TEXT    NOT NULL,
    excerpt    TEXT    NOT NULL,
    sent_at    REAL    NOT NULL,
    rating     TEXT,
    rated_at   REAL
);
"""

RATINGS = {"u": "useful", "n": "noise"}
_LABEL = {"useful": "👍 Marked useful", "noise": "👎 Marked noise"}
_EXCERPT_CHARS = 280


def _excerpt(text: str) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= _EXCERPT_CHARS else flat[: _EXCERPT_CHARS - 1] + "…"


def keyboard(row_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("👍 Useful", callback_data=f"fb:{row_id}:u"),
                InlineKeyboardButton("👎 Noise", callback_data=f"fb:{row_id}:n"),
            ]
        ]
    )


def rated_keyboard(rating: str) -> InlineKeyboardMarkup:
    # A single inert button replaces the pair, so the message shows what was
    # recorded and can't be double-rated by accident.
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(_LABEL.get(rating, rating), callback_data="fb:done")]]
    )


def parse_callback(data: str) -> tuple[int, str] | None:
    """``fb:<row>:<u|n>`` → (row_id, rating). Anything else → None."""
    parts = (data or "").split(":")
    if len(parts) != 3 or parts[0] != "fb" or parts[2] not in RATINGS:
        return None
    try:
        return int(parts[1]), RATINGS[parts[2]]
    except ValueError:
        return None


class Feedback:
    def __init__(self, path: str):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_CREATE)

    def record_sent(self, chat_id: int, thread_id: int, kind: str, text: str) -> int:
        cur = self._conn.execute(
            "INSERT INTO proactive (chat_id, thread_id, kind, excerpt, sent_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (chat_id, thread_id, kind, _excerpt(text), time.time()),
        )
        return int(cur.lastrowid)

    def set_message_id(self, row_id: int, message_id: int) -> None:
        self._conn.execute(
            "UPDATE proactive SET message_id = ? WHERE id = ?", (message_id, row_id)
        )

    def rate(self, row_id: int, rating: str) -> dict | None:
        """Record a rating; returns the row, or None if the id is unknown.
        Re-rating overwrites (a mis-tap can be corrected by tapping again
        before the buttons are swapped)."""
        self._conn.execute(
            "UPDATE proactive SET rating = ?, rated_at = ? WHERE id = ?",
            (rating, time.time(), row_id),
        )
        row = self._conn.execute(
            "SELECT id, chat_id, thread_id, kind, excerpt, rating FROM proactive "
            "WHERE id = ?",
            (row_id,),
        ).fetchone()
        if not row:
            return None
        keys = ("id", "chat_id", "thread_id", "kind", "excerpt", "rating")
        return dict(zip(keys, row))

    def stats(self, days: int = 30) -> list[dict]:
        """Per-kind counts over the window: sent, useful, noise, unrated."""
        since = time.time() - days * 86400
        rows = self._conn.execute(
            "SELECT kind, COUNT(*), "
            "SUM(rating = 'useful'), SUM(rating = 'noise'), SUM(rating IS NULL) "
            "FROM proactive WHERE sent_at >= ? GROUP BY kind ORDER BY COUNT(*) DESC",
            (since,),
        ).fetchall()
        return [
            {
                "kind": k,
                "sent": n,
                "useful": u or 0,
                "noise": z or 0,
                "unrated": r or 0,
            }
            for k, n, u, z, r in rows
        ]

    def recent_summary(self, days: int = 21, limit: int = 8) -> str:
        """Short block for the scheduler prompt: Damon's latest ratings, so
        the silence bar is calibrated on evidence. Empty when nothing rated."""
        since = time.time() - days * 86400
        rows = self._conn.execute(
            "SELECT kind, rating, excerpt FROM proactive "
            "WHERE rating IS NOT NULL AND rated_at >= ? "
            "ORDER BY rated_at DESC LIMIT ?",
            (since, limit),
        ).fetchall()
        if not rows:
            return ""
        lines = [
            "Damon's recent one-tap ratings of your proactive messages "
            "(calibrate what's worth sending on these — 'noise' means don't "
            "send that kind of thing again):"
        ]
        for kind, rating, excerpt in rows:
            lines.append(f"- [{rating.upper()}] {kind}: {excerpt}")
        return "\n".join(lines)


async def send_rated(
    telegram_bot,
    feedback: Feedback | None,
    *,
    chat_id: int,
    thread_id: int = 0,
    kind: str,
    text: str,
) -> None:
    """Send a proactive message with 👍/👎 buttons and log it for rating.

    Falls back to a plain send when no Feedback store is wired, so callers
    never have to branch. A logging failure must never cost the message.
    """
    row_id = None
    if feedback is not None:
        try:
            row_id = feedback.record_sent(chat_id, thread_id, kind, text)
        except Exception:
            log.exception("could not record proactive message; sending unrated")
    markup = keyboard(row_id) if row_id is not None else None

    def _send(body, pm):
        kwargs = {"chat_id": chat_id, "text": body, "parse_mode": pm}
        if thread_id:
            kwargs["message_thread_id"] = thread_id
        if markup is not None:
            kwargs["reply_markup"] = markup
        return telegram_bot.send_message(**kwargs)

    sent = await send_markdown(_send, text)
    if row_id is not None and sent is not None:
        try:
            feedback.set_message_id(row_id, sent.message_id)
        except Exception:
            log.exception("could not store message id for proactive row %s", row_id)
