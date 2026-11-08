"""Tests for the control-centre page, the job registry and one-tap feedback.

The things worth pinning: breakage alerts fire once per *new* problem (never
on the first sync, never again for a known one), a dead account with a live
replacement is not "broken", and ratings round-trip into the prompt summary.
"""

from __future__ import annotations

import importlib
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import control_centre
import feedback as feedback_mod
import jobs


def _acct(toolkit, status, user="user_d6iab", updated="2026-05-12"):
    return {"id": f"ca_{toolkit}_{status}", "toolkit": toolkit, "status": status,
            "user_id": user, "updated": updated}


class AttentionTests(unittest.TestCase):
    def test_expired_with_live_replacement_is_fine(self):
        accts = [_acct(s, "ACTIVE") for s in control_centre.CRITICAL_TOOLKITS]
        accts.append(_acct("gmail", "EXPIRED"))
        self.assertEqual(control_centre.attention_items(accts, []), {})

    def test_only_expired_critical_account_is_flagged(self):
        accts = [_acct(s, "ACTIVE") for s in control_centre.CRITICAL_TOOLKITS if s != "gmail"]
        accts.append(_acct("gmail", "EXPIRED"))
        items = control_centre.attention_items(accts, [])
        self.assertEqual(list(items), ["account:gmail"])
        self.assertIn("expired", items["account:gmail"])

    def test_unused_toolkit_never_flagged(self):
        accts = [_acct(s, "ACTIVE") for s in control_centre.CRITICAL_TOOLKITS]
        accts.append(_acct("spotify", "REVOKED"))
        self.assertEqual(control_centre.attention_items(accts, []), {})

    def test_disabled_trigger_flagged(self):
        accts = [_acct(s, "ACTIVE") for s in control_centre.CRITICAL_TOOLKITS]
        trig = {"id": "ti_1", "name": "GMAIL_NEW_GMAIL_MESSAGE", "enabled": False,
                "disabled_at": "2026-05-12", "config": {}}
        items = control_centre.attention_items(accts, [trig])
        self.assertIn("trigger:ti_1", items)
        self.assertIn("OFF", items["trigger:ti_1"])


class ReminderParsingTests(unittest.TestCase):
    MEM = (
        "## User\n- x\n\n## Reminders (pending — check)\nHow reminders work: blah\n"
        "- 2026-11-09 09:00 — call the dentist\n- _(none pending)_\n\n## Other\n- y\n"
    )

    def test_parses_only_reminder_bullets(self):
        self.assertEqual(
            control_centre.pending_reminders(self.MEM),
            ["2026-11-09 09:00 — call the dentist"],
        )

    def test_none_pending(self):
        mem = "## Reminders (pending)\n- _(none pending)_\n## Next\n"
        self.assertEqual(control_centre.pending_reminders(mem), [])


class RenderTests(unittest.TestCase):
    def test_sections_and_escaping(self):
        md = control_centre.render(
            now=datetime(2026, 11, 8, 7, 0),
            job_rows=[{"when": "08:00", "name": "Triage | daily", "where": "DM", "what": "x\ny"}],
            accounts=[_acct("gmail", "ACTIVE")],
            triggers=[],
            reminders=[],
            attention={},
            ratings=[{"kind": "morning triage", "sent": 3, "useful": 1, "noise": 1, "unrated": 1}],
        )
        for heading in ("Needs attention", "Scheduled jobs", "Event triggers",
                        "Connections", "How proactive messages are landing"):
            self.assertIn(heading, md)
        self.assertIn("Triage / daily", md)  # pipe can't break the table
        self.assertIn("| x y |", md)  # newline collapsed inside a cell
        self.assertIn("Nothing broken.", md)


class JobsTests(unittest.TestCase):
    def test_sorted_and_hourly_first(self):
        with patch.dict(os.environ, {"SCHEDULE_INTERVAL_SECONDS": "3600"}):
            importlib.reload(jobs)
            rows = jobs.describe()
        hours = [r["hour"] for r in rows]
        self.assertEqual(hours, sorted(hours))
        self.assertEqual(rows[0]["hour"], -1)
        self.assertIn("Morning triage", [r["name"] for r in rows])

    def test_scheduler_off_means_no_jobs(self):
        env = {k: v for k, v in os.environ.items() if k != "SCHEDULE_INTERVAL_SECONDS"}
        with patch.dict(os.environ, env, clear=True):
            importlib.reload(jobs)
            self.assertEqual(jobs.describe(), [])
        importlib.reload(jobs)


class _FakeClient:
    """Just enough of the Composio client for sync()."""

    def __init__(self, accounts, triggers):
        self.calls = []
        self.connected_accounts = SimpleNamespace(
            list=lambda limit: SimpleNamespace(items=accounts)
        )
        self.triggers = SimpleNamespace(
            list_active=lambda show_disabled: SimpleNamespace(items=triggers)
        )
        self.tools = SimpleNamespace(execute=self._execute)

    def _execute(self, slug, user_id, dangerously_skip_version_check, arguments):
        self.calls.append(slug)
        return {"successful": True, "data": {"id": "page-123"}}


def _sdk_acct(slug, status):
    return SimpleNamespace(id=f"ca_{slug}", toolkit=SimpleNamespace(slug=slug),
                           status=status, user_id="user_d6iab", updated_at="2026-11-01T00:00:00Z")


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name) / "cc.json"
        self.patches = [
            patch.object(control_centre, "STATE_PATH", self.state),
            patch.object(control_centre, "MEMORY_PATH", Path(self.tmp.name) / "none.md"),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def _client(self, gmail_status="ACTIVE"):
        accts = [_sdk_acct(s, "ACTIVE") for s in control_centre.CRITICAL_TOOLKITS if s != "gmail"]
        accts.append(_sdk_acct("gmail", gmail_status))
        return _FakeClient(accts, [])

    def test_first_sync_is_silent_baseline_then_alerts_once(self):
        now = datetime(2026, 11, 8, 7, 0)
        first = control_centre.sync(self._client("EXPIRED"), now=now)
        self.assertEqual(first["new_attention"], [])  # baseline, even though broken
        self.assertEqual(first["page_id"], "page-123")

        # Fixed, then breaks again → exactly one alert, then quiet.
        control_centre.sync(self._client("ACTIVE"), now=now)
        broke = control_centre.sync(self._client("EXPIRED"), now=now)
        self.assertEqual(len(broke["new_attention"]), 1)
        again = control_centre.sync(self._client("EXPIRED"), now=now)
        self.assertEqual(again["new_attention"], [])

    def test_updates_existing_page_instead_of_creating(self):
        now = datetime(2026, 11, 8, 7, 0)
        control_centre.sync(self._client(), now=now)
        client = self._client()
        control_centre.sync(client, now=now)
        self.assertEqual(client.calls, ["NOTION_UPDATE_PAGE_MARKDOWN"])


class FeedbackTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.fb = feedback_mod.Feedback(str(Path(self.tmp.name) / "h.sqlite"))

    def tearDown(self):
        self.tmp.cleanup()

    def test_parse_callback(self):
        self.assertEqual(feedback_mod.parse_callback("fb:12:u"), (12, "useful"))
        self.assertEqual(feedback_mod.parse_callback("fb:12:n"), (12, "noise"))
        for bad in ("fb:done", "fb:x:u", "fb:1:z", "", "model:opus", "fb:1:u:extra"):
            self.assertIsNone(feedback_mod.parse_callback(bad))

    def test_callback_data_fits_telegram_limit(self):
        # Telegram caps callback_data at 64 bytes.
        kb = feedback_mod.keyboard(10**12)
        for button in kb.inline_keyboard[0]:
            self.assertLessEqual(len(button.callback_data.encode()), 64)

    def test_round_trip_and_summary(self):
        self.assertEqual(self.fb.recent_summary(), "")
        a = self.fb.record_sent(1, 0, "morning triage", "Housekeeper comes today " * 30)
        b = self.fb.record_sent(1, 357, "spend digest", "Sat: £25")
        self.fb.record_sent(1, 0, "hourly check", "Leave by 17:15")
        self.assertEqual(self.fb.rate(a, "noise")["kind"], "morning triage")
        self.fb.rate(b, "useful")
        self.assertIsNone(self.fb.rate(999, "useful"))

        stats = {r["kind"]: r for r in self.fb.stats()}
        self.assertEqual(stats["morning triage"]["noise"], 1)
        self.assertEqual(stats["spend digest"]["useful"], 1)
        self.assertEqual(stats["hourly check"]["unrated"], 1)

        summary = self.fb.recent_summary()
        self.assertIn("[NOISE] morning triage", summary)
        self.assertIn("[USEFUL] spend digest", summary)
        self.assertNotIn("hourly check", summary)  # unrated never shown
        # Excerpts are bounded so the prompt can't balloon.
        self.assertLess(max(len(l) for l in summary.splitlines()), 340)


class SendRatedTests(unittest.IsolatedAsyncioTestCase):
    async def test_attaches_buttons_and_stores_message_id(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        fb = feedback_mod.Feedback(str(Path(tmp.name) / "h.sqlite"))
        sent = []

        class Bot:
            async def send_message(self, **kw):
                sent.append(kw)
                return SimpleNamespace(message_id=4242)

        await feedback_mod.send_rated(Bot(), fb, chat_id=5, thread_id=357,
                                      kind="spend digest", text="hello")
        self.assertEqual(sent[0]["message_thread_id"], 357)
        self.assertIsNotNone(sent[0]["reply_markup"])
        row = fb._conn.execute("SELECT message_id FROM proactive").fetchone()
        self.assertEqual(row[0], 4242)

    async def test_no_store_means_plain_send(self):
        sent = []

        class Bot:
            async def send_message(self, **kw):
                sent.append(kw)
                return SimpleNamespace(message_id=1)

        await feedback_mod.send_rated(Bot(), None, chat_id=5, kind="x", text="hi")
        self.assertNotIn("reply_markup", sent[0])
        self.assertNotIn("message_thread_id", sent[0])


if __name__ == "__main__":
    unittest.main()
