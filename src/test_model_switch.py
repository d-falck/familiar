"""Tests for the no-agent `/model` switch.

Run: `uv run python -m unittest discover -s src -p 'test_*.py'`
"""

import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))

import model_switch  # noqa: E402


class ResolveTests(unittest.TestCase):
    def test_alias(self):
        self.assertEqual(model_switch.resolve(["opus"]), ("claude", "claude-opus-5"))
        self.assertEqual(model_switch.resolve(["OPUS"]), ("claude", "claude-opus-5"))

    def test_raw_model_id_infers_backend(self):
        self.assertEqual(
            model_switch.resolve(["claude-opus-4-5"]), ("claude", "claude-opus-4-5")
        )
        self.assertEqual(model_switch.resolve(["gpt-5.2"]), ("codex", "gpt-5.2"))

    def test_explicit_backend_and_model(self):
        self.assertEqual(
            model_switch.resolve(["codex", "gpt-5.2"]), ("codex", "gpt-5.2")
        )

    def test_bare_backend_is_rejected_with_guidance(self):
        with self.assertRaises(ValueError) as ctx:
            model_switch.resolve(["claude"])
        self.assertIn("is a backend, not a model", str(ctx.exception))

    def test_typo_is_rejected_rather_than_wedging_later_turns(self):
        with self.assertRaises(ValueError):
            model_switch.resolve(["opsu"])

    def test_unknown_backend_is_rejected(self):
        with self.assertRaises(ValueError):
            model_switch.resolve(["gemini", "gemini-3-pro"])

    def test_too_many_arguments(self):
        with self.assertRaises(ValueError):
            model_switch.resolve(["codex", "gpt-5.2", "extra"])


class PersistenceTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = model_switch.override_path(
            str(Path(self.dir.name, "history.sqlite"))
        )

    def test_round_trip(self):
        self.assertIsNone(model_switch.load_override(self.path))
        model_switch.save_override(self.path, "claude", "claude-opus-5")
        self.assertEqual(
            model_switch.load_override(self.path), ("claude", "claude-opus-5")
        )
        model_switch.clear_override(self.path)
        self.assertIsNone(model_switch.load_override(self.path))

    def test_clearing_a_missing_override_is_fine(self):
        model_switch.clear_override(self.path)  # must not raise

    def test_corrupt_file_is_ignored_rather_than_crashing_boot(self):
        self.path.write_text("{not json")
        self.assertIsNone(model_switch.load_override(self.path))

    def test_invalid_backend_in_file_is_ignored(self):
        self.path.write_text(json.dumps({"backend": "gemini", "model": "x"}))
        self.assertIsNone(model_switch.load_override(self.path))


class _FakeMessage:
    def __init__(self):
        self.sent = []
        self.chat_id = 42
        self.from_user = SimpleNamespace(username="damon")

    async def reply_text(self, body, parse_mode=None):
        self.sent.append(body)


def _run_command(bot_data, args):
    import bot as bot_module

    message = _FakeMessage()
    update = SimpleNamespace(effective_message=message)
    context = SimpleNamespace(
        application=SimpleNamespace(bot_data=bot_data), args=args
    )
    asyncio.run(bot_module.on_model(update, context))
    return "\n".join(message.sent)


class CommandTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.cfg = {"backend": "claude", "model": "claude-fable-5-1"}
        self.override_path = model_switch.override_path(
            str(Path(self.dir.name, "history.sqlite"))
        )
        self.bot_data = {
            "cfg": self.cfg,
            "model_defaults": ("claude", "claude-fable-5-1"),
            "model_override_path": self.override_path,
        }

    def test_no_args_reports_current_model(self):
        out = _run_command(self.bot_data, [])
        self.assertIn("`claude-fable-5-1`", out)

    def test_switch_updates_shared_cfg_in_place(self):
        # The scheduler and webhook hold a reference to this same dict, so an
        # in-place update is what makes the switch global.
        out = _run_command(self.bot_data, ["opus"])
        self.assertEqual(self.cfg["model"], "claude-opus-5")
        self.assertEqual(self.cfg["backend"], "claude")
        self.assertIn("`claude-opus-5`", out)
        self.assertEqual(
            model_switch.load_override(self.override_path),
            ("claude", "claude-opus-5"),
        )

    def test_reset_restores_default_and_drops_the_file(self):
        _run_command(self.bot_data, ["opus"])
        _run_command(self.bot_data, ["reset"])
        self.assertEqual(self.cfg["model"], "claude-fable-5-1")
        self.assertFalse(self.override_path.exists())

    def test_bad_input_leaves_the_running_model_untouched(self):
        out = _run_command(self.bot_data, ["opsu"])
        self.assertEqual(self.cfg["model"], "claude-fable-5-1")
        self.assertIn("unknown model", out.replace("\\", ""))

    def test_codex_switch_warns_when_the_key_is_missing(self):
        previous = os.environ.pop("OPENAI_API_KEY", None)
        try:
            out = _run_command(self.bot_data, ["codex"])
        finally:
            if previous is not None:
                os.environ["OPENAI_API_KEY"] = previous
        self.assertEqual(self.cfg["backend"], "codex")
        self.assertIn("OPENAI_API_KEY", out)


class LimitDetectionTests(unittest.TestCase):
    def test_detects_the_subscription_limit_wording(self):
        import bot as bot_module

        self.assertTrue(
            bot_module._is_limit_message(
                "You've reached your Fable limit. Switch to another model to continue."
            )
        )
        self.assertTrue(
            bot_module._is_limit_message(RuntimeError("Claude usage limit reached"))
        )
        self.assertTrue(
            bot_module._is_limit_message(
                RuntimeError("Your credit balance is too low to access the API")
            )
        )

    def test_does_not_fire_on_ordinary_replies(self):
        import bot as bot_module

        self.assertFalse(
            bot_module._is_limit_message("Booked the viewing for Tuesday at 6pm.")
        )
        self.assertFalse(
            bot_module._is_limit_message("I hit an internal error on that turn.")
        )

    def test_limit_exception_gets_the_switch_hint(self):
        import bot as bot_module

        text = bot_module._user_error_message(
            RuntimeError("Claude usage limit reached"), "claude-fable-5-1"
        )
        self.assertIn("/model opus", text)

    def test_hint_never_suggests_the_model_that_just_failed(self):
        import bot as bot_module

        text = bot_module._user_error_message(
            RuntimeError("Claude usage limit reached"), "claude-opus-5"
        )
        self.assertNotIn("/model opus", text)
        self.assertIn("/model sonnet", text)


class SystemPromptTests(unittest.TestCase):
    """The agent should know what it's running on, and that the user (not it)
    does the switching."""

    def _build(self, **kwargs):
        from prompt import build_system_prompt

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return build_system_prompt(
            persona_path="prompts/assistant.md",
            memory_path=str(Path(tmp.name, "memory.md")),
            history_path=str(Path(tmp.name, "history.sqlite")),
            **kwargs,
        )

    def test_names_the_running_model_and_the_command(self):
        prompt = self._build(backend="claude", model="claude-opus-5")
        self.assertIn("claude-opus-5", prompt)
        self.assertIn("/model reset", prompt)

    def test_section_is_omitted_when_no_model_is_passed(self):
        self.assertNotIn("Which model you're running on", self._build())


if __name__ == "__main__":
    unittest.main()
