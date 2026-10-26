"""Resume/import naming uses existing provider metadata, never a model call."""
from __future__ import annotations

import asyncio
import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import agent_server as server
from tests.native_transcripts import write_claude_transcript, write_codex_transcript


class ResumeTitleCreation(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store = server.SessionStore()
        self.store.save = AsyncMock()
        self.enterContext(patch.object(server, "STORE", self.store))
        self.ensure_dirs = self.enterContext(patch.object(server, "ensure_dirs"))
        self.enterContext(patch.object(server, "append_event", new_callable=AsyncMock))
        self.enterContext(patch.object(server, "ensure_codex_thread_not_pending_fork_cleanup"))
        self.native_reader = server.read_native_session_title
        self.reader = self.enterContext(patch.object(server, "read_native_session_title", return_value="Existing native title"))
        self.generator = self.enterContext(patch.object(server.title_generation, "generate_title", new_callable=AsyncMock))

    async def create(self, backend="claude", **kwargs):
        return await self.store.create(server.CreateSessionRequest(
            backend=backend, provider_session_id="native-chat", cwd="/work", **kwargs,
        ))

    async def test_all_providers_use_native_title_before_returning_resume_response(self):
        for backend in ("claude", "codex"):
            for title in (None, "New chat", f"Resumed {backend.title()} native-c"):
                with self.subTest(backend=backend, title=title):
                    created = await self.create(backend, title=title)
                    self.assertEqual(created["title"], "Existing native title")
                    self.assertEqual(created["_title_source"], "provider")
                    self.assertEqual(created["_title_auto_value"], created["title"])
                    self.assertEqual(created["session_id"], "native-chat")
        self.generator.assert_not_awaited()

    async def test_explicit_name_always_wins(self):
        for title in ("My manual name", "Resumed Claude other-id", "Resumed chat"):
            created = await self.create(title=title)
            self.assertEqual(created["title"], title)
            self.assertEqual(created["_title_source"], "manual")
        self.reader.assert_not_called()

    async def test_missing_unreadable_or_slow_metadata_keeps_resume_working(self):
        for error in (None, OSError("unavailable"), sqlite3.DatabaseError("unknown schema"), TimeoutError()):
            self.reader.return_value = None
            self.reader.side_effect = error
            created = await self.create()
            self.assertEqual(created["title"], "Resumed Claude native-c")
            self.assertEqual(created["_title_source"], "placeholder")

    async def test_new_chats_forks_and_staged_imports_do_not_probe_metadata(self):
        await self.store.create(server.CreateSessionRequest(backend="claude"))
        for flags in ({"parent_id": "parent"}, {"initializing_fork": True}, {"initializing_import": True}):
            await self.store.create(server.CreateSessionRequest(backend="claude", provider_session_id="native-chat"), **flags)
        self.reader.assert_not_called()

    async def test_placeholder_wording_is_manual_when_user_renames_existing_chat(self):
        created = await self.create()
        await self.store.update(created["id"], {"title": "Resumed Claude native-c"})
        self.assertEqual(created["_title_source"], "manual")
        self.reader.reset_mock()
        await server.refresh_native_session_title(created["id"])
        self.reader.assert_not_called()

    async def test_resume_endpoint_returns_native_title_without_import_or_model_call(self):
        with patch.object(server, "import_session_history", new_callable=AsyncMock) as history:
            response = await server.create_session(server.CreateSessionRequest(
                backend="claude", provider_session_id="native-chat", cwd="/work",
                title="Resumed Claude native-c", import_history=False,
            ))
            self.assertEqual(response["session"]["title"], "Existing native title")
            history.assert_not_awaited()
        self.generator.assert_not_awaited()

    async def test_real_provider_metadata_flows_into_created_title(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.reader.side_effect = self.native_reader
            projects = root / "claude"
            transcript = projects / "project" / "native-chat.jsonl"
            write_claude_transcript(transcript, cwd="/work", first_user_text="First prompt")
            with transcript.open("a") as stream:
                stream.write(json.dumps({"type": "ai-title", "sessionId": "native-chat", "aiTitle": "Claude title"}) + "\n")
            index = root / "session_index.jsonl"
            index.write_text(json.dumps({"id": "native-chat", "thread_name": "Codex title"}) + "\n")
            with patch.object(server, "CLAUDE_PROJECTS_ROOT", projects), patch.object(server, "CODEX_SESSION_INDEX_PATH", index):
                for backend in ("claude", "codex"):
                    created = await self.create(backend, title=f"Resumed {backend.title()} native-c")
                    self.assertEqual(created["title"], f"{backend.title()} title")
        self.generator.assert_not_awaited()

    async def test_actual_deadline_does_not_block_resume(self):
        release = threading.Event()
        self.reader.side_effect = lambda _session: release.wait(3) and "Too late"
        try:
            created = await asyncio.wait_for(self.create(), timeout=2)
            self.assertEqual(created["title"], "Resumed Claude native-c")
        finally:
            release.set()
        self.assertEqual(created["_title_source"], "placeholder")

    async def test_cancelled_resume_does_not_create_orphaned_session(self):
        entered, release = threading.Event(), threading.Event()
        def pending(_session):
            entered.set()
            release.wait(3)
            return "Late result"
        self.reader.side_effect = pending
        task = asyncio.create_task(self.create())
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 2))
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        finally:
            release.set()
        self.assertEqual(self.store.sessions, {})
        self.ensure_dirs.assert_not_called()

    async def test_missing_title_can_be_adopted_later_without_running_provider(self):
        self.reader.return_value = None
        created = await self.create()
        self.reader.return_value = "Now available"
        await server.refresh_native_session_title(created["id"])
        self.assertEqual(created["title"], "Now available")
        self.assertEqual(created["_title_source"], "provider")
        self.generator.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
