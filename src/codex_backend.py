"""Codex backend: runs a turn by shelling out to `codex exec --json`.

Mirrors the Claude backend's interface (same callbacks, same return: the
final assistant text). Codex reads MCP servers from a per-run config.toml
in a temp CODEX_HOME, streams newline-delimited JSON events on stdout, and
writes its final message to a file via --output-last-message.

Auth: uses OPENAI_API_KEY from the environment (no ChatGPT login present in
the container, so Codex falls back to the API key automatically).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import tempfile
from collections.abc import Awaitable, Callable
from pathlib import Path

from prompt import render_transcript

log = logging.getLogger(__name__)

# See claude_backend for rationale: must clear the longest legitimate silent
# gap (a single multi-minute tool call), not just normal token latency.
IDLE_TIMEOUT_SECONDS = int(os.environ.get("AGENT_IDLE_TIMEOUT_SECONDS", "300"))


def _toml_str(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _write_codex_home(mcp_servers: dict) -> str:
    """Write a throwaway CODEX_HOME/config.toml wiring up the MCP servers as
    remote streamable-HTTP servers with their auth headers. Returns the dir.
    """
    # Codex refuses to create its helper binaries under a temp dir, so keep
    # CODEX_HOME on a real volume (CODEX_HOME_BASE=/data in prod).
    base = os.environ.get("CODEX_HOME_BASE") or tempfile.gettempdir()
    Path(base).mkdir(parents=True, exist_ok=True)
    home = tempfile.mkdtemp(prefix="codex-home-", dir=base)
    lines: list[str] = []
    for name, spec in mcp_servers.items():
        url = spec.get("url")
        if not url:
            continue
        lines.append(f"[mcp_servers.{name}]")
        lines.append(f"url = {_toml_str(url)}")
        headers = spec.get("headers") or {}
        if headers:
            pairs = ", ".join(f"{_toml_str(k)} = {_toml_str(v)}" for k, v in headers.items())
            lines.append(f"http_headers = {{ {pairs} }}")
        lines.append("")
    Path(home, "config.toml").write_text("\n".join(lines))
    return home


async def _dispatch_event(
    event: dict,
    *,
    on_tool_use,
    on_text,
    on_thinking,
) -> None:
    etype = event.get("type")
    item = event.get("item") or {}
    itype = item.get("type")
    if etype == "item.started":
        if itype == "reasoning" and on_thinking:
            await on_thinking(item.get("text") or item.get("content") or "")
        elif itype == "command_execution" and on_tool_use:
            await on_tool_use("bash", {"command": item.get("command", "")})
        elif itype == "mcp_tool_call" and on_tool_use:
            name = item.get("tool") or item.get("name") or "mcp"
            await on_tool_use(name, dict(item.get("arguments") or {}))
    elif etype == "item.completed":
        if itype == "agent_message" and on_text:
            text = item.get("text") or item.get("content") or ""
            if text.strip():
                await on_text(text)
    elif etype == "error":
        log.error("codex error event: %s", event.get("message"))


async def run(
    messages: list[dict],
    *,
    system_prompt: str,
    model: str,
    mcp_servers: dict,
    workdir: str | None = None,
    max_turns: int = 40,
    on_tool_use: Callable[[str, dict], Awaitable[None]] | None = None,
    on_text: Callable[[str], Awaitable[None]] | None = None,
    on_thinking: Callable[[str], Awaitable[None]] | None = None,
    on_tool_result: Callable[[str, str], Awaitable[None]] | None = None,
) -> str:
    codex_home = _write_codex_home(mcp_servers)
    final_file = Path(codex_home, "final.txt")
    full_prompt = f"{system_prompt}\n\n{render_transcript(messages)}"

    # `codex exec` reads CODEX_API_KEY (its exec-only inline auth var) for
    # headless API-key auth; plain OPENAI_API_KEY is not picked up here.
    env = {**os.environ, "CODEX_HOME": codex_home}
    if os.environ.get("OPENAI_API_KEY"):
        env["CODEX_API_KEY"] = os.environ["OPENAI_API_KEY"]

    try:
        return await _run_proc(
            full_prompt, model, final_file, workdir, env,
            on_tool_use=on_tool_use, on_text=on_text, on_thinking=on_thinking,
        )
    finally:
        shutil.rmtree(codex_home, ignore_errors=True)


async def _run_proc(
    full_prompt, model, final_file, workdir, env,
    *, on_tool_use, on_text, on_thinking,
) -> str:
    proc = await asyncio.create_subprocess_exec(
        "codex", "exec", "--json", "--yolo",
        "-m", model,
        "--output-last-message", str(final_file),
        "-",
        cwd=workdir or None,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    proc.stdin.write(full_prompt.encode())
    await proc.stdin.drain()
    proc.stdin.close()

    last_text = ""

    async def _on_text(text: str) -> None:
        nonlocal last_text
        last_text = text
        if on_text:
            await on_text(text)

    while True:
        try:
            raw = await asyncio.wait_for(
                proc.stdout.readline(), timeout=IDLE_TIMEOUT_SECONDS
            )
        except asyncio.TimeoutError:
            proc.kill()
            raise RuntimeError(
                f"codex produced no output for {IDLE_TIMEOUT_SECONDS}s — aborting"
            )
        if not raw:
            break
        line = raw.decode(errors="replace").strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            log.info("[codex non-json] %s", line[:500])
            continue
        log.info("[codex event] %s", line[:500])
        await _dispatch_event(
            event, on_tool_use=on_tool_use, on_text=_on_text, on_thinking=on_thinking
        )

    await proc.wait()
    if proc.returncode != 0:
        stderr = (await proc.stderr.read()).decode(errors="replace")
        log.error("codex exited %s: %s", proc.returncode, stderr[:1000])

    final = ""
    if final_file.exists():
        final = final_file.read_text().strip()
    return final or last_text or "(no response)"
