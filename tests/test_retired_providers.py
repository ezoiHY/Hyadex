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
import provider_connections
from hyadex.groups import ParticipantSpec


class RetiredProviderTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="retired-provider-")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.store = server.SessionStore()
        self.jobs = server.JobStore()
        self.session_id = "sess_retired_provider"
        self.session = {
            "id": self.session_id, "backend": "gemini", "title": "Saved conversation",
            "session_id": "old-native-id", "gemini_session_id": "old-native-id",
            "backend_locked": True, "cwd": str(self.root), "folder": "General",
            "created_at": "2026-01-01T00:00:00+00:00", "updated_at": "2026-01-01T00:00:00+00:00",
            "provider_connection": "custom", "provider_connection_revision": "a" * 32,
        }
        self.events = [
            {"seq": 1, "type": "turn_started", "run_id": "old-run", "backend": "gemini", "prompt": "Saved question"},
            {"seq": 2, "type": "assistant_text", "run_id": "old-run", "backend": "gemini", "text": "Saved answer"},
            {"seq": 3, "type": "turn_finished", "run_id": "old-run", "backend": "gemini", "exit_code": 0},
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
        with patch.object(server, "kill_terminal_session"), patch.object(
            server, "terminalize_archived_cross_chat_session", new_callable=AsyncMock,
        ), patch.object(server.PROVIDER_CONNECTION_STORE, "bind", side_effect=AssertionError("No runtime binding")):
            for changes in ({"title": "Renamed", "folder": "Saved", "pinned": True},
                            {"archived": True}, {"archived": False}):
                response = await self.client.patch(f"/api/sessions/{self.session_id}", json=changes)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()["session"]["backend"], "gemini")
            for changes in ({"backend": "codex"}, {"provider_connection": "default"}, {"model": "new"}):
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

    async def test_import_rejects_and_uncertain_cleanup_fence_is_preserved(self):
        response = await self.client.post(f"/api/sessions/{self.session_id}/import-history", json={"force": True})
        self.assertEqual(response.status_code, 410, response.text)
        self.store.sessions[self.session_id]["gemini_cleanup_pending_run_id"] = "old-unconfirmed-run"
        result = await server.stop_turn(self.session_id)
        self.assertFalse(result["stopped"])
        self.assertTrue(result["pending"])
        response = await self.client.delete(f"/api/sessions/{self.session_id}")
        self.assertEqual(response.status_code, 409, response.text)
        self.assertIn(self.session_id, self.store.sessions)
        self.assertEqual(server.events_path(self.session_id).read_bytes(), self.original_events)

    async def test_saved_chat_load_and_http_read_preserve_identity_and_event_bytes(self):
        for _ in range(2):
            response = await self.client.get(f"/api/sessions/{self.session_id}")
            self.assertEqual(response.status_code, 200, response.text)
            value = response.json()
            self.assertEqual(value["session"]["backend"], "gemini")
            self.assertEqual(value["session"]["session_id"], "old-native-id")
            self.assertTrue(any(event.get("text") == "Saved answer" for event in value["events"]))
            self.assertEqual(server.events_path(self.session_id).read_bytes(), self.original_events)
        self.assertEqual(self.store.sessions[self.session_id]["gemini_session_id"], "old-native-id")
        await self.store.save(durable=True)
        await self.store.load()
        self.assertEqual(self.store.sessions[self.session_id]["backend"], "gemini")
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
        with patch.dict(os.environ, {"HYADEX_ENABLED_BACKENDS": "claude,codex,gemini"}):
            self.assertNotIn("gemini", server.enabled_backends())
            response = await self.client.post("/api/sessions", json={"backend": "gemini"})
            self.assertEqual(response.status_code, 400, response.text)
        response = await self.client.get("/api/admin/provider-connections/gemini")
        self.assertEqual(response.status_code, 400, response.text)
        response = await self.client.get(f"/api/sessions/{self.session_id}/gemini/runtime")
        self.assertEqual(response.status_code, 404, response.text)
        with self.assertRaises(ValidationError):
            ParticipantSpec(backend="gemini")
        with self.assertRaises(ValidationError):
            server.BulkImportSessionItem(backend="gemini", provider_session_id="old-native-id")
        self.assertNotIn("gemini", provider_connections.PROTOCOLS)

    async def test_direct_runtime_admission_fails_before_any_probe(self):
        with patch.object(server, "runtime_diagnostic", new=Mock()) as probe:
            with self.assertRaises(HTTPException) as raised:
                await server.ensure_runtime_available("gemini", session=self.session)
            self.assertEqual(raised.exception.status_code, 410)
            probe.assert_not_called()
