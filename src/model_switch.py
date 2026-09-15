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
    "fable": ("claude", "claude-fable-5-1"),
    "opus": ("claude", "claude-opus-5"),
    "sonnet": ("claude", "claude-sonnet-5"),
    "haiku": ("claude", "claude-haiku-4-5-20251001"),
    "codex": ("codex", "gpt-5.6-sol"),
    "gpt": ("codex", "gpt-5.6-sol"),
}

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


# Order to fall back through when suggesting a way out of a usage limit: the
# most capable alternative first, ending with a different provider entirely.
_FALLBACK_ORDER = ("opus", "sonnet", "codex", "fable", "haiku")


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
    lines = [f"`/model {alias}` → `{model}`" for alias, (_, model) in ALIASES.items()]
    return (
        current
        + "\n\n**Switch to:**\n"
        + "\n".join(lines)
        + "\n\nAlso: `/model <backend> <model-id>` for anything not listed, "
        "and `/model reset` to go back to the default."
    )
