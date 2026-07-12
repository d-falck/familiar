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
    f"acknowledgement, no variations like `<silence>`. NEVER narrate, "
    f"explain, or describe your decision in the reply, in any language: do "
    f"not write things like \"this is a non-triage tick\" or \"nothing "
    f"warrants breaking silence\". Keep all such reasoning internal. Your "
    f"entire reply is either a real user-facing message or the bare "
    f"sentinel — nothing in between."
)

_SILENCE_RE = re.compile(
    r"^[\s`\"'*<>()\[\]]*"
    r"(silent|silence|no[-_ ]?response|nothing(?:\s+to\s+(?:do|report|say))?)"
    r"[\s`\"'*<>()\[\]\.\!\?]*$",
    re.IGNORECASE,
)

# A silence sentinel token (`<silent>`, `<silence>`, `(no response)`) appearing
# ANYWHERE in the text. On a default-silent path its presence means the model
# meant to stay silent but wrapped/appended the sentinel inside narration —
# e.g. "…我应该回复 `<silent>`。<silent>" (language-agnostic).
_SILENCE_TOKEN_ANYWHERE_RE = re.compile(
    r"[<(\[]\s*(?:silent|silence|no[-_ ]?response)\s*[>)\]]",
    re.IGNORECASE,
)

# High-precision echoes of the scheduler/proactive *prompt* that only appear
# when the model leaks its internal deliberation instead of emitting the bare
# sentinel. These never occur in a genuine proactive nudge (travel reminder,
# deadline, email flag) or a real triage summary, so matching them on a
# default-silent path is safe. Cross-language leaks are still caught above via
# the sentinel checks.
_REASONING_LEAK_RE = re.compile(
    r"non[-\s]?triage tick"
    r"|scheduled (?:check[-\s]?in|tick)"
    r"|break(?:ing)? silence"
    r"|(?:stay|remain|staying|remaining|should stay|will stay) silent",
    re.IGNORECASE,
)


def is_placeholder_reply(reply: str | None) -> bool:
    """True when the ENTIRE reply is empty or a bare silence/no-response
    sentinel — i.e. the model collapsed instead of producing real text.

    SAFE for a NORMAL user-reply path (unlike is_silent, which also matches
    embedded sentinels and reasoning-leak phrases): this only fires when the
    *whole* stripped message is a sentinel, so a genuine answer that merely
    mentions "silence" or "no response" mid-sentence will NOT match. Use it to
    catch a collapsed turn and replace the confusing literal "(no response)"
    placeholder with an honest message, rather than sending garbage to the user.
    """
    if not reply:
        return True
    s = reply.strip()
    if not s:
        return True
    return bool(_SILENCE_RE.match(s))


def is_silent(reply: str | None) -> bool:
    """True when a *proactive*, default-silent turn should send nothing.

    Only applied to default-silent paths (scheduler ticks, trigger handlers),
    never to a normal user reply — so it can aggressively swallow leaked
    sentinels and leaked internal deliberation without risking a real answer.
    """
    if not reply:
        return True
    s = reply.strip()
    if not s:
        return True
    if _SILENCE_RE.match(s):
        return True
    # Narration then a standalone sentinel on the final line.
    lines = [ln.strip() for ln in s.splitlines() if ln.strip()]
    if lines and _SILENCE_RE.match(lines[-1]):
        return True
    # A sentinel appended/embedded anywhere (any language).
    if _SILENCE_TOKEN_ANYWHERE_RE.search(s):
        return True
    # Leaked internal deliberation about whether to break silence.
    if _REASONING_LEAK_RE.search(s):
        return True
    return False
