# familiar

Telegram group-chat bot that forwards @mentions to Claude via the Claude Agent SDK, with Notion + Google Maps (and everything else) via a Composio MCP server.

## Roadmap

- [ ] **Composio triggers for reactive behaviour.** Run a small aiohttp server alongside long-polling, expose a public webhook on Fly, and let Composio POST incoming events (new Gmail, calendar invites, etc.) so the bot can act on them without being mentioned. Needs signature verification, a configurable default reply chat, and some debounce to avoid runaway cost on email-heavy periods.
- [ ] **Approval flow for irreversible actions.** Right now `can_use_tool` auto-approves everything. Before sending email, creating/modifying calendar events, or editing Notion pages, the bot should post a compact preview to the chat and wait for a ✅ reaction (or `/approve`) before executing. Denials should propagate back to Claude as a tool result.
- [ ] **Multi-account Composio support.** Today each familiar gets one `COMPOSIO_USER_ID` and all tool calls act as that identity. Need to let a single agent hold multiple connected accounts simultaneously — e.g. Iris sends email from her own Gmail but reads from my personal inbox, or reads Damon's and the flatmate's calendars to find joint viewing slots. Probably a map of role → user_id and a way for Claude to pick the right identity per tool call.
- [ ] **Persist intermediate thinking + tool use across turns.** Right now each `respond()` call is a fresh `ClaudeSDKClient` session — only final user/assistant text survives via SQLite. Tool calls and thinking blocks from the previous turn are gone by the next mention. Store raw assistant-turn blocks and replay them (or use `ClaudeSDKClient.resume(session_id)` with a per-chat persisted session id) so Claude "remembers" what it tried last time within the same conversation.
- [ ] **Self-managed triggers and schedules.** Once Composio triggers infrastructure is live, let the bot create / list / delete its own triggers via Composio MCP tools (or a small custom tool wrapping the composio SDK) and record them in memory. User says "every Monday at 9am check my inbox for viewing confirmations"; bot wires up the trigger itself and maintains it over time. Blocks on the triggers item above; also probably wants the approval flow so it can't trigger-spam silently.

## Architecture

- **bot.py** — python-telegram-bot long-polling, listens in groups, filters to messages that @mention or reply to the bot. Persists every group message to SQLite.
- **history.py** — per-chat SQLite message log. Replays the full conversation as a transcript on each turn.
- **claude_client.py** — wraps `claude_agent_sdk.query()` with one HTTP MCP server pointing at Composio. Agent SDK handles the tool-call loop.

## Env vars

| Name | Required | Notes |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | yes | from @BotFather |
| `ANTHROPIC_API_KEY` | either | pay-as-you-go API credits (`sk-ant-api...`). Set this OR `CLAUDE_CODE_OAUTH_TOKEN`. |
| `CLAUDE_CODE_OAUTH_TOKEN` | either | long-lived Claude Max/Pro subscription token (`sk-ant-oat...`). Generate once with `claude setup-token` on any authed machine, paste the output. Takes precedence if both are set. No file or refresh management. |
| `COMPOSIO_API_KEY` | yes | read by the `composio` SDK at startup |
| `COMPOSIO_USER_ID` | yes | your Composio user id (e.g. `user_7svs9s`) — the bot creates a Tool Router session for this user at startup, which exposes all your connected toolkits |
| `AGENT_BACKEND` | no | `claude` (default, Claude Agent SDK) or `codex` (OpenAI Codex CLI). Both share the same Composio MCP server and system prompt. |
| `ANTHROPIC_MODEL` | no | default `claude-fable-5-1` (used when backend is `claude`) |
| `CODEX_MODEL` | no | default `gpt-5.6-sol` (used when backend is `codex`; reuses `OPENAI_API_KEY`) |
| `HISTORY_DB_PATH` | no | default `./history.sqlite`; in Docker/Fly, `/data/history.sqlite` |
| `MAX_AGENT_TURNS` | no | default `12` |

## Running locally

```bash
cp .env.example .env     # fill in values
uv sync
uv run python bot.py
```

## ElevenLabs Iris agent-as-code

The voice-call Iris agent should be managed with ElevenLabs' official CLI
agent-as-code workflow, not a custom REST sync script.

Install and authenticate the CLI:

```bash
npm install -g @elevenlabs/cli
npm run elevenlabs:login
```

The first import must be read-only from the live ElevenLabs agent:

```bash
npm run elevenlabs:init
npm run elevenlabs:pull
```

That creates the official repo-managed structure:

- `agents.json` — central agent registry.
- `tools.json` — tool registry.
- `tests.json` — test registry.
- `agent_configs/` — per-agent JSON configs.
- `tool_configs/` — per-tool JSON configs.
- `test_configs/` — per-test JSON configs.

After pulling, inspect and commit the generated baseline before making edits.
Do not run a push against an unreviewed or hand-recreated config.

For future changes:

```bash
npm run elevenlabs:pull:update      # refresh local config from dashboard/API changes
git diff                           # inspect what changed
npm run elevenlabs:push:dry-run     # preview platform changes
npm run elevenlabs:push             # apply after review
```

The voice dispatch endpoint used by Iris-owned webhook tools is:

```bash
IRIS_VOICE_DISPATCH_URL=https://iris-familiar.fly.dev/voice/dispatch
VOICE_DISPATCH_SECRET=...
```

### Shared context (voice ⇄ text)

The phone agent gets the **same brain as the text agent** via a
conversation-initiation webhook on the Fly server:

```
POST https://iris-familiar.fly.dev/voice/init
header: x-dispatch-secret: <VOICE_INIT_SECRET, defaults to VOICE_DISPATCH_SECRET>
```

ElevenLabs calls this at the start of every call. The server reads the live
`/data/memory.md` plus the tail of the main DM history (keyed off
`TRIGGER_CHAT_ID`) and returns:

- `dynamic_variables`: `{ memory, recent_context, today }` — substituted into
  any `{{memory}}` / `{{recent_context}}` / `{{today}}` placeholders in the
  ElevenLabs-side prompt; **and**
- `conversation_config_override.agent.prompt.prompt`: the full phone system
  prompt (voice persona + memory + recent history), built by
  `build_voice_prompt()` and used verbatim **if** prompt-override is enabled
  in the agent's Security settings.

Both are returned so either wiring works. To finish wiring on the ElevenLabs
side (dashboard or agent-as-code), pick one:

1. **Override (simplest, recommended):** in the agent's *Security* tab enable
   "Overrides → System prompt", then set the agent's conversation-initiation
   webhook to `/voice/init`. The server-built prompt replaces the dashboard
   prompt each call — fully managed here.
2. **Dynamic variables:** keep the persona in the dashboard prompt and add
   `{{memory}}` and `{{recent_context}}` placeholders where you want the
   context injected; the webhook fills them. No override permission needed.

Because the context is fetched fresh per call, memory/history edits made by
the text agent show up on the very next call — no re-provisioning.

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
  COMPOSIO_API_KEY=... \
  COMPOSIO_USER_ID=...
# Pick ONE of the following for Claude auth:
fly secrets set ANTHROPIC_API_KEY=sk-ant-api...
#   — OR —
fly secrets set CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat...   # generated via `claude setup-token` on any authed machine
fly deploy
```

Long-polling means no public ports — Fly will run the machine without HTTP services.

## Notes

- **History model**: every group message is persisted, even ones that don't mention the bot. When the bot is mentioned, the full chat is replayed as a transcript prompt. With the 1M-context Opus model, compaction isn't needed for v1. If/when it is, add the `compact-2026-01-12` beta.
- **Composio identity**: a single `user_id` is baked into the Composio MCP URL at creation time; every tool call acts as that user. Pre-authorize Notion + Maps in the Composio dashboard once.
- **Tool permissions**: the agent is started with `permission_mode="bypassPermissions"` and `allowed_tools=["mcp__composio__*"]`, so only Composio-exposed tools are callable — no Bash/Read/Write.

## Read-only finance connector

`finance_connector/` is a deliberately separate service for financial
planning data. It retains no database and never logs response bodies. Its
security boundary is code, not a prompt:

- every externally exposed route except `/health` requires a bearer secret;
- every route is GET-only;
- Trading 212 endpoints use an explicit read-resource allowlist;
- Monzo/Amex use only UK Open Banking account-information resource paths;
- payment initiation, withdrawals, orders, portfolio mutation, and account
  management are absent and rejected.

Run tests locally:

```bash
uv run python -m unittest finance_connector.test_app
```

Deploy it as a separate Fly app using `fly.finance.toml.example`; do not add
the credentials to the main Iris app. Set secrets out-of-band with `flyctl
secrets set`—never paste them into Telegram or store them in memory/history.

Trading 212 can be connected directly with a key generated with only account,
history, orders-read, and portfolio/positions-read permissions. Do not grant
order placement. Monzo's production Open Banking interface requires a
licensed Account Information Service Provider; Amex should be connected
through the same provider. Put that provider's per-institution base URL and
access token in `MONZO_OB_*` / `AMEX_OB_*`.

Emma exports are supported as an alternative read-only Monzo/Amex source. Put
the XLSX on the connector's private volume and set `EMMA_EXPORT_XLSX_PATH` to
its absolute path. `GET /v1/emma/transactions` accepts optional `from`, `to`,
`bank`, and `account` filters. The service reads the file on demand and never
copies transaction rows into the repository or a database.
