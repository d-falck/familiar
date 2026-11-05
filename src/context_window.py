"""Keep the transcript handed to the model bounded.

Every turn replays the *whole* session transcript, so a long-lived chat makes
even "hi" expensive. Measured 2026-11-05: the main DM was 1,130 messages /
529KB (~132k tokens), and a forum-topic turn carries that full DM history as
shared background *on top of* its own thread. That prefill — not the model —
is what made short back-and-forths feel slow.

Trimming is safe here in a way it wouldn't be for a stateless chatbot:

* the complete history stays in the SQLite file the system prompt points at,
  so any older turn can still be fetched on demand;
* durable facts live in memory.md, which is never trimmed.

So the newest window is kept verbatim and a marker tells the model what was
dropped and where to go looking, rather than silently shortening its memory.
"""

from __future__ import annotations

import logging
import os

log = logging.getLogger(__name__)


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    try:
        value = int(raw) if raw not in (None, "") else default
    except ValueError:
        log.warning("bad %s=%r, using %s", name, raw, default)
        return default
    return value if value > 0 else default


# Budgets are in characters, not tokens: no tokenizer dependency, no model
# coupling, and it's a hot path. ~4 chars/token, so 120k chars ≈ 30k tokens.
def session_budget() -> int:
    """Window for the session actually being talked in."""
    return _env_int("CONTEXT_SESSION_BUDGET_CHARS", 120_000)


def background_budget() -> int:
    """Window for the main-DM history prefixed onto a forum-topic session.

    Smaller than the session budget on purpose: it's there so a topic knows
    what's going on in Damon's life, not so it can quote the DM verbatim.
    """
    return _env_int("CONTEXT_BACKGROUND_BUDGET_CHARS", 40_000)


def fast_budget() -> int:
    """Window for a turn that plainly needs no history (see fast_path)."""
    return _env_int("CONTEXT_FAST_BUDGET_CHARS", 20_000)


def min_messages() -> int:
    """Floor kept regardless of budget, so recency never collapses."""
    return _env_int("CONTEXT_MIN_MESSAGES", 30)


_MARKER = (
    "[Context note: {dropped} older message(s) earlier in this same "
    "conversation were trimmed from this transcript to keep the turn fast. "
    "Nothing is lost — the full history is in the SQLite file named in the "
    "cross-session history section of your system prompt, so query it if you "
    "need something from earlier rather than guessing or claiming it didn't "
    "happen. Durable facts are in your memory file, which is never trimmed.]"
)


def total_chars(messages: list[dict]) -> int:
    return sum(len(m.get("content") or "") for m in messages)


def trim(
    messages: list[dict],
    *,
    budget_chars: int | None = None,
    floor: int | None = None,
    marker: bool = True,
) -> list[dict]:
    """Return the newest messages fitting `budget_chars`, oldest-first.

    At least `floor` messages are always kept even if that exceeds the budget
    (a handful of huge recent turns must not wipe out the conversation), and a
    marker message is prepended when anything was dropped.
    """
    if not messages:
        return messages
    budget = session_budget() if budget_chars is None else budget_chars
    keep_min = min_messages() if floor is None else floor

    if total_chars(messages) <= budget:
        return messages

    kept: list[dict] = []
    used = 0
    for msg in reversed(messages):
        size = len(msg.get("content") or "")
        if len(kept) >= keep_min and used + size > budget:
            break
        kept.append(msg)
        used += size
    kept.reverse()

    dropped = len(messages) - len(kept)
    if dropped <= 0:
        return messages
    log.info(
        "trimmed transcript: kept %d/%d messages (%d chars, budget %d)",
        len(kept),
        len(messages),
        used,
        budget,
    )
    if not marker:
        return kept
    return [{"role": "user", "content": _MARKER.format(dropped=dropped)}] + kept
