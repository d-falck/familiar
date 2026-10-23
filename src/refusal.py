"""Detect a provider-level non-answer in a finished turn.

Three kinds, all of which arrive as ordinary assistant *text* rather than a
raised exception: a usage-policy block, a quota/usage limit, and a bare
harness error. None of them is a turn, so none should ever be persisted or
posted on a default-silent path — see `undeliverable_reason`.

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
    # Claude Code 2.1.2xx+ / Opus 5-era wording. Shares almost no phrasing
    # with the older block above — it never says "usage policy" and never says
    # "unable to respond" — so it slipped past every pattern here and got
    # posted AND persisted (then replayed to the classifier every later turn,
    # which is why one flagged tick turned into a day of them). Real text:
    #   "API Error: Opus 5's safeguards flagged this message
    #    (https://www.anthropic.com/legal/aup). … Claude Code can't respond to
    #    this message with Opus 5. … Details: `[bio]`"
    re.compile(r"safeguards? flagged this (?:message|request|conversation)", re.IGNORECASE),
    re.compile(
        r"can'?t respond to this (?:message|request)\b.{0,80}?\bwith\b",
        re.IGNORECASE | re.DOTALL,
    ),
    # The AUP link only counts inside an error envelope, so that *talking*
    # about the policy (or about this bug) isn't mistaken for being blocked.
    re.compile(r"API Error\b.{0,300}?anthropic\.com/legal/aup", re.IGNORECASE | re.DOTALL),
)

# Provider quota/usage exhaustion. The turn was fine; the *model* is
# unavailable, and the fix is to switch models — which the agent cannot do for
# you, because it is the unavailable thing.
_LIMIT_MARKERS = (
    "usage limit reached",
    "reached your usage limit",
    "switch to another model to continue",
    "credit balance is too low",
    "insufficient_quota",
    "exceeded your current quota",
)
_LIMIT_PATTERN = re.compile(r"reached your \w[\w.\- ]{0,30} limit", re.IGNORECASE)

# `_LIMIT_MARKERS` match anywhere, which is right for an *exception* detail and
# wrong for a *reply*: a real answer that quotes a quota error ("the key is
# valid but the account `exceeded your current quota`, so top up at…") is a
# genuine turn and must be kept. The notices are one or two bare sentences and
# real answers about a quota are not, so length is the honest separator.
# Audited against history.sqlite: the single stored "limit" row was exactly
# that false positive — a 1.4k-char answer diagnosing an OpenAI billing error.
_LIMIT_REPLY_MAX_CHARS = 400

# A harness/transport failure serialized into the reply slot. Anchored to the
# start of the message so a reply that merely quotes one is not swallowed.
_BACKEND_ERROR_PATTERNS = (
    re.compile(r"^\s*(?:⚠️\s*)?API Error\b", re.IGNORECASE),
    re.compile(r"^\s*(?:⚠️\s*)?Failed to authenticate\b", re.IGNORECASE),
    re.compile(r"^\s*(?:⚠️\s*)?\w[\w ]{0,20} produced no output for \d+", re.IGNORECASE),
    re.compile(r"^\s*(?:⚠️\s*)?(?:trigger handler|voice dispatch) failed\b", re.IGNORECASE),
)


def is_refusal(reply: str | None) -> bool:
    """True when `reply` is a provider usage-policy block rather than a turn."""
    if not reply:
        return False
    return any(p.search(reply) for p in _REFUSAL_PATTERNS)


def is_limit_message(value: object) -> bool:
    """True when `value` is a provider quota/usage-limit notice."""
    if not value:
        return False
    detail = str(value).lower()
    return any(m in detail for m in _LIMIT_MARKERS) or bool(_LIMIT_PATTERN.search(detail))


def is_limit_reply(reply: str | None) -> bool:
    """True when `reply` *is* a limit notice rather than a turn that mentions
    one. Use this on reply text; use `is_limit_message` on exception detail."""
    if not reply or len(reply.strip()) > _LIMIT_REPLY_MAX_CHARS:
        return False
    return is_limit_message(reply)


def is_backend_error(reply: str | None) -> bool:
    """True when `reply` is a raw harness/transport error, not a turn."""
    if not reply:
        return False
    return any(p.search(reply) for p in _BACKEND_ERROR_PATTERNS)


def undeliverable_reason(reply: str | None) -> str | None:
    """Why a default-silent path must not post `reply` — or None if it's a
    real turn.

    Proactive paths (scheduler ticks, Composio triggers, voice dispatch) have
    nobody waiting on an answer, so a provider non-answer there is pure noise:
    it must be logged and dropped, never sent and never written to history.
    Posting them is what turned one quota limit into 55 chat messages and one
    flagged tick into a self-sustaining day of them.
    """
    if is_refusal(reply):
        return "refusal"
    if is_limit_reply(reply):
        return "usage-limit"
    if is_backend_error(reply):
        return "backend-error"
    return None
