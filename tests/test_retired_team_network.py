"""Removed network authority cannot be silently reinterpreted as local work."""
from collections import deque
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from pydantic import ValidationError

import agent_server as server


class RetiredTeamNetworkTests(unittest.IsolatedAsyncioTestCase):
    def test_new_requests_reject_retired_authority(self):
        for model, fields in (
            (server.TurnRequest, {"prompt": "hello"}),
            (server.UpdateQueuedTurnRequest, {}),
            (server.JobCreateFields, {"title": "Hello", "prompt": "hello"}),
            (server.UpdateJobRequest, {}),
        ):
            model(**fields, team_references=[])
            with self.subTest(model=model.__name__), self.assertRaises(ValidationError):
                model(**fields, team_references=[{"target_id": "old-team-recipient"}])
        with self.assertRaises(ValidationError):
            server.TurnRequest(prompt="hello", secure_peer_envelope_id="old-delivery")

    def test_retired_routes_and_helpers_are_absent(self):
        self.assertTrue({"jobs", "publish"} <= server.PROVIDER_TOOL_HELPERS)
        self.assertFalse({"mail", "team", "chats"} & server.PROVIDER_TOOL_HELPERS)
        paths = [getattr(route, "path", "") for route in server.app.routes]
        self.assertFalse([path for path in paths if any(name in path for name in (
            "team-hub", "secure-peers", "/agent/team", "/agent/mail",
        ))])

    async def test_saved_network_queue_is_discarded_before_provider_admission(self):
        for metadata in (
            {"team_references": [{"target_id": "old"}]},
            {"secure_peer_envelope_id": "old"},
            {"purpose": "secure_peer_handoff_delivery"},
        ):
            item = {"queued_id": "old-queue", "prompt": "private body", "backend": "codex", **metadata}
            with self.subTest(metadata=metadata), \
                 patch.object(server.RUNS, "queued", {"chat": deque([item])}), \
                 patch.object(server.RUNS, "current_turns", {}), \
                 patch.object(server, "terminally_discard_queued_turn", AsyncMock()) as discard, \
                 patch.object(server, "_start_turn_locked", AsyncMock()) as start:
                await server._start_next_queued_turn_locked("chat", admission_backend="codex")
                discard.assert_awaited_once()
                start.assert_not_awaited()
                self.assertNotIn("chat", server.RUNS.queued)

    async def test_saved_team_job_is_paused_including_group_jobs(self):
        for group_id in (None, "legacy-group"):
            store = server.JobStore()
            store.jobs["job"] = {
                "id": "job", "_revision": "revision", "session_id": "chat", "enabled": True,
                "prompt": "old routed task", "team_references": [{"target_id": "old"}],
                "group_id": group_id, "next_run_at": 1,
            }
            with self.subTest(group=group_id), \
                 patch.object(store, "save", AsyncMock()), \
                 patch.object(server, "append_event", AsyncMock()), \
                 patch.object(server, "start_turn", AsyncMock()) as start, \
                 patch.object(server, "start_group_job_run", AsyncMock()) as group_start:
                with self.assertRaises(server.ScheduledJobChatReferenceRepairRequired):
                    await store._start_job_run("job", manual=False)
                self.assertFalse(store.jobs["job"]["enabled"])
                self.assertIsNone(store.jobs["job"]["next_run_at"])
                start.assert_not_awaited()
                group_start.assert_not_awaited()

    async def test_internal_legacy_delivery_cannot_bypass_route_removal(self):
        with patch.object(server, "wait_for_queue_recovery_admission", AsyncMock()):
            with self.assertRaises(HTTPException) as raised:
                await server._start_turn_locked("missing-chat", server.TurnRequest(
                    prompt="old body", purpose="secure_peer_handoff_delivery",
                ))
        self.assertEqual(raised.exception.status_code, 410)
