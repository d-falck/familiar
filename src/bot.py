"""Telegram long-polling bot that forwards @mentions to Claude + Composio MCP."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
from collections import deque
from pathlib import Path
from zoneinfo import ZoneInfo

from aiohttp import web
from composio import Composio
from dotenv import load_dotenv
from telegram import MessageEntity, ReactionTypeEmoji, Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from agent import respond
from history import History
from silence import SILENCE_INSTRUCTION, is_placeholder_reply, is_silent
from telegram_md import send_markdown
from voice import transcribe as transcribe_voice
from webhook import build_app as build_webhook_app

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("bot")

# Sent on a direct user turn when the model collapses into a bare placeholder
# ("(no response)"/"<silent>"/empty) instead of a real answer. Never send the
# raw placeholder to the user — that's a bug, not a message.
EMPTY_REPLY_FALLBACK = (
    "⚠️ Sorry — that turn ended without producing a reply (usually a "
    "long/heavy task dropping its output). Nothing's lost on my end — re-send "
    "and I'll pick it straight back up."
)

# The once-daily morning nudge fired by the scheduler. Kept deliberately tight:
# Damon is overwhelmed and wants a short nudge, not a wall of text. Iterate on
# this over time.
TRIAGE_INSTRUCTION = (
    "Daily triage nudge — HIGH bar, silent-by-default. Most mornings there is "
    "nothing worth sending: when that's the case, respond with the silence "
    "sentinel and send nothing. Only break silence for a GENUINE same-day "
    "actionable with real cost: a hard deadline today, someone actively blocked "
    "on his reply, or a time-critical change. Keep it to a couple of lines. "
    "Do NOT surface: (a) anything already on his calendar or that recurs and he "
    "already knows about — housekeeper/cleaner days, scheduled meetings, "
    "rehearsals, deliveries, parcels; (b) a generic 'your most important task "
    "today is X' or task-list pointer — he can see his own task list; (c) "
    "newsletters, receipts, routine noise; (d) anything already flagged on a "
    "previous day. He has repeatedly called routine morning nudges noise, so "
    "when in doubt, stay silent.\n\n"
    f"{SILENCE_INSTRUCTION}"
)


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

_DM_CONTEXT_SEP = (
    "[Above is your main DM history — shared background you carry into every "
    "topic. This topic's own conversation follows.]"
)

# How many recently-completed update ids to remember (in memory and on disk).
_HANDLED_CAP = 2000


def _load_handled(path: Path) -> tuple[set[int], deque]:
    """Load the persisted set of fully-handled Telegram update ids. Survives
    restarts so a deploy (which wipes the in-memory dedup) can't cause Telegram
    to re-deliver an already-answered update and fire a duplicate reply. Best
    effort: a missing/corrupt file just yields an empty set."""
    try:
        ids = [int(x) for x in json.loads(path.read_text())]
        return set(ids), deque(ids)
    except Exception:
        return set(), deque()


def _mark_handled(bot_data: dict, update_id: int) -> None:
    """Record an update as fully handled (turn ran AND reply was sent), in
    memory and on disk. Called only at the end of a turn, so an interrupted
    turn (e.g. killed by a deploy restart) is NOT marked — it re-runs on
    redelivery and the reply isn't lost. Persistence is best-effort."""
    ids: set[int] = bot_data["handled_update_ids"]
    order: deque = bot_data["handled_update_order"]
    if update_id in ids:
        return
    ids.add(update_id)
    order.append(update_id)
    while len(order) > _HANDLED_CAP:
        ids.discard(order.popleft())
    try:
        bot_data["handled_updates_path"].write_text(json.dumps(list(order)))
    except Exception:
        log.exception("failed to persist handled update ids")


def _session_lock(bot_data: dict, chat_id: int, thread_id: int) -> asyncio.Lock:
    """One lock per (chat, topic) session: turns within a session are
    serialized (a follow-up waits for the prior turn), while different
    sessions run in parallel."""
    locks = bot_data["session_locks"]
    key = (chat_id, thread_id)
    lock = locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        locks[key] = lock
    return lock


def _load_session_context(
    history: History,
    *,
    main_dm_chat_id: int | None,
    chat_id: int,
    thread_id: int,
    workspace_chat_ids: set[int],
) -> list[dict]:
    """Transcript for a session. Forum-topic sessions in a workspace group are
    prefixed with the main DM history so they share that background on top of
    their own thread; the DM and ordinary chats just get their own."""
    own = history.load_as_messages(chat_id, thread_id)
    if (
        main_dm_chat_id
        and chat_id in workspace_chat_ids
        and not (chat_id == main_dm_chat_id and thread_id == 0)
    ):
        base = history.load_as_messages(main_dm_chat_id, 0)
        if base:
            return base + [{"role": "user", "content": _DM_CONTEXT_SEP}] + own
    return own


async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Drop duplicate deliveries of the same update. Telegram can re-deliver an
    # update (network retries; a restart before the poll offset is acked), and
    # since updates are handled concurrently a redelivery would otherwise run a
    # second full turn — producing two complete replies to one message. The
    # check + add are await-free, so concurrent handlers can't interleave here.
    seen_ids: set = context.application.bot_data["seen_update_ids"]
    handled_ids: set = context.application.bot_data["handled_update_ids"]
    if update.update_id in seen_ids or update.update_id in handled_ids:
        log.info("duplicate update_id=%s dropped", update.update_id)
        return
    seen_order: deque = context.application.bot_data["seen_update_order"]
    seen_ids.add(update.update_id)
    seen_order.append(update.update_id)
    if len(seen_order) > 2000:
        seen_ids.discard(seen_order.popleft())

    message = update.effective_message
    chat = update.effective_chat
    text = message.text or message.caption or ""
    user = message.from_user
    # Forum-topic id (0 for DMs / non-topic chats). Each topic is its own
    # isolated session: separate history, serialized within itself, parallel
    # with other topics and the DM.
    thread_id = message.message_thread_id or 0

    history: History = context.application.bot_data["history"]
    cfg: dict = context.application.bot_data["cfg"]
    attachments_dir: Path = context.application.bot_data["attachments_dir"]
    workspace_chat_ids: set[int] = context.application.bot_data["workspace_chat_ids"]
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

    # If the message is a voice note, download and transcribe with Whisper.
    if message.voice:
        attachments_dir.mkdir(parents=True, exist_ok=True)
        file = await message.voice.get_file()
        audio_path = attachments_dir / f"{chat.id}_{message.message_id}.ogg"
        await file.download_to_drive(audio_path)
        try:
            transcript = await transcribe_voice(audio_path)
        except Exception:
            log.exception("voice transcription failed")
            transcript = "(voice note — transcription failed)"
        text = (text + f"\n[voice] {transcript}").strip()

    if not text:
        return

    author = user.username or user.full_name
    history.add_user(chat.id, author, text, thread_id=thread_id)

    # DMs and configured workspace groups: respond to every message. Other
    # groups: only @-mentions or replies to bot.
    if chat.type == "private" or chat.id in workspace_chat_ids:
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
        await send_markdown(
            lambda body, pm: context.bot.send_message(
                chat_id=chat.id,
                message_thread_id=thread_id or None,
                text=body,
                parse_mode=pm,
            ),
            text,
        )

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
            await context.bot.send_chat_action(
                chat_id=chat.id, message_thread_id=thread_id or None, action="typing"
            )
            await asyncio.sleep(4)

    bot_data = context.application.bot_data

    async def run_turn() -> None:
        typing = asyncio.create_task(keep_typing())
        error = None
        try:
            messages = _load_session_context(
                history,
                main_dm_chat_id=bot_data["main_dm_chat_id"],
                chat_id=chat.id,
                thread_id=thread_id,
                workspace_chat_ids=workspace_chat_ids,
            )
            reply = await respond(
                messages,
                on_tool_use=on_tool_use,
                on_text=on_text,
                on_thinking=on_thinking,
                on_tool_result=on_tool_result,
                **cfg,
            )
        except asyncio.CancelledError:
            # Superseded by a newer message in this session (interrupt-on-send).
            # Abandon quietly: no reply, no assistant row, not marked handled —
            # nothing is lost, the newer turn already has this message in its
            # transcript. typing is stopped by the finally below.
            log.info("turn superseded chat=%s thread=%s", chat.id, thread_id)
            raise
        except Exception as exc:
            log.exception("respond failed")
            error = exc
            reply = f"⚠️ {exc}"
        finally:
            typing.cancel()

        # A direct user turn should NEVER answer with a bare placeholder. If the
        # model collapsed (empty result / "(no response)" / a stray sentinel),
        # swap in an honest message instead of sending the literal garbage.
        if not error and is_placeholder_reply(reply):
            log.warning(
                "collapsed placeholder reply on user path chat=%s thread=%s reply=%r",
                chat.id,
                thread_id,
                reply,
            )
            reply = EMPTY_REPLY_FALLBACK

        history.add_assistant(chat.id, reply, thread_id=thread_id)
        await set_reaction(REACTION_ERROR if error else None)
        await send_debug((f"⚠️ {error}" if error else f"✅ {reply}"))
        # If the final reply is identical to the last text block we already
        # streamed, skip it — no point duplicating the message.
        if error or reply != last_streamed_text["value"]:
            await send_markdown(
                lambda body, pm: message.reply_text(body, parse_mode=pm),
                reply,
            )
        # Mark fully handled only now (turn done + reply sent) so a redelivery
        # after a restart can't fire a duplicate, while an interrupted turn
        # (which never reaches here) still re-runs and isn't lost.
        _mark_handled(bot_data, update.update_id)

    key = (chat.id, thread_id)
    if bot_data.get("interrupt_on_send", True):
        # Interrupt-on-send: a new message in this session cancels the in-flight
        # turn and starts a fresh one, which reloads the transcript (now holding
        # BOTH the interrupted message and this one) and re-plans against the
        # latest input. The swap (cancel prior + register new) is done under a
        # short-held per-session lock so two near-simultaneous messages can't
        # both start a turn; the turn itself runs OUTSIDE that lock. Different
        # sessions (other topics / the DM) are unaffected and run in parallel.
        running: dict = bot_data["running_turns"]
        swap_lock = _session_lock(bot_data, chat.id, thread_id)
        async with swap_lock:
            prev = running.get(key)
            if prev is not None and not prev.done():
                log.info("interrupting in-flight turn chat=%s thread=%s", chat.id, thread_id)
                prev.cancel()
            task = asyncio.create_task(run_turn())
            running[key] = task
        try:
            await task
        except asyncio.CancelledError:
            # This turn was itself superseded by a later message — fine.
            pass
        finally:
            if running.get(key) is task:
                running.pop(key, None)
    else:
        # Legacy: serialize turns within a session — a follow-up waits for the
        # prior turn rather than interrupting it.
        async with _session_lock(bot_data, chat.id, thread_id):
            await run_turn()


def _seconds_to_next_boundary(interval_seconds: int, tz) -> float:
    """Seconds until the next interval boundary aligned to local midnight.

    For interval_seconds=3600 this means the next full hour in the given
    timezone (e.g. 15:00:00 local). Drift-free: recomputed each iteration
    so processing time doesn't shift subsequent firings.
    """
    from datetime import datetime

    now = datetime.now(tz)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    elapsed = (now - midnight).total_seconds()
    next_boundary = (int(elapsed // interval_seconds) + 1) * interval_seconds
    return next_boundary - elapsed


async def _run_scheduler(
    *,
    interval_seconds: int,
    chat_id: int,
    tz,
    telegram_bot,
    history,
    respond_cfg: dict,
    triage_hour: int = 8,
) -> None:
    """In-process hourly loop. Exactly one tick per day (the one landing on
    ``triage_hour``) carries the minimal daily-triage nudge; every other tick
    is travel/deadline-only so the loop stays near-silent the rest of the day.
    """
    from datetime import datetime

    while True:
        await asyncio.sleep(_seconds_to_next_boundary(interval_seconds, tz))
        try:
            now_dt = datetime.now(tz)
            now = now_dt.strftime("%a %Y-%m-%d %H:%M %Z")
            if now_dt.hour == triage_hour:
                prompt = f"[scheduled check-in @ {now}] {TRIAGE_INSTRUCTION}"
            else:
                prompt = (
                    f"[scheduled check-in @ {now}] Non-triage tick. Only break "
                    f"silence for a travel departure reminder that is due now "
                    f"(per the travel rules in memory), or a genuine same-day "
                    f"deadline with real cost. Otherwise stay silent.\n\n"
                    f"{SILENCE_INSTRUCTION}"
                )
            messages = history.load_as_messages(chat_id)
            messages.append({"role": "user", "content": prompt})
            reply = await respond(messages, **respond_cfg)
            if not is_silent(reply):
                history.add_assistant(chat_id, reply)
                await send_markdown(
                    lambda body, pm: telegram_bot.send_message(
                        chat_id=chat_id, text=body, parse_mode=pm
                    ),
                    reply,
                )
        except Exception:
            log.exception("scheduled check-in failed")


def _bootstrap_self_repo() -> str | None:
    """Ensure a git checkout of our own source exists on the persistent
    volume so the agent can read, edit, push and redeploy itself.

    Returns the checkout path, or None if self-improvement is disabled
    (SELF_REPO_DIR unset). The GitHub token is baked into the remote URL
    (single-tenant personal bot) and refreshed each boot so rotations take
    effect. Survives restarts via the /data volume.
    """
    repo_dir = os.environ.get("SELF_REPO_DIR")
    if not repo_dir:
        return None
    repo_url = os.environ["SELF_REPO_URL"]
    token = os.environ.get("GITHUB_TOKEN", "")
    auth_url = (
        repo_url.replace("https://", f"https://x-access-token:{token}@")
        if token
        else repo_url
    )
    if not Path(repo_dir, ".git").exists():
        subprocess.run(["git", "clone", auth_url, repo_dir], check=True)
    subprocess.run(["git", "-C", repo_dir, "remote", "set-url", "origin", auth_url], check=True)
    subprocess.run(["git", "-C", repo_dir, "config", "user.name", "Iris"], check=True)
    subprocess.run(["git", "-C", repo_dir, "config", "user.email", "iris@familiar.bot"], check=True)
    log.info("self-repo ready at %s", repo_dir)
    return repo_dir


async def _run() -> None:
    load_dotenv()

    self_repo_dir = _bootstrap_self_repo()

    session = Composio().create(user_id=os.environ["COMPOSIO_USER_ID"])
    mcp_servers = {
        "composio": {
            "type": session.mcp.type,
            "url": session.mcp.url,
            "headers": session.mcp.headers,
        }
    }

    backend = os.environ.get("AGENT_BACKEND", "claude").lower()
    model = (
        os.environ.get("CODEX_MODEL", "gpt-5.5")
        if backend == "codex"
        else os.environ.get("ANTHROPIC_MODEL", "claude-opus-4-8[1m]")
    )
    log.info("agent backend=%s model=%s", backend, model)

    # Workspace groups: chats where she answers every message (no @-mention
    # needed) and treats forum topics as parallel sessions.
    workspace_chat_ids = {
        int(x)
        for x in os.environ.get("WORKSPACE_CHAT_IDS", "").replace(" ", "").split(",")
        if x
    }

    history_path = os.environ.get("HISTORY_DB_PATH", "./history.sqlite")
    respond_cfg = {
        "backend": backend,
        "mcp_servers": mcp_servers,
        "model": model,
        "max_turns": int(os.environ.get("MAX_AGENT_TURNS", "40")),
        "memory_path": os.environ.get("MEMORY_PATH", "./memory.md"),
        "persona_path": os.environ.get("PERSONA_PATH", "prompts/flat_hunt.md"),
        "history_path": history_path,
        "self_repo_dir": self_repo_dir,
        "self_deploy_cmd": os.environ.get("SELF_DEPLOY_CMD"),
        "workspace_chat_ids": sorted(workspace_chat_ids),
    }
    history = History(history_path)

    # Process messages concurrently so you can fire several requests at once
    # and they run in parallel (replies interleave). Bounded because each turn
    # spawns its own model subprocess — too many at once would exhaust RAM.
    max_concurrent = int(os.environ.get("MAX_CONCURRENT_UPDATES", "4"))
    app = (
        Application.builder()
        .token(os.environ["TELEGRAM_BOT_TOKEN"])
        .concurrent_updates(max_concurrent)
        .build()
    )
    app.bot_data["cfg"] = respond_cfg
    app.bot_data["history"] = history
    app.bot_data["debug_chat_id"] = os.environ.get("DEBUG_CHAT_ID")
    app.bot_data["attachments_dir"] = Path(
        os.environ.get("ATTACHMENTS_DIR", "./attachments")
    )
    # Off by default: streaming every intermediate text block to the main chat
    # turns one multi-step turn into several messages that read like duplicate
    # or contradicting answers. Reactions already signal progress; the main chat
    # now gets exactly one message per turn (the final reply), while intermediate
    # text still goes to the debug feed. Set STREAM_INTERMEDIATE_TEXT=true to
    # restore live streaming.
    app.bot_data["stream_intermediate_text"] = (
        os.environ.get("STREAM_INTERMEDIATE_TEXT", "false").lower() == "true"
    )
    app.bot_data["workspace_chat_ids"] = workspace_chat_ids
    app.bot_data["session_locks"] = {}
    # Interrupt-on-send: a new message in a session cancels its in-flight turn
    # and restarts with the updated transcript, instead of queuing behind it.
    # running_turns maps (chat_id, thread_id) -> the live turn's asyncio Task.
    # Set INTERRUPT_ON_SEND=false to fall back to serialized (queued) turns.
    app.bot_data["running_turns"] = {}
    app.bot_data["interrupt_on_send"] = (
        os.environ.get("INTERRUPT_ON_SEND", "true").lower() == "true"
    )
    # Dedup of already-handled Telegram updates (see on_message). seen_* is the
    # in-process guard against concurrent re-delivery; handled_* is persisted to
    # the data volume so a restart/deploy can't re-answer an already-completed
    # update (the cause of duplicate/late replies around deploys).
    app.bot_data["seen_update_ids"] = set()
    app.bot_data["seen_update_order"] = deque()
    handled_path = Path(history_path).with_name("handled_updates.json")
    handled_ids, handled_order = _load_handled(handled_path)
    app.bot_data["handled_updates_path"] = handled_path
    app.bot_data["handled_update_ids"] = handled_ids
    app.bot_data["handled_update_order"] = handled_order
    app.bot_data["main_dm_chat_id"] = (
        int(os.environ["MAIN_DM_CHAT_ID"]) if os.environ.get("MAIN_DM_CHAT_ID") else None
    )

    async def on_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat = update.effective_chat
        await context.bot.send_message(chat_id=chat.id, text=f"chat id: {chat.id}")

    app.add_handler(CommandHandler("id", on_id))
    app.add_handler(
        MessageHandler(
            (filters.ChatType.GROUPS | filters.ChatType.PRIVATE)
            & (filters.TEXT | filters.CAPTION | filters.PHOTO | filters.VOICE),
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
    voice_dispatch_secret = os.environ.get("VOICE_DISPATCH_SECRET")
    # Conversation-initiation webhook for the voice agent reuses the dispatch
    # secret by default (same ElevenLabs↔Fly trust boundary); override with
    # VOICE_INIT_SECRET if you want them separate.
    voice_init_secret = os.environ.get("VOICE_INIT_SECRET") or voice_dispatch_secret
    voice_persona_path = os.environ.get("VOICE_PERSONA_PATH", "prompts/iris_voice.md")
    mcp_proxy_upstream = os.environ.get("MCP_PROXY_UPSTREAM")
    aiohttp_app = build_webhook_app(
        secret=webhook_secret,
        target_chat_id=int(trigger_chat_raw) if trigger_chat_raw else None,
        voice_dispatch_secret=voice_dispatch_secret,
        voice_init_secret=voice_init_secret,
        voice_persona_path=voice_persona_path,
        memory_path=respond_cfg["memory_path"],
        tz=ZoneInfo(os.environ.get("TIMEZONE", "UTC")),
        mcp_proxy_upstream=mcp_proxy_upstream,
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
    if voice_init_secret and trigger_chat_raw:
        log.info("voice-init shared-context endpoint live at /voice/init")

    # In-process scheduler: periodically nudge the agent so it has a chance
    # to act proactively (check email, surface reminders, etc). Silence is
    # the default; the agent should return nothing for uneventful ticks.
    schedule_interval_raw = os.environ.get("SCHEDULE_INTERVAL_SECONDS")
    if schedule_interval_raw and trigger_chat_raw:
        tz = ZoneInfo(os.environ.get("TIMEZONE", "UTC"))
        asyncio.create_task(
            _run_scheduler(
                interval_seconds=int(schedule_interval_raw),
                chat_id=int(trigger_chat_raw),
                tz=tz,
                telegram_bot=app.bot,
                history=history,
                respond_cfg=respond_cfg,
                triage_hour=int(os.environ.get("TRIAGE_HOUR", "8")),
            )
        )
        log.info(
            "scheduler running every %ss (tz=%s, triage_hour=%s)",
            schedule_interval_raw,
            tz,
            os.environ.get("TRIAGE_HOUR", "8"),
        )

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
