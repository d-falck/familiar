"""Audio helpers: transcribe incoming Telegram voice notes with Whisper."""

from __future__ import annotations

import logging
import os
from pathlib import Path

from openai import AsyncOpenAI

log = logging.getLogger("voice")

_client: AsyncOpenAI | None = None


def _get_client() -> AsyncOpenAI:
    global _client
    if _client is None:
        _client = AsyncOpenAI(api_key=os.environ["OPENAI_API_KEY"])
    return _client


async def transcribe(path: Path) -> str:
    """Transcribe a local audio file via OpenAI Whisper (whisper-1)."""
    client = _get_client()
    with open(path, "rb") as f:
        result = await client.audio.transcriptions.create(
            model="whisper-1",
            file=f,
        )
    return result.text.strip()
