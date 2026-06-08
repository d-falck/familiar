"""Shared Telegram MarkdownV2 sender with a safe plain-text fallback.

Used by every outbound path (the Telegram handler's replies, the scheduler's
proactive nudges, and the Composio/voice webhook posts) so they all render
markdown consistently and never silently drop a message.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

import telegramify_markdown
from telegram.constants import ParseMode
from telegram.error import BadRequest

log = logging.getLogger("telegram_md")

# A ``send`` callable takes ``(body, parse_mode_or_None)`` and returns an
# awaitable. This lets the same logic serve send_message and reply_text.
Send = Callable[[str, object], Awaitable[object]]


async def send_markdown(send: Send, text: str) -> None:
    """Deliver ``text`` to Telegram as MarkdownV2, falling back to plain text
    if the converted markup is rejected. MarkdownV2 is finicky — one stray
    entity makes Telegram reject the whole message — so a bad conversion must
    never mean a missing or raw-looking message. The raw text is truncated
    *before* markdownify so we never slice through an escape and corrupt the
    markup.
    """
    raw = text[:3900]
    md = None
    try:
        # telegramify renders unordered lists with a ⦁ (Z-NOTATION SPOT) glyph
        # that looks broken in Telegram; swap it for a clean bullet.
        md = telegramify_markdown.markdownify(raw).replace("⦁", "•")[:4096]
    except Exception:
        log.exception("markdownify failed; sending plain text")
    if md:
        try:
            await send(md, ParseMode.MARKDOWN_V2)
            return
        except BadRequest as exc:
            log.warning("MarkdownV2 rejected (%s); retrying as plain text", exc)
        except Exception:
            log.exception("markdown send failed; retrying as plain text")
    try:
        await send(raw, None)
    except Exception:
        log.exception("plain-text send failed")
