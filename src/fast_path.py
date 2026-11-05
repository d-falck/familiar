"""Pick a reasoning effort from the shape of the latest turn.

Thinking effort is the other half of why short back-and-forths felt slow:
every turn ran at `xhigh`, so "hi" got the same minutes-long reasoning budget
as a finance reconciliation.

Downshifting is only safe when the turn plainly cannot need tools, lookups or
analysis, so this matches a *tight allowlist of conversational no-ops in full*
and returns the normal effort for anything else. The important negative case:
a short message is NOT the same as a trivial one. "yes", "ok", "go",
"go for it", "do it", "continue" are the shortest messages Damon sends and
they routinely authorise real work (a deploy, 100 archive calls, a Splitwise
write). Those must never be downshifted — hence full-string matching against
an explicit list rather than any length or "looks simple" heuristic.
"""

from __future__ import annotations

import os
import re

NORMAL_EFFORT = os.environ.get("AGENT_EFFORT", "xhigh")
FAST_EFFORT = os.environ.get("AGENT_FAST_EFFORT", "low")

# Strip the "Damon: " speaker prefix that load_as_messages adds, and any
# trailing punctuation/emoji-ish padding, before matching.
_SPEAKER_PREFIX = re.compile(r"^[^\n:]{1,40}:\s*")
_TRAILING = re.compile(r"[\s!?.,~)]+$")

_CHITCHAT = {
    # greetings
    "hi", "hey", "hello", "yo", "hiya", "hey iris", "hi iris", "hello iris",
    "good morning", "morning", "good evening", "evening",
    "good night", "goodnight", "night",
    # presence checks
    "you there", "are you there", "u there", "you there iris",
    "iris you there", "are you back", "you back", "you alive",
    "hey are you there", "hi are you there",
    # small talk
    "whats up", "what's up", "wassup", "sup", "how are you", "how are you doing",
    "hows it going", "how's it going", "how goes it",
    # acknowledgements — terminal, not instructions. Bare "ok"/"yes"/"sure"
    # are deliberately absent: they mean "go ahead" and start real work.
    "thanks", "thank you", "thanks iris", "thx", "ty", "cheers",
    "ok thanks", "okay thanks", "thanks!", "great thanks", "perfect thanks",
    "nice", "cool", "great", "awesome", "amazing", "perfect", "lovely",
    "lol", "haha", "lmao", "ha",
    # liveness tests
    "radio check", "ping", "test", "testing",
}


def normalise(text: str) -> str:
    cleaned = _SPEAKER_PREFIX.sub("", (text or "").strip())
    # Only ever consider the last line: a multi-line message is not chit-chat.
    if "\n" in cleaned:
        return ""
    return _TRAILING.sub("", cleaned).strip().lower()


def is_chitchat(messages: list[dict]) -> bool:
    """True when the latest user message is a conversational no-op."""
    if not messages:
        return False
    last = messages[-1]
    if last.get("role") != "user":
        return False
    return normalise(last.get("content") or "") in _CHITCHAT


def effort_for(messages: list[dict]) -> str:
    return FAST_EFFORT if is_chitchat(messages) else NORMAL_EFFORT
