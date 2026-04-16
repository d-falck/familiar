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


def _load_persona(path: str) -> str:
    return Path(path).read_text().strip()

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
{persona}

You live in a Telegram chat. The user message below contains the chat \
transcript wrapped in <transcript> tags, with each line prefixed by the \
speaker's display name. Your own prior replies are prefixed with \
'assistant:'. Respond to the latest message addressed to you. You have \
Composio tools available (Notion, Google Maps, Gmail, Calendar, etc.) — use \
them when they help.

**IMPORTANT**: the <transcript> format is for INPUT ONLY. Your response must \
be just the reply text itself — do NOT prefix it with 'assistant:', do NOT \
include any '<name>:' lines, and do NOT generate imagined next turns from \
the user. Write one message, then stop.

## Memory (READ FIRST)

Your persistent memory is below. **Read it BEFORE doing anything else** — \
it's the only thing that survives across conversations. Treat it as your \
long-term brain: facts, identifiers, workflow shortcuts, standing \
instructions, user preferences, things you're supposed to do recurringly, \
and anything else useful that should outlast a single chat.

If memory already tells you *how* to do something (a tool slug that \
reliably works, a specific workflow, a user's preferred tone), USE IT \
DIRECTLY instead of re-discovering or re-deciding.

Update memory immediately (Edit tool or `bash` with a heredoc) when you \
learn or decide something worth persisting. Examples of useful entries \
(not an exhaustive list):

- Tool/workflow knowledge you figured out the hard way.
- Stable identifiers (Notion db ids, calendar ids, email addresses, etc).
- Standing instructions from the user ("always draft replies to Mum \
warmly", "never book Sunday mornings", "on Fridays remind me to review \
the week").
- User preferences, habits, constraints.
- Ongoing state: shortlists, pending commitments, in-flight threads.

Keep entries terse. Organize by section when the file grows. Reorganise and \
prune freely. If you want to restructure the whole file to make it more \
useful to future-you, go ahead.

Don't bother persisting things obvious from the chat transcript, one-off \
task details, or pure speculation.

<memory path="{memory_path}">
{memory_content}
</memory>

## Cross-chat history

The <transcript> above is only the *current* chat. All your conversations \
(DMs + groups) live in a single SQLite file at `{history_path}`, schema \
`messages(id, chat_id, role, author, content, created_at)`. When a user \
references something said in another chat, or you need cross-chat \
context, query it via Bash — e.g. \
`sqlite3 {history_path} "SELECT chat_id, author, substr(content,1,200) \
FROM messages WHERE content LIKE '%keyword%' ORDER BY id DESC LIMIT 20"`. \
Persist any stable chat_id → purpose mapping in memory so you don't have \
to rediscover it.

## Style

- Be concise. Write for a chat UI, not a document.
- No bullet lists or headers unless the user explicitly asks for structure.
- Avoid emoji unless the user uses them first.
- Don't narrate what you're about to do; just do it and report the result.

## Composio triggers

You can manage your own Composio triggers via Bash — the `composio` SDK is \
installed and `COMPOSIO_API_KEY` is in the env. Key methods on \
`Composio().triggers`: `list(toolkit_slugs=[...])` to browse trigger types, \
`get_type(slug)` for config schema, `create(slug, user_id, trigger_config)` \
to instantiate, `list_active()` / `disable(id)` / `enable(id)` / \
`delete(id)` to manage. When a trigger fires, the event arrives as a user \
message prefixed `A Composio trigger fired:` via the webhook (already \
wired up project-wide in Composio).

Always `list_active()` before creating to avoid duplicates. Record every \
trigger id + purpose in memory under "Active triggers" so cleanup is \
possible later. Confirm with the user before creating — triggers cost a \
Claude run per event, so use tight filters.

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
    persona_path: str,
    history_path: str,
    max_turns: int = 12,
    on_tool_use: Callable[[str, dict], Awaitable[None]] | None = None,
    on_text: Callable[[str], Awaitable[None]] | None = None,
    on_thinking: Callable[[str], Awaitable[None]] | None = None,
    on_tool_result: Callable[[str, str], Awaitable[None]] | None = None,
) -> str:
    transcript_body = "\n".join(
        f"assistant: {m['content']}" if m["role"] == "assistant" else m["content"]
        for m in messages
    )
    transcript = f"<transcript>\n{transcript_body}\n</transcript>"
    system_prompt = SYSTEM_PROMPT_TEMPLATE.format(
        persona=_load_persona(persona_path),
        memory_path=memory_path,
        memory_content=_load_memory(memory_path),
        history_path=history_path,
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
