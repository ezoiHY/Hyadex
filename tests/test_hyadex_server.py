"""Hyadex product defaults and the group-discussion wiring inside the server."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import HTTPException
from fastapi.testclient import TestClient

import agent_server


class HyadexDefaultsTests(unittest.IsolatedAsyncioTestCase):
    async def test_new_chats_are_limited_to_enabled_backends(self) -> None:
        with patch.dict(os.environ, {"HYADEX_ENABLED_BACKENDS": ""}):
            self.assertEqual(agent_server.enabled_backends(), {"claude", "codex"})
            with self.assertRaises(HTTPException) as raised:
                await agent_server.create_session(agent_server.CreateSessionRequest(backend="cursor"))
            self.assertEqual(raised.exception.status_code, 400)
            self.assertEqual(agent_server.require_enabled_backend("codex"), "codex")
        with patch.dict(os.environ, {"HYADEX_ENABLED_BACKENDS": "claude,codex,opencode,bogus"}):
            self.assertEqual(agent_server.enabled_backends(), {"claude", "codex"})

    async def test_in_app_updates_are_disabled_unless_explicitly_enabled(self) -> None:
        with patch.dict(os.environ, {"HYADEX_MANAGED_UPDATES": ""}):
            self.assertFalse(agent_server.managed_updates_enabled())
            with self.assertRaises(HTTPException) as raised:
                await agent_server.check_server_update(None)
            self.assertEqual(raised.exception.status_code, 409)
            self.assertIn("disabled", raised.exception.detail)
        with patch.dict(os.environ, {"HYADEX_MANAGED_UPDATES": "1"}):
            self.assertTrue(agent_server.managed_updates_enabled())

    def test_release_channel_points_at_hyadex(self) -> None:
        import update_runner

        self.assertEqual(update_runner.RELEASE_REPOSITORY, "ezoiHY/Hyadex")


class GroupWiringTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = agent_server.SessionStore()
        self.store.sessions = {
            "sess_visible": {"id": "sess_visible", "title": "Normal chat", "backend": "claude"},
            "sess_member": {"id": "sess_member", "title": "Group · @codex", "backend": "codex",
                            "group_member": {"group_id": "grp_0123456789abcdef", "handle": "codex"},
                            "codex_needs_user_action": True},
        }
        self.patches = [
            patch.object(agent_server, "STORE", self.store),
            patch.object(agent_server, "STATE_DIR", self.root),
        ]
        for item in self.patches:
            item.start()

    async def asyncTearDown(self) -> None:
        for item in reversed(self.patches):
            item.stop()
        self.temporary.cleanup()

    async def test_member_chats_are_hidden_from_lists_and_global_search(self) -> None:
        with patch.object(agent_server.STORE, "ensure_sort_orders", AsyncMock()):
            listed = await agent_server.list_sessions()
        self.assertEqual([item["id"] for item in listed["sessions"]], ["sess_visible"])
        self.assertNotIn("sess_member", agent_server.active_history_search_session_ids())
        member = agent_server.public_session(self.store.sessions["sess_member"])
        self.assertEqual(member["group_member"], {"group_id": "grp_0123456789abcdef", "handle": "codex"})
        self.assertNotIn("group_member", agent_server.public_session(self.store.sessions["sess_visible"]))

    async def test_turn_events_are_forwarded_to_the_group_engine(self) -> None:
        engine = MagicMock()
        with (
            patch.object(agent_server, "GROUPS", engine),
            patch.object(agent_server, "HUB", MagicMock(broadcast=AsyncMock())),
        ):
            await agent_server.append_event("sess_member", "turn_finished", {"run_id": "run_1", "result_text": "done"})
            await agent_server.append_event("sess_member", "assistant_text", {"text": "partial"})
        forwarded = [call.args for call in engine.observe_member_event.call_args_list]
        self.assertEqual([(session_id, event["type"]) for session_id, event in forwarded],
                         [("sess_member", "turn_finished")])

    async def test_durably_appended_turn_events_reach_the_group_engine(self) -> None:
        # Turns that leave a queue start through append_durable_event.
        engine = MagicMock()
        with (
            patch.object(agent_server, "GROUPS", engine),
            patch.object(agent_server, "HUB", MagicMock(broadcast=AsyncMock())),
        ):
            await agent_server.append_durable_event("sess_member", "turn_started", {"run_id": "run_2", "queued_id": "q7"})
            await agent_server.append_durable_event("sess_member", "turn_unqueued", {"queued_id": "q8"})
            await agent_server.append_durable_event("sess_member", "turn_queue_paused", {})
        forwarded = [call.args[1]["type"] for call in engine.observe_member_event.call_args_list]
        self.assertEqual(forwarded, ["turn_started", "turn_unqueued"])

    async def test_group_jobs_are_not_deferred_by_a_busy_anchor_chat(self) -> None:
        with (
            patch.object(agent_server, "BUSY_SESSIONS", {"sess_member"}),
            patch.object(agent_server, "JOB_MIN_AVAILABLE_MEM_MB", 0),
            patch.object(agent_server, "MIN_START_AVAILABLE_MEM_MB", 0),
            patch.object(agent_server, "JOB_MAX_ACTIVE_RUNS", 0),
            patch.object(agent_server, "MAX_ACTIVE_AGENT_RUNS", 0),
            patch.object(agent_server, "managed_server_update_scheduled_job_blocker", lambda **_: None),
            patch.object(agent_server, "managed_server_update_admission_blocker", lambda: None),
        ):
            self.assertEqual(await agent_server.scheduled_job_blocker("sess_member"), "chat already has a running turn")
            self.assertIsNone(await agent_server.scheduled_job_blocker("sess_member", group=True))

    async def test_removed_anchor_member_hands_group_jobs_to_the_new_anchor(self) -> None:
        jobs = agent_server.JobStore()
        with (
            patch.object(agent_server, "JOBS", jobs),
            patch.object(agent_server, "JOBS_FILE", self.root / "jobs.json"),
            patch.object(agent_server, "HUB", MagicMock(broadcast=AsyncMock())),
        ):
            job = await jobs.create(agent_server.CreateJobRequest(
                session_id="sess_member", title="Nightly", prompt="@claude summarize",
                schedule_kind="interval", interval_seconds=3600), group_id="grp_0123456789abcdef")
            revision = jobs.jobs[job["id"]]["_revision"]
            await agent_server.HyadexGroupRuntime().participant_removed(
                "grp_0123456789abcdef", "sess_member", "sess_visible")
        self.assertEqual(jobs.jobs[job["id"]]["session_id"], "sess_visible")
        self.assertNotEqual(jobs.jobs[job["id"]]["_revision"], revision)
        self.assertEqual(json.loads((self.root / "jobs.json").read_text())[job["id"]]["session_id"], "sess_visible")

    async def test_runtime_reports_member_state_and_finds_terminals(self) -> None:
        runtime = agent_server.HyadexGroupRuntime()
        self.assertEqual(runtime.member_status("sess_missing"), {"exists": False})
        status = runtime.member_status("sess_member")
        self.assertTrue(status["exists"])
        self.assertTrue(status["needs_action"])
        self.assertEqual(runtime.reply_text({"result_text": "  final answer  "}).strip(), "final answer")
        events = [
            {"seq": 1, "type": "turn_queued", "queued_id": "q1", "client_request_id": "gd_0123456789abcdef"},
            {"seq": 2, "type": "turn_started", "queued_id": "q1", "run_id": "run_9"},
            {"seq": 3, "type": "turn_finished", "run_id": "run_9", "result_text": "ok"},
            {"seq": 4, "type": "turn_queued", "queued_id": "q2", "client_request_id": "gd_fedcba9876543210"},
            {"seq": 5, "type": "turn_unqueued", "queued_id": "q2"},
        ]
        path = agent_server.events_path("sess_member")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(item) + "\n" for item in events))
        found = await runtime.find_member_progress("sess_member", run_id=None, queued_id=None,
                                                   request_id="gd_0123456789abcdef")
        self.assertEqual((found["queued_id"], found["run_id"], found["terminal"]["type"]), ("q1", "run_9", "turn_finished"))
        removed = await runtime.find_member_progress("sess_member", run_id=None, queued_id=None,
                                                     request_id="gd_fedcba9876543210")
        self.assertTrue(removed["unqueued"])
        self.assertEqual(await runtime.find_member_progress("sess_missing", run_id=None, queued_id=None,
                                                            request_id="gd_x"), {"exists": False})

    async def test_group_jobs_post_their_prompt_into_the_group(self) -> None:
        jobs = agent_server.JobStore()
        engine = MagicMock()
        engine.post_message = AsyncMock(return_value={"message": {"seq": 7}, "targets": ["codex"]})
        with (
            patch.object(agent_server, "JOBS", jobs),
            patch.object(agent_server, "JOBS_FILE", self.root / "jobs.json"),
            patch.object(agent_server, "GROUPS", engine),
            patch.object(agent_server, "HUB", MagicMock(broadcast=AsyncMock())),
        ):
            job = await jobs.create(agent_server.CreateJobRequest(
                session_id="sess_member", title="Nightly check", prompt="@codex run the test suite",
                schedule_kind="interval", interval_seconds=3600), group_id="grp_0123456789abcdef")
            self.assertEqual(job["group_id"], "grp_0123456789abcdef")
            self.assertEqual(agent_server.public_job(job)["group_id"], "grp_0123456789abcdef")
            result = await jobs._start_job_run(job["id"], manual=True)
        self.assertEqual(result["group_message_seq"], 7)
        args, kwargs = engine.post_message.call_args
        self.assertEqual(args[0], "grp_0123456789abcdef")
        self.assertEqual(args[1].text, "@codex run the test suite")
        self.assertEqual(kwargs["origin"], "job")
        self.assertEqual(kwargs["meta"]["job_id"], job["id"])
        ran = [json.loads(line) for line in agent_server.events_path("sess_member").read_text().splitlines()]
        self.assertEqual([(item["type"], item.get("group_id")) for item in ran if item["type"] == "job_ran"],
                         [("job_ran", "grp_0123456789abcdef")])
        self.assertEqual(jobs.jobs[job["id"]]["run_count"], 1)

    def test_autopilot_is_wired_to_the_group_engine_and_token_protected(self) -> None:
        self.assertIs(agent_server.AUTOPILOT.groups, agent_server.GROUPS)
        self.assertIn(agent_server.AUTOPILOT._on_group_event, agent_server.GROUPS.listeners)
        engine = agent_server.autopilot.AutopilotEngine(self.root / "autopilot", agent_server.GROUPS)
        with patch.object(agent_server, "AGENT_TOKEN", "secret-token"), patch.object(agent_server, "AUTOPILOT", engine):
            client = TestClient(agent_server.app)
            self.assertEqual(client.get("/api/autopilot/runs").status_code, 401)
            self.assertEqual(client.get("/api/autopilot/runs", headers={"Authorization": "Bearer secret-token"}).status_code, 200)
        agent_server.GROUPS.listeners.remove(engine._on_group_event)

    def test_group_routes_require_the_server_token(self) -> None:
        engine = agent_server.group_chat.GroupEngine(self.root / "groups", agent_server.HyadexGroupRuntime())
        with patch.object(agent_server, "AGENT_TOKEN", "secret-token"), patch.object(agent_server, "GROUPS", engine):
            client = TestClient(agent_server.app)
            self.assertEqual(client.get("/api/groups").status_code, 401)
            response = client.get("/api/groups", headers={"Authorization": "Bearer secret-token"})
        self.assertEqual(response.status_code, 200, response.text)


if __name__ == "__main__":
    unittest.main()
