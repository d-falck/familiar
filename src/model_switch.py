"""Runtime model/provider switching, with no model in the loop.

Hitting a provider usage limit ("You've reached your Fable limit") is exactly
the moment when asking the agent to switch itself is impossible: the agent is
the thing that's unavailable. So the switch lives in a plain Telegram command
handler — pure Python, no LLM turn, no tool call — that rewrites the shared
``respond_cfg`` dict in place. Every caller (Telegram handler, scheduler,
webhook) holds a reference to that same dict, so the change takes effect on
the next turn everywhere at once.

The choice is persisted next to the history DB so it survives a restart or a
redeploy; ``/model reset`` drops it and falls back to the env defaults
(``AGENT_BACKEND`` + ``ANTHROPIC_MODEL``/``CODEX_MODEL``).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

log = logging.getLogger("model_switch")

# Short aliases for the models worth switching between in a hurry. A raw model
# id is always accepted too, so this list going stale can never block a switch.
ALIASES: dict[str, tuple[str, str]] = {
    # Family aliases point at the newest of each line, so `/model opus` keeps
    # meaning "the current Opus" as the lineup moves on.
    "fable": ("claude", "claude-fable-5-1"),
    "opus": ("claude", "claude-opus-5-5"),
    "sonnet": ("claude", "claude-sonnet-5-5"),
    # Never date-suffix a current model id: the bare id is complete, and the
    # dated variant is the thing that breaks first when a snapshot retires.
    "haiku": ("claude", "claude-haiku-5-5"),
    # Version-pinned, so "the newest Opus" and "Opus 5 specifically" can't
    # drift apart, and so stepping back a version during an outage or a quota
    # limit is one word instead of a remembered model id.
    "opus5.5": ("claude", "claude-opus-5-5"),
    "opus5": ("claude", "claude-opus-5"),
    "opus4.8": ("claude", "claude-opus-4-8"),
    "opus4.7": ("claude", "claude-opus-4-7"),
    "fable5.1": ("claude", "claude-fable-5-1"),
    "fable5": ("claude", "claude-fable-5"),
    "mythos": ("claude", "claude-mythos-5-1"),
    "sonnet5.5": ("claude", "claude-sonnet-5-5"),
    "sonnet5": ("claude", "claude-sonnet-5"),
    "sonnet4.6": ("claude", "claude-sonnet-4-6"),
    "haiku5.5": ("claude", "claude-haiku-5-5"),
    "haiku4.5": ("claude", "claude-haiku-4-5"),
    "codex": ("codex", "gpt-5.6-sol"),
    "gpt": ("codex", "gpt-5.6-sol"),
}

# Shown next to each alias by `/model`, so the choice is informed rather than a
# list of bare ids. Price is the thing actually worth knowing when switching
# under a quota limit, so lead with it ($ per million input/output tokens).
# Keep terse — this renders in a chat message.
NOTES: dict[str, str] = {
    "fable": "$10/$50 — top reasoning + long-horizon agentic, own quota pool",
    "opus": "deploy default — $4/$20, long agentic coding and knowledge work",
    "sonnet": "$2/$10 — best speed/intelligence mix, fine for routine turns",
    "haiku": "$0.10/$0.50 — cheapest by far, simple high-volume tasks",
    "opus5": "previous Opus — $5/$25, so pricier *and* weaker than 5.5",
    "opus4.8": "two Opus versions back",
    "fable5": "previous Fable",
    "mythos": "gated access — may fail",
    "sonnet5": "previous Sonnet — same $2/$10",
    "haiku4.5": "previous Haiku — $1/$5, and 200K context (rest are 1M)",
    "codex": "different provider entirely (GPT-5.6 Sol)",
}

# The shortlist `/model` puts in front of a human; the rest stay reachable by
# name without cluttering the message. Exact-synonym aliases (`opus5.5`,
# `fable5.1`, `sonnet5.5`, `haiku5.5`, `gpt`) are deliberately omitted from
# both.
PRIMARY_ALIASES = ("opus", "fable", "sonnet", "haiku", "codex")
OLDER_ALIASES = ("opus5", "opus4.8", "fable5", "sonnet5", "haiku4.5", "mythos")

BACKENDS = ("claude", "codex")

RESET_WORDS = ("reset", "default", "clear", "off", "-")

# Raw model ids starting with one of these belong to the Codex backend;
# anything else is assumed to be a Claude model id.
_CODEX_PREFIXES = ("gpt", "o1", "o3", "o4", "codex")


def infer_backend(model: str) -> str:
    return "codex" if model.lower().startswith(_CODEX_PREFIXES) else "claude"


def resolve(args: list[str]) -> tuple[str, str]:
    """Turn ``/model`` arguments into a ``(backend, model)`` pair.

    Accepts an alias (``opus``), a raw model id (``claude-opus-4-5``, backend
    inferred from the prefix), or an explicit ``<backend> <model>`` pair.
    Raises ValueError with a user-facing message on anything else.
    """
    if not args:
        raise ValueError("no model given")
    if len(args) > 2:
        raise ValueError("too many arguments — use `/model <name>`")

    if len(args) == 2:
        backend, model = args[0].lower(), args[1]
        if backend not in BACKENDS:
            raise ValueError(
                f"unknown backend `{backend}` — expected one of "
                + ", ".join(f"`{b}`" for b in BACKENDS)
            )
        return backend, model

    token = args[0].lower()
    if token in ALIASES:
        return ALIASES[token]
    if token in BACKENDS:
        raise ValueError(
            f"`{token}` is a backend, not a model — use "
            f"`/model {token} <model-id>` or an alias like `/model opus`"
        )
    # A bare word with no hyphen is far more likely to be a typo'd alias than a
    # real model id, and silently accepting it would wedge every later turn.
    if "-" not in token and "." not in token:
        raise ValueError(
            f"unknown model `{args[0]}` — try one of "
            + ", ".join(f"`{a}`" for a in ALIASES)
            + ", or pass a full model id"
        )
    return infer_backend(args[0]), args[0]


# Order to fall back through when suggesting a way out of a usage limit. Opus
# and Fable first because they draw on separate quota pools, so they rescue
# each other; then cheaper models, then a different provider entirely.
_FALLBACK_ORDER = ("opus", "fable", "sonnet", "codex", "haiku")


def suggest_alternative(current_model: str | None) -> str:
    """An alias worth switching TO, given what's currently running. Never
    suggests the model that just hit a limit."""
    for alias in _FALLBACK_ORDER:
        if ALIASES[alias][1] != current_model:
            return alias
    return "opus"


def override_path(history_path: str) -> Path:
    """Where the persisted choice lives: beside the history DB, which is on
    the Fly volume in prod, so it survives restarts and redeploys."""
    return Path(history_path).with_name("model_override.json")


def load_override(path: Path) -> tuple[str, str] | None:
    """Read the persisted choice. Best effort: a missing or corrupt file just
    means 'no override' rather than a boot failure."""
    try:
        data = json.loads(path.read_text())
        backend, model = data["backend"], data["model"]
    except FileNotFoundError:
        return None
    except Exception:
        log.exception("ignoring unreadable model override at %s", path)
        return None
    if backend not in BACKENDS or not isinstance(model, str) or not model:
        log.warning("ignoring invalid model override: %r", data)
        return None
    return backend, model


def save_override(path: Path, backend: str, model: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"backend": backend, "model": model}))


def clear_override(path: Path) -> None:
    path.unlink(missing_ok=True)


def format_status(cfg: dict, defaults: tuple[str, str]) -> str:
    """The `/model` (no args) reply: what's running now, what's on offer."""
    default_backend, default_model = defaults
    current = f"**Current:** `{cfg['model']}` (backend `{cfg['backend']}`)"
    if (cfg["backend"], cfg["model"]) == (default_backend, default_model):
        current += "\n_This is the deploy default._"
    else:
        current += f"\n_Overridden; default is_ `{default_model}`."

    def line(alias: str) -> str:
        model = ALIASES[alias][1]
        note = NOTES.get(alias)
        marker = " ← running" if model == cfg.get("model") else ""
        return f"`/model {alias}` → `{model}`" + (f" — {note}" if note else "") + marker

    return (
        current
        + "\n\n**Switch to:**\n"
        + "\n".join(line(a) for a in PRIMARY_ALIASES)
        + "\n\n**Step back a version:**\n"
        + "\n".join(line(a) for a in OLDER_ALIASES)
        + "\n\nAlso: `/model <backend> <model-id>` for anything not listed, "
        "and `/model reset` to go back to the default."
    )
