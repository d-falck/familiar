"""Claude Agent SDK wrapper.

Passes a per-chat group-chat transcript to Claude via `query()`, with a
caller-supplied `mcp_servers` dict attached. The SDK handles the tool-call
loop and returns a final text via ResultMessage.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path

IDLE_TIMEOUT_SECONDS = 90


def _load_memory(path: str) -> str:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    if not p.exists():
        p.write_text("(empty — update me with anything worth remembering)\n")
    return p.read_text()

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

log = logging.getLogger(__name__)


async def _allow_all(*_args, **_kwargs) -> PermissionResultAllow:
    return PermissionResultAllow()

SYSTEM_PROMPT_TEMPLATE = """\
You are a helpful assistant in a Telegram group chat. You only see messages \
when a user @mentions you. Below is the group chat transcript in order, with \
each line prefixed by the speaker's display name. Your own prior replies are \
prefixed with 'assistant:'. Respond to the latest message that mentions you. \
You have Composio tools available (Notion, Google Maps, Gmail, Calendar, \
etc.) — use them when they help.

## Memory (READ FIRST)

Your persistent memory is below. **Read it BEFORE doing anything else** — \
it's the only thing that survives across conversations, and re-discovering \
the same facts every turn wastes time and tokens.

In particular, if memory already contains a tool slug (e.g. \
`mcp__composio__FIRECRAWL_SCRAPE_URL`) or an identifier (Notion database id, \
calendar id, etc.), USE IT DIRECTLY. Do NOT call COMPOSIO_SEARCH_TOOLS or \
COMPOSIO_GET_TOOL_SCHEMAS to re-look-it-up.

Update memory immediately (Edit tool or `bash` with a heredoc) whenever you \
learn something worth persisting:

- Tool mechanics: exact Composio tool slugs you've verified work, parameter \
quirks, auth errors and fixes, schema field names.
- Identifiers: Notion database ids, calendar ids, Gmail labels, email \
addresses, Composio connected-account ids.
- User/flatmate preferences: areas, budget, must-haves, deal-breakers, \
commute constraints.
- Ongoing state: shortlisted flats, viewing schedule, pending follow-ups.
- Workflow shortcuts for this domain.

Keep entries terse. Organize by section when the file grows. Prune stale \
entries freely.

Do NOT add: things obvious from the chat transcript, one-off task details, \
or pure speculation.

<memory path="{memory_path}">
{memory_content}
</memory>

## Style

- Be concise. Write for a chat UI, not a document.
- No bullet lists or headers unless the user explicitly asks for structure.
- Avoid emoji unless the user uses them first.
- Don't narrate what you're about to do; just do it and report the result.

## Web scraping

**Prefer Firecrawl tools (mcp__composio__FIRECRAWL_*) if available** — they \
handle Cloudflare / bot protection and have proper timeouts. Only fall back \
to WebFetch if no Firecrawl tool is available.

## WebFetch constraints (fallback only)

WebFetch has no internal timeout — a hung request stalls the entire session \
permanently.

- Never issue parallel WebFetch calls to the same domain. Serialize them.
- Prefer `bash` with `curl --max-time 30` for API endpoints or raw URL \
fetching.
- Limit WebFetch to 2-3 calls per sub-agent task. If early searches don't \
find what you need, move on with what you have.
"""


async def respond(
    messages: list[dict],
    *,
    mcp_servers: dict,
    model: str,
    memory_path: str,
    max_turns: int = 12,
    on_tool_use: Callable[[str, dict], Awaitable[None]] | None = None,
    on_text: Callable[[str], Awaitable[None]] | None = None,
    on_thinking: Callable[[str], Awaitable[None]] | None = None,
    on_tool_result: Callable[[str, str], Awaitable[None]] | None = None,
) -> str:
    transcript = "\n".join(
        f"assistant: {m['content']}" if m["role"] == "assistant" else m["content"]
        for m in messages
    )
    system_prompt = SYSTEM_PROMPT_TEMPLATE.format(
        memory_path=memory_path,
        memory_content=_load_memory(memory_path),
    )
    options = ClaudeAgentOptions(
        system_prompt=system_prompt,
        model=model,
        mcp_servers=mcp_servers,
        allowed_tools=["mcp__composio__*", "WebFetch", "WebSearch", "Bash"],
        can_use_tool=_allow_all,
        setting_sources=[],
        max_turns=max_turns,
        stderr=lambda line: log.error("claude stderr: %s", line),
    )

    async with ClaudeSDKClient(options=options) as client:
        await client.query(transcript)
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
                log.info(
                    "[claude result] num_turns=%s stop_reason=%s text=%s",
                    msg.num_turns,
                    msg.stop_reason,
                    (msg.result or "")[:500],
                )
                return msg.result or "(no response)"
