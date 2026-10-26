"""Resuming a native conversation another local instance owns is refused without state."""
from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from fastapi import HTTPException

import local_session_ownership
from tests.session_store_fixture import make_namespace


class ResumeOwnershipTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="session-resume-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.ns = make_namespace(self.root)

    async def create(self, **kwargs):
        return (await self.ns["create_session"](self.ns["CreateSessionRequest"](**kwargs)))["session"]

    async def test_resume_uses_isolated_ownership_guard_and_releases_it_after_rejection(self):
        peer = local_session_ownership.Instance("peer", self.root)
        peer.config.mkdir(parents=True)
        (peer.config / "env").write_text("")
        peer.state.mkdir(parents=True)
        (peer.state / "sessions.json").write_text(json.dumps({
            "peer-chat": {"backend": "codex", "codex_thread_id": "owned-thread"},
        }))
        with self.assertRaises(HTTPException) as caught:
            await self.create(codex_thread_id="owned-thread", import_history=False)
        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(self.ns["STORE"].sessions, {})
        self.ns["STORE"].save.assert_not_awaited()
        resumed = await self.create(codex_thread_id="available-thread", import_history=False)
        self.assertEqual(resumed["codex_thread_id"], "available-thread")
        self.ns["read_native_session_title"].assert_called_once()
        self.ns["import_session_history"].assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
