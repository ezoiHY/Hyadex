"""Retired runtimes cannot execute, while persisted timelines remain readable."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

import httpx
from fastapi import HTTPException
from pydantic import ValidationError

import agent_server as server
from hyadex.groups import ParticipantSpec


class RetiredProviderTests(unittest.IsolatedAsyncioTestCase):
    backend = "gemini"
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="retired-provider-")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.store = server.SessionStore()
        self.jobs = server.JobStore()
        self.session_id = "sess_retired_provider"
        self.session = {
            "id": self.session_id, "backend": self.backend, "title": "Saved conversation",
            "session_id": "old-native-id", self.backend + "_session_id": "old-native-id",
            "backend_locked": True, "cwd": str(self.root), "folder": "General",
            "created_at": "2026-01-01T00:00:00+00:00", "updated_at": "2026-01-01T00:00:00+00:00",
            "provider_connection": "custom", "provider_connection_revision": "a" * 32,
        }
        self.events = [
            {"seq": 1, "type": "turn_started", "run_id": "old-run", "backend": self.backend, "prompt": "Saved question"},
            {"seq": 2, "type": "assistant_text", "run_id": "old-run", "backend": self.backend, "text": "Saved answer"},
            {"seq": 3, "type": "turn_finished", "run_id": "old-run", "backend": self.backend, "exit_code": 0},
        ]
        for name, value in (
            ("STATE_DIR", self.root), ("SESSIONS_FILE", self.root / "sessions.json"),
            ("FILES_ROOT", self.root / "files"), ("CODE_DIFFS_ROOT", self.root / "code_diffs"),
            ("STORE", self.store), ("AGENT_TOKEN", "retirement-test-token"),
            ("JOBS", self.jobs), ("JOBS_FILE", self.root / "jobs.json"),
            ("QUEUE_RECOVERY_TASK", None), ("QUEUE_RECOVERY_READY", set()),
            ("HISTORY_MAINTENANCE_STATE", {}), ("HISTORY_MAINTENANCE_TASKS", {}),
            ("HISTORY_SYNC_SCHEDULED", set()),
        ):
            self.enterContext(patch.object(server, name, value))
        self.root.joinpath("sessions.json").write_text(json.dumps({self.session_id: self.session}))
        event_file = server.events_path(self.session_id)
        event_file.parent.mkdir(parents=True)
        self.original_events = "".join(json.dumps(event) + "\n" for event in self.events).encode()
        event_file.write_bytes(self.original_events)
        await self.store.load()
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=server.app), base_url="http://testserver",
            headers={"X-Hyadex-Token": "retirement-test-token"},
        )
        self.addAsyncCleanup(self.client.aclose)
        self.addAsyncCleanup(self.store.flush_pending_save)

    async def test_metadata_edits_work_without_rebinding_or_rewriting_history(self):
        with patch.object(server, "kill_terminal_session"):
            for changes in ({"title": "Renamed", "folder": "Saved", "pinned": True},
                            {"archived": True}, {"archived": False}):
                response = await self.client.patch(f"/api/sessions/{self.session_id}", json=changes)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()["session"]["backend"], self.backend)
            for changes in ({"backend": "codex"}, {"effort": "high"}, {"model": "new"}):
                response = await self.client.patch(f"/api/sessions/{self.session_id}", json=changes)
                self.assertEqual(response.status_code, 410, response.text)
        self.assertEqual(self.store.sessions[self.session_id]["provider_connection_revision"], "a" * 32)
        self.assertEqual(server.events_path(self.session_id).read_bytes(), self.original_events)

    async def test_retired_jobs_pause_durably_and_cannot_be_reenabled_or_run(self):
        jobs = {
            "job_retired": {"id": "job_retired", "session_id": self.session_id,
                "title": "Old schedule", "prompt": "Never launch", "interval_seconds": 60,
                "loop": True, "enabled": True, "manual_run_pending": True, "next_run_at": 1},
        }
        server.JOBS_FILE.write_text(json.dumps(jobs))
        await self.jobs.load()
        for _ in range(2):
            current = self.jobs.jobs["job_retired"]
            self.assertFalse(current["enabled"])
            self.assertFalse(current["manual_run_pending"])
            self.assertIsNone(current["next_run_at"])
            self.assertEqual(current["disabled_reason"], "provider_removed")
            await self.jobs.load()
        with patch.object(server, "start_turn", new_callable=AsyncMock) as launch:
            for operation in (self.jobs.update("job_retired", {"enabled": True}),
                              self.jobs.request_manual_run("job_retired"),
                              self.jobs.run_job("job_retired"),
                              self.jobs.create(server.CreateJobRequest(
                                  session_id=self.session_id, title="New schedule", prompt="Never launch",
                                  interval_seconds=60, context_mode="standalone", backend="codex"))):
                with self.assertRaises(HTTPException) as raised:
                    await operation
                self.assertEqual(raised.exception.status_code, 410)
            launch.assert_not_awaited()
        self.assertEqual(server.events_path(self.session_id).read_bytes(), self.original_events)

    async def test_uncertain_gemini_cleanup_fence_is_preserved(self):
        if self.backend != "gemini":
            self.skipTest("The persisted cleanup fence belongs to Gemini.")
        self.store.sessions[self.session_id]["gemini_cleanup_pending_run_id"] = "old-unconfirmed-run"
        result = await server.stop_turn(self.session_id)
        self.assertFalse(result["stopped"])
        self.assertTrue(result["pending"])
        response = await self.client.delete(f"/api/sessions/{self.session_id}")
        self.assertEqual(response.status_code, 409, response.text)
        self.assertIn(self.session_id, self.store.sessions)
        self.assertEqual(server.events_path(self.session_id).read_bytes(), self.original_events)

    async def test_retired_job_override_on_a_supported_parent_stays_paused(self):
        self.store.sessions["supported-parent"] = {"id": "supported-parent", "backend": "codex"}
        server.JOBS_FILE.write_text(json.dumps({"old-override": {
            "id": "old-override", "session_id": "supported-parent", "backend": self.backend,
            "context_mode": "standalone", "title": "Old override", "prompt": "Never launch",
            "interval_seconds": 60, "loop": True, "enabled": True, "manual_run_pending": True,
        }}))
        for _ in range(2):
            await self.jobs.load()
            job = self.jobs.jobs["old-override"]
            self.assertFalse(job["enabled"])
            self.assertFalse(job["manual_run_pending"])
            self.assertEqual(job["disabled_reason"], "provider_removed")
            with self.assertRaises(HTTPException) as raised:
                await self.jobs.request_manual_run("old-override")
            self.assertEqual(raised.exception.status_code, 410)
        self.assertEqual(self.store.sessions["supported-parent"]["backend"], "codex")

    async def test_saved_chat_load_and_http_read_preserve_identity_and_event_bytes(self):
        for _ in range(2):
            response = await self.client.get(f"/api/sessions/{self.session_id}")
            self.assertEqual(response.status_code, 200, response.text)
            value = response.json()
            self.assertEqual(value["session"]["backend"], self.backend)
            self.assertEqual(value["session"]["session_id"], "old-native-id")
            self.assertTrue(any(event.get("text") == "Saved answer" for event in value["events"]))
            self.assertEqual(server.events_path(self.session_id).read_bytes(), self.original_events)
        self.assertEqual(self.store.sessions[self.session_id][self.backend + "_session_id"], "old-native-id")
        await self.store.save(durable=True)
        await self.store.load()
        self.assertEqual(self.store.sessions[self.session_id]["backend"], self.backend)
        self.assertEqual(server.events_path(self.session_id).read_bytes(), self.original_events)

    async def test_turn_and_fork_reject_without_invoking_a_replacement_runtime(self):
        launch = self.enterContext(patch.object(server, "ensure_runtime_available", new_callable=AsyncMock))
        for endpoint, body in (("turns", {"prompt": "Continue"}), ("fork", {})):
            response = await self.client.post(f"/api/sessions/{self.session_id}/{endpoint}", json=body)
            self.assertEqual(response.status_code, 410, response.text)
            self.assertIn("removed", response.json()["detail"])
        launch.assert_not_awaited()
        self.assertEqual(server.events_path(self.session_id).read_bytes(), self.original_events)

    async def test_removed_provider_is_absent_from_admission_and_admin_contracts(self):
        with patch.dict(os.environ, {"HYADEX_ENABLED_BACKENDS": "claude,codex," + self.backend}):
            self.assertNotIn(self.backend, server.enabled_backends())
            response = await self.client.post("/api/sessions", json={"backend": self.backend})
            self.assertEqual(response.status_code, 400, response.text)
        response = await self.client.get(f"/api/sessions/{self.session_id}/{self.backend}/runtime")
        self.assertEqual(response.status_code, 404, response.text)
        with self.assertRaises(ValidationError):
            ParticipantSpec(backend=self.backend)

    def test_direct_retired_runtime_probe_cannot_select_a_replacement_executable(self):
        with patch.object(server.shutil, "which", side_effect=AssertionError("Must not resolve a replacement runtime")):
            for backend in (self.backend, "unknown-runtime"):
                with self.subTest(backend=backend):
                    with self.assertRaises(HTTPException) as raised:
                        server.probe_runtime(backend)
                    self.assertEqual(raised.exception.status_code, 410)

    async def test_direct_runtime_admission_fails_before_any_probe(self):
        with patch.object(server, "runtime_diagnostic", new=Mock()) as probe:
            with self.assertRaises(HTTPException) as raised:
                await server.ensure_runtime_available(self.backend, session=self.session)
            self.assertEqual(raised.exception.status_code, 410)
            probe.assert_not_called()

class RetiredOpenCodeTests(RetiredProviderTests):
    backend = "opencode"


class RetiredCursorTests(RetiredProviderTests):
    backend = "cursor"
