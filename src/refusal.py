"""Detect a provider-level refusal in a finished turn.

Anthropic runs a usage-policy classifier over the WHOLE conversation, not
just the newest message, and the Claude Code CLI hands back a fixed refusal
string when it fires. Every turn here replays the entire per-session
transcript (see `History.load_as_messages`), so a refusal that gets persisted
as an assistant row is re-submitted to the classifier on every later turn —
one refusal wedges the session permanently, which is exactly the "stuck in a
refusal" failure mode.

Callers use `is_refusal` to keep these out of the history DB and to send the
user something actionable instead of the raw provider string. Deliberately
narrow: it matches provider-level *blocks* only, never a model politely
declining a task, so a genuine reply that happens to discuss policy won't
trip it.
"""

from __future__ import annotations

import re

_REFUSAL_PATTERNS = (
    # Claude Code's fixed usage-policy block, e.g. "Claude Code is unable to
    # respond to this request, which appears to violate our Usage Policy".
    re.compile(
        r"unable to (?:respond|continue|assist|help)\b.{0,120}?violate.{0,60}?usage polic",
        re.IGNORECASE | re.DOTALL,
    ),
    # Same block, and any paraphrase that names the policy as the reason.
    re.compile(
        r"violat\w*\s+(?:our|the|Anthropic'?s|OpenAI'?s)\s+(?:usage|acceptable\s+use)\s+polic",
        re.IGNORECASE,
    ),
    # API-side content filter, surfaced verbatim by the SDK (often as an
    # exception string that we then format into the "⚠️ …" reply).
    re.compile(r"blocked by (?:the )?content[- ]filtering polic", re.IGNORECASE),
    re.compile(r"\bstop_reason\W{0,4}refusal\b", re.IGNORECASE),
)


def is_refusal(reply: str | None) -> bool:
    """True when `reply` is a provider usage-policy block rather than a turn."""
    if not reply:
        return False
    return any(p.search(reply) for p in _REFUSAL_PATTERNS)
