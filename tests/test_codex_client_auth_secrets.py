"""Codex app-server client: authentication requests never retain or repeat secrets."""
from __future__ import annotations

import asyncio
import unittest

from hyadex.providers.codex.app_server import CodexAppServerClient, CodexAppServerRequestError, CodexAppServerTimeout
from tests.test_codex_app_server import FakeProcessFactory, NO_RESPONSE


SECRET = "sk-synthetic-auth-test-only-not-a-real-key"


class CodexAuthTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_auth_write_timeout_does_not_retire_shared_provider_or_repeat_request(self):
        for method, params in (("account/read", {"refreshToken": False}),
                ("account/login/start", {"type": "apiKey", "apiKey": SECRET})):
            factory = FakeProcessFactory()
            client = CodexAppServerClient("synthetic-codex", cwd="/tmp", env_factory=lambda: {},
                process_factory=factory, request_timeout=1)
            self.addAsyncCleanup(client.close)
            await client.start()
            async def blocked_drain():
                await asyncio.Event().wait()
            factory.process.stdin.drain = blocked_drain
            with self.assertRaises(CodexAppServerTimeout):
                await client.request(method, params, timeout=0.01)
            self.assertIsNone(factory.process.returncode)
            self.assertTrue(client.ready)
            self.assertEqual(len(factory.calls), 1)
            self.assertEqual(len([item for item in factory.process.messages if item.get("method") == method]), 1)

    async def test_auth_errors_notifications_and_delayed_stderr_do_not_retain_secret(self):
        factory = FakeProcessFactory()
        client = CodexAppServerClient("synthetic-codex", cwd="/tmp", env_factory=lambda: {},
            process_factory=factory, request_timeout=1)
        self.addAsyncCleanup(client.close)
        seen = []
        client.add_notification_handler(lambda item: seen.append(item))
        def reject(message):
            factory.process.feed_stderr(SECRET)
            factory.process.feed({"method": "account/login/completed", "params": {"error": SECRET}})
            factory.process.feed({"id": message["id"], "error": {"code": -1, "message": SECRET, "data": SECRET}})
            return NO_RESPONSE
        factory.process.responders["account/login/start"] = reject
        with self.assertRaises(CodexAppServerRequestError) as raised:
            await client.request("account/login/start", {"type": "apiKey", "apiKey": SECRET})
        self.assertNotIn(SECRET, str(raised.exception))
        self.assertNotIn(SECRET, repr(raised.exception.error))
        factory.process.feed_stderr("delayed " + SECRET)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        self.assertEqual(client.stderr_tail, [])
        self.assertEqual(seen, [])
        rate_limits = {"method": "account/rateLimits/updated", "params": {"rateLimits": {"limitId": "codex"}}}
        client._route_notification(rate_limits)
        self.assertEqual(seen, [rate_limits])
        self.assertEqual(len([item for item in factory.process.messages if item.get("method") == "account/login/start"]), 1)


if __name__ == "__main__":
    unittest.main()
