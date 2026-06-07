#!/usr/bin/env python3
"""Provision the ElevenLabs Iris voice agent from repo-owned config.

The script is intentionally conservative:
- reads the live agent before updating
- upserts only Iris-owned webhook tools by name
- patches only configured conversation_config fields
- defaults to dry-run; pass --apply to write

Required env vars:
  ELEVENLABS_API_KEY
  ELEVENLABS_IRIS_AGENT_ID
  IRIS_VOICE_DISPATCH_URL
  VOICE_DISPATCH_SECRET
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


API_BASE = "https://api.elevenlabs.io/v1"
REPO_ROOT = Path(__file__).resolve().parents[1]


class ElevenLabsError(RuntimeError):
    pass


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


def env_required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ElevenLabsError(f"missing required env var: {name}")
    return value


def request_json(
    method: str,
    path: str,
    *,
    api_key: str,
    body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    data = None
    headers = {
        "xi-api-key": api_key,
        "api-key": api_key,
        "Content-Type": "application/json",
    }
    if body is not None:
        data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        f"{API_BASE}{path}",
        data=data,
        headers=headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = resp.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise ElevenLabsError(
            f"{method} {path} failed: HTTP {exc.code}: {detail}"
        ) from exc
    except urllib.error.URLError as exc:
        raise ElevenLabsError(f"{method} {path} failed: {exc}") from exc
    return json.loads(payload.decode("utf-8") or "{}")


def deep_merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(base)
    for key, value in patch.items():
        if (
            isinstance(value, dict)
            and isinstance(result.get(key), dict)
        ):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def clean_none(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: clean_none(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [clean_none(v) for v in value]
    return value


def find_tool(tools_response: dict[str, Any], name: str) -> dict[str, Any] | None:
    for tool in tools_response.get("tools", []):
        if tool.get("tool_config", {}).get("name") == name:
            return tool
    return None


def build_webhook_tool_config(tool: dict[str, Any]) -> dict[str, Any]:
    url = env_required(tool["url_env"])
    secret = env_required(tool["secret_env"])
    return {
        "type": "webhook",
        "name": tool["name"],
        "description": tool["description"],
        "response_timeout_secs": tool.get("response_timeout_secs", 3),
        "expects_response": tool.get("expects_response", False),
        "parameters": tool["parameters"],
        "api_schema": {
            "url": url,
            "method": "POST",
            "path_params_schema": {"properties": {}, "required": []},
            "query_params_schema": {"properties": {}, "required": []},
            "request_body_schema": tool["parameters"],
            "request_headers": {
                tool.get("secret_header", "x-dispatch-secret"): secret
            },
        },
    }


def upsert_tool(
    *,
    api_key: str,
    tool: dict[str, Any],
    apply: bool,
) -> str:
    config = build_webhook_tool_config(tool)
    tools = request_json("GET", "/convai/tools", api_key=api_key)
    existing = find_tool(tools, tool["name"])
    payload = {"tool_config": config}

    if existing:
        tool_id = existing["id"]
        print(f"tool {tool['name']}: update {tool_id}")
        if apply:
            request_json("PATCH", f"/convai/tools/{tool_id}", api_key=api_key, body=payload)
        return tool_id

    print(f"tool {tool['name']}: create")
    if apply:
        created = request_json("POST", "/convai/tools", api_key=api_key, body=payload)
        return created["id"]
    return f"dry_run_{tool['name']}"


def attach_tools_to_prompt(
    conversation_config: dict[str, Any],
    tool_ids: list[str],
) -> dict[str, Any]:
    if not tool_ids:
        return conversation_config

    agent_config = conversation_config.setdefault("agent", {})
    prompt = agent_config.setdefault("prompt", {})
    existing = prompt.get("tools", [])

    normalized: list[Any] = []
    seen: set[str] = set()
    for item in existing:
        tool_id = item.get("tool_id") if isinstance(item, dict) else item
        if isinstance(tool_id, str):
            seen.add(tool_id)
        normalized.append(item)

    for tool_id in tool_ids:
        if tool_id not in seen:
            normalized.append({"tool_id": tool_id})
    prompt["tools"] = normalized
    return conversation_config


def build_agent_patch(
    config: dict[str, Any],
    current_agent: dict[str, Any],
    tool_ids: list[str],
) -> dict[str, Any]:
    prompt_file = REPO_ROOT / config["prompt_file"]
    prompt_text = prompt_file.read_text().strip()

    current_conversation_config = current_agent.get("conversation_config") or {}
    conversation_patch = copy.deepcopy(config.get("conversation_config") or {})
    prompt_patch = conversation_patch.setdefault("agent", {}).setdefault("prompt", {})
    prompt_patch["prompt"] = prompt_text

    merged_conversation_config = deep_merge(
        current_conversation_config,
        conversation_patch,
    )
    attach_tools_to_prompt(merged_conversation_config, tool_ids)

    return clean_none({
        "name": config.get("name"),
        "tags": config.get("tags"),
        "conversation_config": merged_conversation_config,
        "version_description": config.get("version_description"),
    })


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        redacted = {}
        for key, inner in value.items():
            if "secret" in key.lower() or key.lower() in {"api-key", "xi-api-key"}:
                redacted[key] = "<redacted>"
            else:
                redacted[key] = redact(inner)
        return redacted
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--config",
        default="config/elevenlabs/iris.json",
        help="Path to ElevenLabs provisioning config",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write changes to ElevenLabs. Omit for dry-run.",
    )
    args = parser.parse_args()

    config = load_json(REPO_ROOT / args.config)
    api_key = env_required("ELEVENLABS_API_KEY")
    agent_id = env_required(config.get("agent_id_env", "ELEVENLABS_IRIS_AGENT_ID"))

    current_agent = request_json("GET", f"/convai/agents/{agent_id}", api_key=api_key)
    print(f"agent: {current_agent.get('name')} ({agent_id})")

    tool_ids = [
        upsert_tool(api_key=api_key, tool=tool, apply=args.apply)
        for tool in config.get("tools", [])
    ]
    patch = build_agent_patch(config, current_agent, tool_ids)

    print("agent patch:")
    print(json.dumps(redact(patch), indent=2, sort_keys=True))

    if args.apply:
        request_json("PATCH", f"/convai/agents/{agent_id}", api_key=api_key, body=patch)
        verified = request_json("GET", f"/convai/agents/{agent_id}", api_key=api_key)
        print(
            "updated:",
            verified.get("name"),
            verified.get("metadata", {}).get("updated_at_unix_secs"),
        )
    else:
        print("dry-run only; pass --apply to write changes")

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ElevenLabsError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)
