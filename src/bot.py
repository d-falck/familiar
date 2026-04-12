"""Telegram long-polling bot that forwards @mentions to Claude + Composio MCP."""

from __future__ import annotations

import logging
import os

from composio import Composio
from dotenv import load_dotenv
from telegram import MessageEntity, Update
from telegram.ext import Application, ContextTypes, MessageHandler, filters

from claude_client import respond
from history import History

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("bot")


async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    chat = update.effective_chat
    text = message.text or message.caption
    user = message.from_user

    history: History = context.application.bot_data["history"]
    cfg: dict = context.application.bot_data["cfg"]
    bot_username = context.bot.username

    author = user.username or user.full_name
    history.add_user(chat.id, author, text)

    # Only respond when the bot is @-mentioned or directly replied to.
    replied = message.reply_to_message
    replied_to_bot = replied and replied.from_user and replied.from_user.username == bot_username
    needle = f"@{bot_username}".lower()
    mentioned = replied_to_bot or any(
        e.type == MessageEntity.MENTION
        and text[e.offset : e.offset + e.length].lower() == needle
        for e in (message.entities or message.caption_entities or [])
    )
    if not mentioned:
        return

    log.info("chat=%s @%s mentioned bot", chat.id, author)
    await context.bot.send_chat_action(chat_id=chat.id, action="typing")
    reply = await respond(history.load_as_messages(chat.id), **cfg)
    history.add_assistant(chat.id, reply)
    await message.reply_text(reply[:4000])


def main() -> None:
    load_dotenv()

    session = Composio().create(user_id=os.environ["COMPOSIO_USER_ID"])
    mcp_servers = {
        "composio": {
            "type": session.mcp.type,
            "url": session.mcp.url,
            "headers": session.mcp.headers,
        }
    }

    app = Application.builder().token(os.environ["TELEGRAM_BOT_TOKEN"]).build()
    app.bot_data["cfg"] = {
        "mcp_servers": mcp_servers,
        "model": os.environ.get("ANTHROPIC_MODEL", "claude-opus-4-6[1m]"),
        "max_turns": int(os.environ.get("MAX_AGENT_TURNS", "12")),
    }
    app.bot_data["history"] = History(
        os.environ.get("HISTORY_DB_PATH", "./history.sqlite")
    )

    app.add_handler(
        MessageHandler(
            filters.ChatType.GROUPS & (filters.TEXT | filters.CAPTION),
            on_message,
        )
    )

    log.info("starting long-polling")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
