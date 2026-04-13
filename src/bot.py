"""Telegram long-polling bot that forwards @mentions to Claude + Composio MCP."""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

import telegramify_markdown
from aiohttp import web
from composio import Composio
from dotenv import load_dotenv
from telegram import MessageEntity, ReactionTypeEmoji, Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from claude_client import respond
from history import History
from webhook import build_app as build_webhook_app

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("bot")


def _describe_tool_input(tool_input: dict) -> str:
    """Pick a key field from a tool's input to show in the status line."""
    for key in ("url", "query", "q", "search", "title", "subject", "to", "name"):
        if key in tool_input and tool_input[key]:
            return str(tool_input[key])
    if tool_input:
        return str(next(iter(tool_input.values())))
    return ""


REACTION_RECEIVED = "👀"
REACTION_WORKING = "✍"
REACTION_THINKING = "🤔"
REACTION_ERROR = "💔"


async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    chat = update.effective_chat
    text = message.text or message.caption or ""
    user = message.from_user

    history: History = context.application.bot_data["history"]
    cfg: dict = context.application.bot_data["cfg"]
    attachments_dir: Path = context.application.bot_data["attachments_dir"]
    bot_username = context.bot.username

    # If the message includes an image, download it to the attachments
    # volume and append a path reference so Claude can Read it.
    if message.photo:
        attachments_dir.mkdir(parents=True, exist_ok=True)
        photo = message.photo[-1]  # highest-resolution variant
        file = await photo.get_file()
        path = attachments_dir / f"{chat.id}_{message.message_id}.jpg"
        await file.download_to_drive(path)
        text = (text + f"\n[attached image: {path}]").strip()

    if not text:
        return

    author = user.username or user.full_name
    history.add_user(chat.id, author, text)

    # DMs: respond to every message. Groups: only @-mentions or replies to bot.
    if chat.type == "private":
        should_respond = True
    else:
        replied = message.reply_to_message
        replied_to_bot = (
            replied
            and replied.from_user
            and replied.from_user.username == bot_username
        )
        needle = f"@{bot_username}".lower()
        should_respond = replied_to_bot or any(
            e.type == MessageEntity.MENTION
            and text[e.offset : e.offset + e.length].lower() == needle
            for e in (message.entities or message.caption_entities or [])
        )
    if not should_respond:
        return

    log.info("chat=%s %s triggered bot", chat.id, author)
    debug_chat_id = context.application.bot_data.get("debug_chat_id")
    stream_intermediate = context.application.bot_data.get("stream_intermediate_text", True)
    current_reaction = {"emoji": None}

    async def set_reaction(emoji: str | None) -> None:
        if emoji == current_reaction["emoji"]:
            return
        current_reaction["emoji"] = emoji
        try:
            await context.bot.set_message_reaction(
                chat_id=chat.id,
                message_id=message.message_id,
                reaction=[ReactionTypeEmoji(emoji=emoji)] if emoji else [],
            )
        except Exception:
            log.exception("failed to set reaction")

    async def send_debug(text: str) -> None:
        if not debug_chat_id or not text:
            return
        chunk_size = 4000
        for i in range(0, len(text), chunk_size):
            try:
                await context.bot.send_message(
                    chat_id=debug_chat_id,
                    text=text[i : i + chunk_size],
                    disable_notification=True,
                )
            except Exception:
                log.exception("failed to send debug message")
                return

    await set_reaction(REACTION_RECEIVED)
    last_streamed_text = {"value": None}

    async def send_to_main(text: str) -> None:
        try:
            await context.bot.send_message(
                chat_id=chat.id,
                text=telegramify_markdown.markdownify(text)[:4000],
                parse_mode=ParseMode.MARKDOWN_V2,
            )
        except Exception:
            log.exception("failed to send main-chat message")

    async def on_tool_use(tool_name: str, tool_input: dict) -> None:
        await set_reaction(REACTION_WORKING)
        pretty = tool_name.removeprefix("mcp__composio__").lower()
        arg = _describe_tool_input(tool_input)
        await send_debug(f"🔧 {pretty}{f': {arg}' if arg else ''}"[:500])

    async def on_text(text: str) -> None:
        if not text.strip():
            return
        await send_debug(f"💭 {text}")
        if stream_intermediate:
            last_streamed_text["value"] = text
            await send_to_main(text)

    async def on_thinking(text: str) -> None:
        await set_reaction(REACTION_THINKING)
        await send_debug(f"🧠 {text}")

    async def on_tool_result(tool_use_id: str, content: str) -> None:
        # tool results are noisy and rarely useful in the debug feed
        pass

    async def keep_typing():
        while True:
            await context.bot.send_chat_action(chat_id=chat.id, action="typing")
            await asyncio.sleep(4)

    typing = asyncio.create_task(keep_typing())
    error = None
    try:
        reply = await respond(
            history.load_as_messages(chat.id),
            on_tool_use=on_tool_use,
            on_text=on_text,
            on_thinking=on_thinking,
            on_tool_result=on_tool_result,
            **cfg,
        )
    except Exception as exc:
        log.exception("respond failed")
        error = exc
        reply = f"⚠️ {exc}"
    finally:
        typing.cancel()

    history.add_assistant(chat.id, reply)
    await set_reaction(REACTION_ERROR if error else None)
    await send_debug((f"⚠️ {error}" if error else f"✅ {reply}"))
    # If the final reply is identical to the last text block we already
    # streamed, skip it — no point duplicating the message.
    if error or reply != last_streamed_text["value"]:
        await message.reply_text(
            telegramify_markdown.markdownify(reply)[:4000],
            parse_mode=ParseMode.MARKDOWN_V2,
        )


async def _run() -> None:
    load_dotenv()

    session = Composio().create(user_id=os.environ["COMPOSIO_USER_ID"])
    mcp_servers = {
        "composio": {
            "type": session.mcp.type,
            "url": session.mcp.url,
            "headers": session.mcp.headers,
        }
    }

    respond_cfg = {
        "mcp_servers": mcp_servers,
        "model": os.environ.get("ANTHROPIC_MODEL", "claude-opus-4-6[1m]"),
        "max_turns": int(os.environ.get("MAX_AGENT_TURNS", "40")),
        "memory_path": os.environ.get("MEMORY_PATH", "./memory.md"),
        "persona_path": os.environ.get("PERSONA_PATH", "prompts/flat_hunt.md"),
    }
    history = History(os.environ.get("HISTORY_DB_PATH", "./history.sqlite"))

    app = Application.builder().token(os.environ["TELEGRAM_BOT_TOKEN"]).build()
    app.bot_data["cfg"] = respond_cfg
    app.bot_data["history"] = history
    app.bot_data["debug_chat_id"] = os.environ.get("DEBUG_CHAT_ID")
    app.bot_data["attachments_dir"] = Path(
        os.environ.get("ATTACHMENTS_DIR", "./attachments")
    )
    app.bot_data["stream_intermediate_text"] = (
        os.environ.get("STREAM_INTERMEDIATE_TEXT", "true").lower() != "false"
    )

    async def on_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat = update.effective_chat
        await context.bot.send_message(chat_id=chat.id, text=f"chat id: {chat.id}")

    app.add_handler(CommandHandler("id", on_id))
    app.add_handler(
        MessageHandler(
            (filters.ChatType.GROUPS | filters.ChatType.PRIVATE)
            & (filters.TEXT | filters.CAPTION | filters.PHOTO),
            on_message,
        )
    )

    await app.initialize()
    await app.start()
    await app.updater.start_polling(allowed_updates=Update.ALL_TYPES)
    log.info("telegram long-polling started")

    # HTTP server (always running so Fly's http_service has something to
    # route to — even if only /health). The /composio/webhook route is
    # only added when the trigger secret + target chat are configured.
    webhook_secret = os.environ.get("COMPOSIO_WEBHOOK_SECRET")
    trigger_chat_raw = os.environ.get("TRIGGER_CHAT_ID")
    aiohttp_app = build_webhook_app(
        secret=webhook_secret,
        target_chat_id=int(trigger_chat_raw) if trigger_chat_raw else None,
        telegram_bot=app.bot,
        history=history,
        respond_fn=respond,
        respond_cfg=respond_cfg,
    )
    runner = web.AppRunner(aiohttp_app)
    await runner.setup()
    port = int(os.environ.get("PORT", "8080"))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    if webhook_secret and trigger_chat_raw:
        log.info("composio webhook listening on 0.0.0.0:%d", port)
    else:
        log.info("http /health listening on 0.0.0.0:%d (webhook disabled)", port)

    try:
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()
        await app.updater.stop()
        await app.stop()
        await app.shutdown()


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
