# familiar

Telegram group-chat bot that forwards @mentions to Claude via the Claude Agent SDK, with Notion + Google Maps (and everything else) via a Composio MCP server.

## Roadmap

- [ ] **Composio triggers for reactive behaviour.** Run a small aiohttp server alongside long-polling, expose a public webhook on Fly, and let Composio POST incoming events (new Gmail, calendar invites, etc.) so the bot can act on them without being mentioned. Needs signature verification, a configurable default reply chat, and some debounce to avoid runaway cost on email-heavy periods.
- [ ] **Approval flow for irreversible actions.** Right now `can_use_tool` auto-approves everything. Before sending email, creating/modifying calendar events, or editing Notion pages, the bot should post a compact preview to the chat and wait for a ✅ reaction (or `/approve`) before executing. Denials should propagate back to Claude as a tool result.
- [ ] **Multi-account Composio support.** Today each familiar gets one `COMPOSIO_USER_ID` and all tool calls act as that identity. Need to let a single agent hold multiple connected accounts simultaneously — e.g. Iris sends email from her own Gmail but reads from my personal inbox, or reads Damon's and the flatmate's calendars to find joint viewing slots. Probably a map of role → user_id and a way for Claude to pick the right identity per tool call.

## Architecture

- **bot.py** — python-telegram-bot long-polling, listens in groups, filters to messages that @mention or reply to the bot. Persists every group message to SQLite.
- **history.py** — per-chat SQLite message log. Replays the full conversation as a transcript on each turn.
- **claude_client.py** — wraps `claude_agent_sdk.query()` with one HTTP MCP server pointing at Composio. Agent SDK handles the tool-call loop.

## Env vars

| Name | Required | Notes |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | yes | from @BotFather |
| `ANTHROPIC_API_KEY` | yes | consumed by the Claude Code CLI under the hood |
| `COMPOSIO_API_KEY` | yes | read by the `composio` SDK at startup |
| `COMPOSIO_USER_ID` | yes | your Composio user id (e.g. `user_7svs9s`) — the bot creates a Tool Router session for this user at startup, which exposes all your connected toolkits |
| `ANTHROPIC_MODEL` | no | default `claude-opus-4-6[1m]` |
| `HISTORY_DB_PATH` | no | default `./history.sqlite`; in Docker/Fly, `/data/history.sqlite` |
| `MAX_AGENT_TURNS` | no | default `12` |

## Running locally

```bash
cp .env.example .env     # fill in values
uv sync
uv run python bot.py
```

The Claude Agent SDK spawns the `claude` CLI as a subprocess, so you need Claude Code installed locally:

```bash
curl -fsSL https://claude.ai/install.sh | bash
```

## Telegram setup

1. Create a bot with @BotFather and copy the token into `TELEGRAM_BOT_TOKEN`.
2. Disable "group privacy" for the bot via @BotFather → Bot Settings → Group Privacy → Turn off. Otherwise it only sees commands, not plain @mentions.
3. Add the bot to your group.
4. Mention it: `@your_bot_name what's on my Notion today?`

## Deploying to Fly.io

```bash
fly launch --no-deploy         # accept fly.toml
fly volumes create bot_data --size 1 --region iad
fly secrets set \
  TELEGRAM_BOT_TOKEN=... \
  ANTHROPIC_API_KEY=... \
  COMPOSIO_API_KEY=... \
  COMPOSIO_MCP_URL=...
fly deploy
```

Long-polling means no public ports — Fly will run the machine without HTTP services.

## Notes

- **History model**: every group message is persisted, even ones that don't mention the bot. When the bot is mentioned, the full chat is replayed as a transcript prompt. With the 1M-context Opus model, compaction isn't needed for v1. If/when it is, add the `compact-2026-01-12` beta.
- **Composio identity**: a single `user_id` is baked into the Composio MCP URL at creation time; every tool call acts as that user. Pre-authorize Notion + Maps in the Composio dashboard once.
- **Tool permissions**: the agent is started with `permission_mode="bypassPermissions"` and `allowed_tools=["mcp__composio__*"]`, so only Composio-exposed tools are callable — no Bash/Read/Write.
