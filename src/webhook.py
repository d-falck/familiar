"""Composio trigger webhook receiver.

Exposes POST /composio/webhook. Verifies the Svix-style signature, turns
the event payload into a user message, dispatches it through the same
respond() path the Telegram handler uses, and posts the result into a
configured Telegram chat.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import hmac
import json
import logging
import time
from collections import OrderedDict
from typing import Any

import aiohttp
from aiohttp import web

from silence import SILENCE_INSTRUCTION, is_silent

log = logging.getLogger("webhook")

TOLERANCE_SECONDS = 300
# Remember recent webhook-ids so we can dedupe retries even if we ACK'd
# successfully. Bounded LRU so it never grows unbounded.
_SEEN_IDS_MAX = 1000


def _candidate_keys(secret: str) -> list[bytes]:
    """Composio webhook secrets have been observed as hex (64 chars → 32 bytes).
    Fall back to raw UTF-8 and base64 so we stay robust to any format change.
    """
    keys: list[bytes] = []
    try:
        keys.append(bytes.fromhex(secret))
    except ValueError:
        pass
    try:
        keys.append(base64.b64decode(secret.removeprefix("whsec_")))
    except (ValueError, binascii.Error):  # type: ignore[name-defined]
        pass
    keys.append(secret.encode("utf-8"))
    return keys


def _verify_signature(body: bytes, headers: dict[str, str], secret: str) -> bool:
    """Verify a Composio webhook signature.

    Header format (Svix-compatible): `webhook-signature: v1,<base64>` — may
    contain multiple space-separated `vN,<b64>` pairs for key rotation.
    Signed payload: `<webhook-id>.<webhook-timestamp>.<raw_body>`
    """
    msg_id = headers.get("webhook-id", "")
    timestamp = headers.get("webhook-timestamp", "")
    sig_header = headers.get("webhook-signature", "")
    if not (msg_id and timestamp and sig_header):
        return False

    try:
        ts = int(timestamp)
    except ValueError:
        return False
    if abs(int(time.time()) - ts) > TOLERANCE_SECONDS:
        return False

    signed = f"{msg_id}.{timestamp}.".encode("utf-8") + body

    for key in _candidate_keys(secret):
        expected = base64.b64encode(
            hmac.new(key, signed, hashlib.sha256).digest()
        ).decode()
        for pair in sig_header.split():
            version, _, sig = pair.partition(",")
            if version.startswith("v") and hmac.compare_digest(sig, expected):
                return True
    return False


def _format_event(payload: dict[str, Any]) -> str:
    """Render a Composio trigger payload into a user message for Claude."""
    trigger_name = payload.get("triggerName") or payload.get("type") or "unknown"
    data = payload.get("data") or payload.get("payload") or payload
    try:
        data_str = json.dumps(data, indent=2, ensure_ascii=False)
    except (TypeError, ValueError):
        data_str = str(data)
    return (
        f"A Composio trigger fired: {trigger_name}\n\n"
        f"<event>\n{data_str}\n</event>\n\n"
        f"Process this event and take whatever action is appropriate.\n\n"
        f"{SILENCE_INSTRUCTION}"
    )


def build_app(
    *,
    secret: str | None = None,
    target_chat_id: int | None = None,
    voice_dispatch_secret: str | None = None,
    mcp_proxy_upstream: str | None = None,
    telegram_bot=None,
    history=None,
    respond_fn=None,
    respond_cfg: dict | None = None,
) -> web.Application:
    """Build an aiohttp app. /health is always exposed; /composio/webhook
    is only added when all the trigger-handling dependencies are supplied.

    The webhook handler ACKs immediately and runs the agent loop in a
    background task. Composio's HTTP client times out fast and retries
    aggressively if we hold the connection open while Claude runs, so a
    synchronous handler produces 3-4× duplicate deliveries. We also track
    seen webhook-ids in an LRU to dedupe retries that slip through.
    """
    seen_ids: OrderedDict[str, None] = OrderedDict()

    def _mark_seen(msg_id: str) -> bool:
        """Return True if this id was already seen (i.e. duplicate)."""
        if msg_id in seen_ids:
            seen_ids.move_to_end(msg_id)
            return True
        seen_ids[msg_id] = None
        while len(seen_ids) > _SEEN_IDS_MAX:
            seen_ids.popitem(last=False)
        return False

    async def _process_event(payload: dict) -> None:
        event_text = _format_event(payload)
        log.info(
            "dispatching composio trigger: %s",
            payload.get("triggerName") or payload.get("type"),
        )
        # Build an ephemeral prompt: the existing persisted chat history
        # plus this one-off event as the final user turn. We do NOT call
        # history.add_user on the raw event — it would bloat the transcript
        # for future turns and quickly blow past the context window.
        messages = history.load_as_messages(target_chat_id)
        messages.append({"role": "user", "content": event_text})

        try:
            reply = await respond_fn(messages, **respond_cfg)
        except Exception as exc:
            log.exception("trigger respond failed")
            reply = f"⚠️ trigger handler failed: {exc}"

        if not is_silent(reply):
            # Persist the reply so the user can refer to it in future turns,
            # but as a single terse assistant row (not the original event).
            history.add_assistant(target_chat_id, reply)
            try:
                await telegram_bot.send_message(chat_id=target_chat_id, text=reply[:4000])
            except Exception:
                log.exception("failed to post trigger reply to telegram")
        else:
            log.info("trigger handler chose silence — skipping telegram send")

    async def webhook_handler(request: web.Request) -> web.Response:
        body = await request.read()
        headers = {k.lower(): v for k, v in request.headers.items()}
        if not _verify_signature(body, headers, secret):
            log.warning(
                "rejected webhook with bad signature; headers=%s body_first_200=%r",
                {k: v for k, v in headers.items() if k.startswith("webhook-") or k in ("content-type",)},
                body[:200],
            )
            return web.Response(status=401, text="invalid signature")

        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            return web.Response(status=400, text="invalid json")

        msg_id = headers.get("webhook-id") or payload.get("id", "")
        if msg_id and _mark_seen(msg_id):
            log.info("dropping duplicate webhook %s", msg_id)
            return web.Response(status=200, text="duplicate ignored")

        # ACK immediately; run the agent in the background so Composio's
        # HTTP client doesn't time out and trigger retries.
        asyncio.create_task(_process_event(payload))
        return web.Response(status=200, text="ok")

    async def _process_voice_dispatch(instruction: str) -> None:
        log.info("dispatching voice-call task: %s", instruction[:120])
        messages = history.load_as_messages(target_chat_id)
        messages.append({"role": "user", "content": f"[voice call dispatch] {instruction}"})
        try:
            reply = await respond_fn(messages, **respond_cfg)
        except Exception as exc:
            log.exception("voice dispatch respond failed")
            reply = f"⚠️ voice dispatch failed: {exc}"
        if not is_silent(reply):
            history.add_assistant(target_chat_id, reply)
            try:
                await telegram_bot.send_message(chat_id=target_chat_id, text=reply[:4000])
            except Exception:
                log.exception("failed to post voice dispatch reply to telegram")

    async def voice_dispatch_handler(request: web.Request) -> web.Response:
        # ElevenLabs webhook-tool config sends the shared secret as a header.
        provided = request.headers.get("x-dispatch-secret", "")
        if not voice_dispatch_secret or not hmac.compare_digest(
            provided, voice_dispatch_secret
        ):
            log.warning("voice dispatch rejected: bad or missing secret header")
            return web.Response(status=401, text="invalid secret")

        try:
            payload = await request.json()
        except Exception:
            return web.Response(status=400, text="invalid json")

        instruction = (payload.get("instruction") or "").strip()
        if not instruction:
            return web.Response(status=400, text="missing 'instruction'")

        # ACK immediately, dispatch in background.
        asyncio.create_task(_process_voice_dispatch(instruction))
        return web.json_response({"status": "dispatched"})

    async def mcp_proxy_handler(request: web.Request) -> web.StreamResponse:
        """Transparent MCP Streamable-HTTP proxy.

        Forwards the request to `mcp_proxy_upstream`, forcing the spec-
        compliant `Accept: application/json, text/event-stream` header.
        Exists because some MCP clients (ElevenLabs) send only
        `application/json`, which Composio's server rejects with 406.
        """
        hop_by_hop = {
            "host", "content-length", "connection", "transfer-encoding",
            "keep-alive", "proxy-authenticate", "proxy-authorization",
            "te", "trailers", "upgrade",
        }
        fwd_headers = {
            k: v for k, v in request.headers.items()
            if k.lower() not in hop_by_hop
        }
        fwd_headers["Accept"] = "application/json, text/event-stream"
        body = await request.read()

        timeout = aiohttp.ClientTimeout(total=None, sock_read=300)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.request(
                request.method,
                mcp_proxy_upstream,
                data=body if body else None,
                headers=fwd_headers,
                allow_redirects=False,
            ) as upstream:
                resp_headers = {
                    k: v for k, v in upstream.headers.items()
                    if k.lower() not in hop_by_hop
                }
                response = web.StreamResponse(
                    status=upstream.status,
                    headers=resp_headers,
                )
                await response.prepare(request)
                async for chunk in upstream.content.iter_any():
                    await response.write(chunk)
                await response.write_eof()
                return response

    async def health(_request: web.Request) -> web.Response:
        return web.Response(status=200, text="ok")

    app = web.Application()
    app.router.add_get("/health", health)
    if secret and target_chat_id is not None:
        app.router.add_post("/composio/webhook", webhook_handler)
    if voice_dispatch_secret and target_chat_id is not None:
        app.router.add_post("/voice/dispatch", voice_dispatch_handler)
    if mcp_proxy_upstream:
        for method in ("POST", "GET", "DELETE"):
            app.router.add_route(method, "/mcp", mcp_proxy_handler)
    return app
