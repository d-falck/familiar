"""Claude backend: runs a turn via the Claude Agent SDK + bundled CLI.

Streams the per-chat transcript to Claude with a caller-supplied
`mcp_servers` dict attached; the SDK handles the tool-call loop and returns
a final text via ResultMessage.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    PermissionResultAllow,
    ResultMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from prompt import render_transcript

log = logging.getLogger(__name__)

# Watchdog: abort a turn only after this long with NO stream activity at all.
# It must clear the longest *legitimate* silent gap. Partial messages (enabled
# below) keep it fed through long reasoning, so this only needs to cover a
# single long tool call that emits nothing until it returns — a multi-minute
# `flyctl deploy`, browser automation, or deep web research. 90s killed those
# healthy turns ("produced no output for 90s — aborting"); 300s does not, while
# still reaping a genuinely stuck subprocess. Override via env if needed.
IDLE_TIMEOUT_SECONDS = int(os.environ.get("AGENT_IDLE_TIMEOUT_SECONDS", "300"))


async def _allow_all(*_args, **_kwargs) -> PermissionResultAllow:
    return PermissionResultAllow()


async def run(
    messages: list[dict],
    *,
    system_prompt: str,
    model: str,
    mcp_servers: dict,
    max_turns: int = 40,
    on_tool_use: Callable[[str, dict], Awaitable[None]] | None = None,
    on_text: Callable[[str], Awaitable[None]] | None = None,
    on_thinking: Callable[[str], Awaitable[None]] | None = None,
    on_tool_result: Callable[[str, str], Awaitable[None]] | None = None,
) -> str:
    options = ClaudeAgentOptions(
        system_prompt=system_prompt,
        model=model,
        mcp_servers=mcp_servers,
        allowed_tools=["mcp__composio__*", "WebFetch", "WebSearch", "Bash"],
        can_use_tool=_allow_all,
        setting_sources=[],
        max_turns=max_turns,
        # Opus 4.7+ rejects the legacy `enabled` thinking config; it requires
        # `adaptive` + a top-level `effort` knob.
        thinking={"type": "adaptive"},
        effort="xhigh",
        # Drip StreamEvent heartbeats during long reasoning/generation so the
        # idle watchdog isn't tripped mid-thought (xhigh effort can spend
        # minutes on a single block before it's delivered as a complete
        # message). StreamEvents aren't AssistantMessages, so the receive loop
        # below ignores them as pure heartbeats — no duplicate streamed text.
        include_partial_messages=True,
        stderr=lambda line: log.error("claude stderr: %s", line),
    )

    async with ClaudeSDKClient(options=options) as client:
        await client.query(render_transcript(messages))
        stream = client.receive_response().__aiter__()
        while True:
            try:
                msg = await asyncio.wait_for(
                    stream.__anext__(), timeout=IDLE_TIMEOUT_SECONDS
                )
            except StopAsyncIteration:
                break
            except asyncio.TimeoutError:
                raise RuntimeError(
                    f"claude produced no output for {IDLE_TIMEOUT_SECONDS}s — aborting"
                )
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        log.info("[claude text] %s", block.text)
                        if on_text:
                            await on_text(block.text)
                    elif isinstance(block, ThinkingBlock):
                        log.info("[claude thinking] %s", block.thinking)
                        if on_thinking:
                            await on_thinking(block.thinking)
                    elif isinstance(block, ToolUseBlock):
                        log.info("[claude tool_use] %s input=%s", block.name, block.input)
                        if on_tool_use:
                            await on_tool_use(block.name, dict(block.input or {}))
            elif isinstance(msg, UserMessage):
                for block in msg.content if isinstance(msg.content, list) else []:
                    if isinstance(block, ToolResultBlock):
                        content_str = str(block.content)
                        log.info("[claude tool_result] %s: %s", block.tool_use_id, content_str[:500])
                        if on_tool_result:
                            await on_tool_result(block.tool_use_id, content_str)
            elif isinstance(msg, ResultMessage):
                # subtype/is_error distinguish a genuine reply from an
                # SDK-level abort (including a usage-policy block, which comes
                # back as an error result rather than text) — without them a
                # refusal is indistinguishable from a quiet turn in the logs.
                log.info(
                    "[claude result] num_turns=%s stop_reason=%s subtype=%s "
                    "is_error=%s text=%s",
                    msg.num_turns,
                    msg.stop_reason,
                    getattr(msg, "subtype", None),
                    getattr(msg, "is_error", None),
                    (msg.result or "")[:500],
                )
                return msg.result or "(no response)"
    return "(no response)"
