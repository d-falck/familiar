"""The daily spend digest must post exactly once per day: retries across the
07:00-11:00 window and restarts must never double-post, an unsynced feed must
defer (not post a £0 day), and the last attempt must say the feed stalled
rather than going silent."""

from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import bot
from history import History


class _Proc:
    def __init__(self, out: str, rc: int = 0):
        self._out, self.returncode = out.encode(), rc

    async def communicate(self):
        return self._out, b""


class FinanceDigestTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.history = History(os.path.join(self.tmp.name, "h.sqlite"))

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, out: str, *, final: bool):
        sent = AsyncMock()
        with patch.object(
            bot.asyncio, "create_subprocess_exec", AsyncMock(return_value=_Proc(out))
        ) as spawn, patch.object(bot, "send_rated", sent):
            ok = asyncio.run(
                bot._send_daily_finance_digest(
                    None, self.history, "2026-11-09", None, final_attempt=final
                )
            )
        return ok, spawn, sent

    def test_header_matches_script_format(self):
        self.assertEqual(bot._digest_header("2026-11-09"), "Mon 9 Nov:")

    def test_sends_once_then_idempotent(self):
        ok, _, sent = self._run("Mon 9 Nov: £155.87  (typical)", final=False)
        self.assertTrue(ok)
        sent.assert_awaited_once()
        ok, spawn, sent = self._run("Mon 9 Nov: £155.87  (typical)", final=False)
        self.assertTrue(ok)
        spawn.assert_not_awaited()
        sent.assert_not_awaited()

    def test_unsynced_feed_defers_without_sending(self):
        ok, _, sent = self._run(
            "!! no data for 2026-11-09; feed syncs to 2026-11-08", final=False
        )
        self.assertFalse(ok)
        sent.assert_not_awaited()

    def test_final_attempt_posts_stall_note(self):
        ok, _, sent = self._run(
            "!! no data for 2026-11-09; feed syncs to 2026-11-08", final=True
        )
        self.assertTrue(ok)
        text = sent.await_args.kwargs["text"]
        self.assertIn("hasn't synced", text)
        self.assertFalse(text.startswith("!!"))

    def test_unrelated_reply_in_topic_does_not_block(self):
        self.history.add_assistant(
            bot.FINANCE_DIGEST_CHAT_ID,
            "Monday's digest lands at 09:00.",
            thread_id=bot.FINANCE_DIGEST_THREAD_ID,
        )
        ok, _, sent = self._run("Mon 9 Nov: £155.87", final=False)
        self.assertTrue(ok)
        sent.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
