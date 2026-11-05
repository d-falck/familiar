import unittest
from unittest.mock import patch

import claude_backend


class _EmptyStream:
    def __aiter__(self):
        return self

    async def __anext__(self):
        raise StopAsyncIteration


class _FakeClient:
    instance = None

    def __init__(self, options):
        self.options = options
        self.query_text = None
        _FakeClient.instance = self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def query(self, text):
        self.query_text = text

    def receive_response(self):
        return _EmptyStream()


class ClaudePromptTransportTest(unittest.IsolatedAsyncioTestCase):
    async def test_large_system_prompt_is_sent_over_stdin_not_process_args(self):
        large_prompt = "memory " * 40_000

        with (
            patch.object(claude_backend, "ClaudeSDKClient", _FakeClient),
            patch.object(claude_backend, "ClaudeAgentOptions", lambda **kwargs: kwargs),
        ):
            await claude_backend.run(
                [{"role": "user", "content": "hello"}],
                system_prompt=large_prompt,
                model="test-model",
                mcp_servers={},
            )

        client = _FakeClient.instance
        self.assertNotIn(large_prompt, client.options["system_prompt"])
        self.assertIn(large_prompt, client.query_text)
        self.assertIn("hello", client.query_text)


if __name__ == "__main__":
    unittest.main()
