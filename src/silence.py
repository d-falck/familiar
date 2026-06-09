"""Shared 'silence' sentinel and lenient matcher.

The agent framework always emits a final text turn, so we use a literal
token the agent is told to produce when there's nothing to say. Claude
drifts across variants (`<silent>`, `<silence>`, "(no response)", …), so
the matcher accepts any of them rather than only an exact string.
"""

from __future__ import annotations

import re

SILENCE_SENTINEL = "<silent>"

SILENCE_INSTRUCTION = (
    f"Silence is the default. Unless a standing instruction in memory or "
    f"context warrants a user-facing reply, or something genuinely needs "
    f"the user's attention right now, respond with EXACTLY "
    f"`{SILENCE_SENTINEL}` and nothing else — no commentary, no "
    f"acknowledgement, no variations like `<silence>`."
)

_SILENCE_RE = re.compile(
    r"^[\s`\"'*<>()\[\]]*"
    r"(silent|silence|no[-_ ]?response|nothing(?:\s+to\s+(?:do|report|say))?)"
    r"[\s`\"'*<>()\[\]\.\!\?]*$",
    re.IGNORECASE,
)


def is_silent(reply: str | None) -> bool:
    if not reply:
        return True
    s = reply.strip()
    if not s:
        return True
    if _SILENCE_RE.match(s):
        return True
    # The model sometimes narrates its silence and then appends the sentinel,
    # e.g. "Nothing needs my attention right now.\n\n<silent>". It still meant
    # to send nothing, so a standalone trailing silence token => silent turn.
    lines = [ln.strip() for ln in s.splitlines() if ln.strip()]
    if lines and _SILENCE_RE.match(lines[-1]):
        return True
    return False
