"""Claude Agent SDK wrapper.

Passes a per-chat group-chat transcript to Claude via `query()`, with a
caller-supplied `mcp_servers` dict attached. The SDK handles the tool-call
loop and returns a final text via ResultMessage.
"""

from __future__ import annotations

from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, query

SYSTEM_PROMPT = (
    "You are a helpful assistant in a Telegram group chat. You only see "
    "messages when a user @mentions you. Below is the group chat transcript "
    "in order, with each line prefixed by the speaker's display name. Your "
    "own prior replies are prefixed with 'assistant:'. Respond to the latest "
    "message that mentions you. You have Composio tools available (Notion, "
    "Google Maps, and others) — use them when they help. Keep replies "
    "concise enough to read in a chat UI."
)


async def respond(
    messages: list[dict],
    *,
    mcp_servers: dict,
    model: str,
    max_turns: int = 12,
) -> str:
    transcript = "\n".join(
        f"assistant: {m['content']}" if m["role"] == "assistant" else m["content"]
        for m in messages
    )
    options = ClaudeAgentOptions(
        system_prompt=SYSTEM_PROMPT,
        model=model,
        mcp_servers=mcp_servers,
        allowed_tools=["mcp__composio__*"],
        permission_mode="bypassPermissions",
        setting_sources=[],
        max_turns=max_turns,
    )

    async for msg in query(prompt=transcript, options=options):
        if isinstance(msg, ResultMessage):
            return msg.result
