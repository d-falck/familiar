"""Render Iris's schedules, triggers and connection health to a Notion page.

Damon asked for "somewhere to see my schedules and triggers" — until now they
only existed inside this codebase and Composio's dashboard. This module
builds one readable page from three live sources:

- ``jobs.describe()`` — what the in-process scheduler actually runs;
- Composio — trigger instances (incl. disabled ones) and connected accounts;
- the feedback store — how his one-tap ratings of proactive messages split.

It runs on boot and once a day. The page is fully regenerated each time, so
it is a view, not a place to edit. When a connection Iris depends on, or a
trigger, newly breaks, ``sync`` reports it so the scheduler can say so once —
the Gmail trigger sat silently disabled for six months before this existed.

Run by hand: ``python src/control_centre.py`` (prints the page URL).
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import jobs

log = logging.getLogger("control_centre")

USER_ID = os.environ.get("COMPOSIO_USER_ID", "user_d6iab")
# The Notion integration only sees the Macros page tree, and the API can't
# create workspace-root pages, so the page is born under Macros; Damon can
# drag it to the sidebar top level and the cached id keeps working.
PARENT_PAGE_ID = os.environ.get(
    "CONTROL_CENTRE_PARENT_ID", "3bf212bf-d261-804c-8471-dbd3cf064010"
)
STATE_PATH = Path(os.environ.get("CONTROL_CENTRE_STATE", "/data/control_centre.json"))
MEMORY_PATH = Path(os.environ.get("MEMORY_PATH", "/data/memory.md"))
PAGE_TITLE = "Iris Control Centre"

# Toolkits Iris relies on day to day. A dead one of these is worth a ping;
# anything else expired/revoked is just listed as unused.
CRITICAL_TOOLKITS = {
    "gmail": "Gmail",
    "googlecalendar": "Google Calendar",
    "notion": "Notion",
    "todoist": "Todoist",
    "googlesheets": "Google Sheets (Emma feed, work meals)",
    "splitwise": "Splitwise",
    "firecrawl": "Firecrawl",
    "google_maps": "Google Maps",
}

_TRIGGER_NAMES = {
    "GMAIL_NEW_GMAIL_MESSAGE": "New Gmail message → urgent-email check",
}

_STATUS_RANK = {"ACTIVE": 0, "INITIATED": 1, "EXPIRED": 2, "FAILED": 3, "REVOKED": 4}


def _date(iso: str | None) -> str:
    return (iso or "")[:10] or "?"


# ---------------------------------------------------------------- collection


def collect_accounts(client) -> list[dict]:
    out = []
    for a in client.connected_accounts.list(limit=200).items:
        tk = getattr(a, "toolkit", None)
        out.append(
            {
                "id": a.id,
                "toolkit": getattr(tk, "slug", None) or str(tk),
                "status": str(a.status),
                "user_id": getattr(a, "user_id", None),
                "updated": _date(str(getattr(a, "updated_at", "") or "")),
            }
        )
    return out


def collect_triggers(client) -> list[dict]:
    out = []
    for t in client.triggers.list_active(show_disabled=True).items:
        out.append(
            {
                "id": t.id,
                "name": t.trigger_name,
                "enabled": not t.disabled_at,
                "disabled_at": _date(t.disabled_at) if t.disabled_at else None,
                "config": t.trigger_config or {},
            }
        )
    return out


def pending_reminders(memory_text: str) -> list[str]:
    """Bullet lines under the memory file's '## Reminders' section, minus
    the how-it-works preamble and the '(none pending)' placeholder."""
    match = re.search(r"^## Reminders.*?$(.*?)(?=^## |\Z)", memory_text, re.M | re.S)
    if not match:
        return []
    items = []
    for line in match.group(1).splitlines():
        line = line.strip()
        if line.startswith("- ") and "none pending" not in line:
            items.append(line[2:].strip())
    return items


# ------------------------------------------------------------------ analysis


def best_accounts(accounts: list[dict]) -> dict[str, dict]:
    """One account per toolkit: the healthiest, preferring Iris's user."""
    best: dict[str, dict] = {}
    for a in accounts:
        key = a["toolkit"]
        rank = (_STATUS_RANK.get(a["status"], 9), a["user_id"] != USER_ID)
        cur = best.get(key)
        if cur is None or rank < cur["_rank"]:
            best[key] = {**a, "_rank": rank}
    return best


def attention_items(accounts: list[dict], triggers: list[dict]) -> dict[str, str]:
    """Stable key → human sentence for everything currently broken that
    matters. Keys let ``sync`` tell *new* breakage from known breakage."""
    items: dict[str, str] = {}
    best = best_accounts(accounts)
    for slug, label in CRITICAL_TOOLKITS.items():
        acct = best.get(slug)
        if acct is None:
            items[f"account:{slug}"] = f"{label}: not connected."
        elif acct["status"] != "ACTIVE":
            items[f"account:{slug}"] = (
                f"{label}: connection {acct['status'].lower()} since "
                f"{acct['updated']} — needs re-authorising."
            )
    for t in triggers:
        if not t["enabled"]:
            label = _TRIGGER_NAMES.get(t["name"], t["name"])
            items[f"trigger:{t['id']}"] = (
                f"Trigger '{label}' is OFF (disabled {t['disabled_at']}, usually "
                f"because its connection expired). Nothing reacts to these events "
                f"until it's recreated on a live connection."
            )
    return items


# ----------------------------------------------------------------- rendering


def _md_escape(text: str) -> str:
    # The page is Notion-flavoured markdown; keep table cells single-line and
    # pipe-free so one odd value can't break the table.
    return " ".join(str(text).split()).replace("|", "/")


def _table(headers: list[str], rows: list[list[str]]) -> str:
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    for row in rows:
        lines.append("| " + " | ".join(_md_escape(c) for c in row) + " |")
    return "\n".join(lines)


def render(
    *,
    now: datetime,
    job_rows: list[dict],
    accounts: list[dict],
    triggers: list[dict],
    reminders: list[str],
    attention: dict[str, str],
    ratings: list[dict],
) -> str:
    out: list[str] = []
    out.append(
        f"> Generated from Iris's code and live Composio state — last synced "
        f"**{now.strftime('%a %d %b %Y, %H:%M')}** ({jobs.TIMEZONE}). Edits here are "
        f"overwritten on the next sync; to change anything, tell Iris in Telegram."
    )

    out.append("## ⚠️ Needs attention")
    if attention:
        out.extend(f"- {msg}" for msg in attention.values())
    else:
        out.append("Nothing broken.")

    out.append("## ⏰ Scheduled jobs")
    if job_rows:
        out.append(
            _table(
                ["When", "Job", "Posts to", "What it does"],
                [[j["when"], j["name"], j["where"], j["what"]] for j in job_rows],
            )
        )
    else:
        out.append("The scheduler is switched off (SCHEDULE_INTERVAL_SECONDS unset).")

    out.append("## 🔔 Pending reminders")
    if reminders:
        out.extend(f"- {r}" for r in reminders)
    else:
        out.append("None. Ask Iris to remind you of something and it appears here "
                   "(plus a Todoist task with a due time).")

    out.append("## ⚡ Event triggers")
    if triggers:
        out.append(
            _table(
                ["Trigger", "Status", "Config"],
                [
                    [
                        _TRIGGER_NAMES.get(t["name"], t["name"]),
                        "On" if t["enabled"] else f"OFF since {t['disabled_at']}",
                        json.dumps(t["config"], separators=(",", ":")),
                    ]
                    for t in triggers
                ],
            )
        )
    else:
        out.append("None. Every proactive message comes from the scheduled jobs above.")

    out.append("## 🔌 Connections")
    best = best_accounts(accounts)
    rows = []
    for slug in sorted(best, key=lambda s: (s not in CRITICAL_TOOLKITS, s)):
        a = best[slug]
        used = "Yes" if slug in CRITICAL_TOOLKITS else "—"
        rows.append([CRITICAL_TOOLKITS.get(slug, slug), a["status"].title(), used, a["updated"]])
    out.append(_table(["Service", "Status", "Relied on", "Last changed"], rows))

    out.append("## 👍 How proactive messages are landing (last 30 days)")
    if ratings:
        out.append(
            _table(
                ["Kind", "Sent", "Useful", "Noise", "Unrated"],
                [
                    [r["kind"], str(r["sent"]), str(r["useful"]), str(r["noise"]), str(r["unrated"])]
                    for r in ratings
                ],
            )
        )
    else:
        out.append("No proactive messages logged yet. Each one now carries 👍 Useful / "
                   "👎 Noise buttons; your taps tune what gets sent.")

    out.append("## 🗂️ Where your life-admin state lives")
    out.extend(
        [
            "- **Gmail inbox (unarchived)** = waiting on you.",
            "- **Gmail label** `waiting/them` = you've replied, ball in their court.",
            "- **Todoist project** `waiting on` = commitments and chases; due date = when "
            "to chase. Anything from WhatsApp/iMessage/calls only exists if it's here.",
            "- **Notion** = Food Log, Interview Question Bank, and this page.",
            "- **Splitwise (Star Wharf)** = what Artjom owes.",
            "- Iris's own SQLite is a disposable cache. Your tools win on any conflict.",
        ]
    )
    return "\n\n".join(out)


# ---------------------------------------------------------------------- sync


def _load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text())
    except Exception:
        return {}


def _save_state(state: dict) -> None:
    try:
        STATE_PATH.write_text(json.dumps(state, indent=2))
    except Exception:
        log.exception("could not save control-centre state")


def _exec(client, slug: str, arguments: dict) -> dict:
    r = client.tools.execute(
        slug, user_id=USER_ID, dangerously_skip_version_check=True, arguments=arguments
    )
    if not r.get("successful", True):
        raise RuntimeError(f"{slug} failed: {r.get('error')}")
    return r.get("data") or {}


def _find_id(data: dict) -> str | None:
    for key in ("id", "page_id"):
        if isinstance(data.get(key), str):
            return data[key]
    for nested in ("response_data", "page", "data"):
        if isinstance(data.get(nested), dict):
            found = _find_id(data[nested])
            if found:
                return found
    return None


def sync(client=None, feedback=None, now: datetime | None = None) -> dict:
    """Regenerate the page. Returns ``{"page_id", "url", "new_attention"}``
    where ``new_attention`` lists sentences for breakage not seen on the
    previous sync. The very first sync records a silent baseline."""
    if client is None:
        from composio import Composio

        client = Composio()
    now = now or datetime.now(ZoneInfo(jobs.TIMEZONE))

    accounts = collect_accounts(client)
    triggers = collect_triggers(client)
    try:
        reminders = pending_reminders(MEMORY_PATH.read_text())
    except Exception:
        reminders = []
    attention = attention_items(accounts, triggers)
    ratings = feedback.stats(30) if feedback is not None else []

    markdown = render(
        now=now,
        job_rows=jobs.describe(),
        accounts=accounts,
        triggers=triggers,
        reminders=reminders,
        attention=attention,
        ratings=ratings,
    )

    state = _load_state()
    page_id = state.get("page_id")
    if page_id:
        try:
            _exec(
                client,
                "NOTION_UPDATE_PAGE_MARKDOWN",
                {
                    "page_id": page_id,
                    "type": "replace_content",
                    "replace_content": {"new_str": markdown},
                },
            )
        except Exception:
            # Page deleted or access revoked: fall through and recreate.
            log.exception("control-centre update failed; recreating page")
            page_id = None
    if not page_id:
        data = _exec(
            client,
            "NOTION_CREATE_NOTION_PAGE",
            {
                "parent_id": PARENT_PAGE_ID,
                "title": PAGE_TITLE,
                "icon": "🛰️",
                "markdown": markdown,
            },
        )
        page_id = _find_id(data)
        if not page_id:
            raise RuntimeError(f"page created but no id in response: {str(data)[:300]}")

    first_run = "attention" not in state
    known = set(state.get("attention", []))
    new_attention = [] if first_run else [
        msg for key, msg in attention.items() if key not in known
    ]
    url = f"https://www.notion.so/{page_id.replace('-', '')}"
    _save_state({**state, "page_id": page_id, "url": url, "attention": sorted(attention)})
    return {"page_id": page_id, "url": url, "new_attention": new_attention}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    from feedback import Feedback

    fb = Feedback(os.environ.get("HISTORY_DB_PATH", "/data/history.sqlite"))
    print(json.dumps(sync(feedback=fb), indent=2))
