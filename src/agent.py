"""Backend-agnostic entry point. Builds the system prompt once, then routes
the turn to the Claude or Codex backend based on `backend`.

Callers (Telegram handler, webhook, scheduler) only ever import `respond`
from here; the backend choice is a deploy-time config flag.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import claude_backend
import codex_backend
from prompt import build_system_prompt


async def respond(
    messages: list[dict],
    *,
    backend: str,
    model: str,
    mcp_servers: dict,
    memory_path: str,
    persona_path: str,
    history_path: str,
    self_repo_dir: str | None = None,
    self_deploy_cmd: str | None = None,
    workspace_chat_ids: list[int] | None = None,
    max_turns: int = 40,
    on_tool_use: Callable[[str, dict], Awaitable[None]] | None = None,
    on_text: Callable[[str], Awaitable[None]] | None = None,
    on_thinking: Callable[[str], Awaitable[None]] | None = None,
    on_tool_result: Callable[[str, str], Awaitable[None]] | None = None,
) -> str:
    system_prompt = build_system_prompt(
        persona_path=persona_path,
        memory_path=memory_path,
        history_path=history_path,
        self_repo_dir=self_repo_dir,
        self_deploy_cmd=self_deploy_cmd,
        workspace_chat_ids=workspace_chat_ids,
    )
    callbacks = dict(
        on_tool_use=on_tool_use,
        on_text=on_text,
        on_thinking=on_thinking,
        on_tool_result=on_tool_result,
    )
    if backend == "codex":
        return await codex_backend.run(
            messages,
            system_prompt=system_prompt,
            model=model,
            mcp_servers=mcp_servers,
            workdir=self_repo_dir,
            max_turns=max_turns,
            **callbacks,
        )
    return await claude_backend.run(
        messages,
        system_prompt=system_prompt,
        model=model,
        mcp_servers=mcp_servers,
        max_turns=max_turns,
        **callbacks,
    )
