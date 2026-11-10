"""Single source of truth for Iris's scheduled jobs.

The scheduler in ``bot.py`` reads its hours and destinations from here, and
``control_centre.py`` renders the same values to the Notion page Damon can
open. Keeping both on one module means the page can't drift from what
actually runs: change an hour here (or its env var), deploy, and the page
follows on the next sync.
"""

from __future__ import annotations

import os

TIMEZONE = os.environ.get("TIMEZONE", "UTC")
SCHEDULE_INTERVAL_SECONDS = os.environ.get("SCHEDULE_INTERVAL_SECONDS")

TRIAGE_HOUR = int(os.environ.get("TRIAGE_HOUR", "8"))

MACRO_PROMPT_HOUR = int(os.environ.get("MACRO_PROMPT_HOUR", "20"))
MACRO_PROMPT_CHAT_ID = int(os.environ.get("MACRO_PROMPT_CHAT_ID", "-1003945099460"))
MACRO_PROMPT_THREAD_ID = int(os.environ.get("MACRO_PROMPT_THREAD_ID", "169"))
MACRO_PROMPT_SCRIPT = os.environ.get(
    "MACRO_PROMPT_SCRIPT", "/data/macros/daily_prompt.py"
)

FINANCE_DIGEST_HOUR = int(os.environ.get("FINANCE_DIGEST_HOUR", "7"))
# The Emma feed usually has yesterday by early morning, but not always: if the
# day hasn't synced (or the run errors) the digest retries each hour until this
# hour, then posts a one-line "feed hasn't synced" note instead of vanishing.
FINANCE_DIGEST_RETRY_UNTIL_HOUR = int(
    os.environ.get("FINANCE_DIGEST_RETRY_UNTIL_HOUR", "11")
)
FINANCE_DIGEST_CHAT_ID = int(
    os.environ.get("FINANCE_DIGEST_CHAT_ID", "-1003945099460")
)
FINANCE_DIGEST_THREAD_ID = int(os.environ.get("FINANCE_DIGEST_THREAD_ID", "357"))
FINANCE_DIGEST_SCRIPT = os.environ.get(
    "FINANCE_DIGEST_SCRIPT", "/data/finance/daily_digest.py"
)

# The Notion control-centre page is re-rendered on boot and once a day here.
CONTROL_CENTRE_HOUR = int(os.environ.get("CONTROL_CENTRE_HOUR", "7"))

# Human names for the workspace forum topics jobs post into.
_TOPIC_NAMES = {169: "Macros topic", 357: "Finance topic"}


def _where(thread_id: int) -> str:
    return _TOPIC_NAMES.get(thread_id, f"topic {thread_id}")


def describe() -> list[dict]:
    """Every scheduled job as plain data, in firing order through the day.

    Returns an empty list when the scheduler is switched off, so the page
    says so rather than listing jobs that never run.
    """
    if not SCHEDULE_INTERVAL_SECONDS:
        return []
    every = int(SCHEDULE_INTERVAL_SECONDS)
    cadence = "Every hour" if every == 3600 else f"Every {every // 60} min"
    rows = [
        {
            "hour": -1,
            "when": f"{cadence}, on the boundary",
            "name": "Travel, deadline and reminder check",
            "where": "DM — silent unless something is due",
            "what": (
                "Departure reminders ~1.5h before in-person events, pending "
                "reminders from memory, genuine same-day deadlines. Anything "
                "else stays silent."
            ),
        },
        {
            "hour": CONTROL_CENTRE_HOUR,
            "when": f"{CONTROL_CENTRE_HOUR:02d}:00 (and on every restart)",
            "name": "Control-centre refresh",
            "where": "This page; DM only if a connection or trigger newly breaks",
            "what": "Re-renders this page from code + live Composio state.",
        },
        {
            "hour": TRIAGE_HOUR,
            "when": f"{TRIAGE_HOUR:02d}:00",
            "name": "Morning triage",
            "where": "DM — silent most days",
            "what": (
                "Only a genuine same-day actionable: hard deadline, someone "
                "blocked on you, a time-critical change."
            ),
        },
        {
            "hour": FINANCE_DIGEST_HOUR,
            "when": f"{FINANCE_DIGEST_HOUR:02d}:00",
            "name": "Daily spend digest",
            "where": _where(FINANCE_DIGEST_THREAD_ID),
            "what": (
                "Yesterday's spend vs typical, what was avoidable, month-to-date. "
                f"Retries hourly until {FINANCE_DIGEST_RETRY_UNTIL_HOUR:02d}:00 "
                "if the bank feed hasn't synced yet."
            ),
        },
        {
            "hour": MACRO_PROMPT_HOUR,
            "when": f"{MACRO_PROMPT_HOUR:02d}:00",
            "name": "Macro check-in",
            "where": _where(MACRO_PROMPT_THREAD_ID),
            "what": "Pre-filled work meals + what's logged; asks what you actually ate.",
        },
    ]
    return sorted(rows, key=lambda r: r["hour"])
