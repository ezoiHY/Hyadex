import asyncio
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from starlette.requests import Request

import agent_server


class ProviderJobsAccessTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.original_sessions = agent_server.STORE.sessions
        self.original_current_turns = agent_server.RUNS.current_turns
        self.original_authority_root = agent_server.PROVIDER_AUTHORITY_ROOT
        self.original_agent_token = agent_server.AGENT_TOKEN
        self.original_jobs = agent_server.JOBS.jobs
        self.original_lifecycle_locks = agent_server.SESSION_LIFECYCLE_LOCKS
        agent_server.STORE.sessions = {
            "source": {
                "id": "source",
                "title": "Source",
                "backend": "codex",
                "provider_jobs_access": "full",
            },
        }
        agent_server.RUNS.current_turns = {}
        agent_server.PROVIDER_AUTHORITY_ROOT = self.root / "authority"
        agent_server.PROVIDER_AUTHORITIES.clear()
        agent_server.AGENT_TOKEN = "test-admin-token"
        agent_server.JOBS.jobs = {}
        agent_server.SESSION_LIFECYCLE_LOCKS = {}

    async def asyncTearDown(self) -> None:
        agent_server.PROVIDER_AUTHORITIES.clear()
        agent_server.STORE.sessions = self.original_sessions
        agent_server.RUNS.current_turns = self.original_current_turns
        agent_server.PROVIDER_AUTHORITY_ROOT = self.original_authority_root
        agent_server.AGENT_TOKEN = self.original_agent_token
        agent_server.JOBS.jobs = self.original_jobs
        agent_server.SESSION_LIFECYCLE_LOCKS = self.original_lifecycle_locks
        self.temporary.cleanup()

    @staticmethod
    def provider_request(token: str, method: str = "GET") -> Request:
        return Request({
            "type": "http",
            "method": method,
            "path": "/api/agent/sessions/source/jobs",
            "headers": [
                (b"x-hyadex-provider-capability", token.encode("utf-8")),
            ],
            "query_string": b"",
            "scheme": "http",
            "server": ("127.0.0.1", 7870),
            "client": ("127.0.0.1", 41000),
        })

    async def issue(self, mode: str, run_id: str) -> tuple[str, dict]:
        agent_server.STORE.sessions["source"]["provider_jobs_access"] = mode
        authority_path = await agent_server.issue_provider_authority("source", run_id)
        payload = json.loads(authority_path.read_text(encoding="utf-8"))
        token = payload["provider_capability"]
        token_hash = agent_server.hashlib.sha256(token.encode("utf-8")).hexdigest()
        return token, agent_server.PROVIDER_AUTHORITIES[token_hash]

    async def test_session_policy_defaults_persists_updates_and_validates(self) -> None:
        store = agent_server.SessionStore()
        sessions_path = self.root / "sessions.json"
        with (
            patch.object(agent_server, "SESSIONS_FILE", sessions_path),
            patch.object(agent_server, "ensure_dirs"),
            patch.object(agent_server, "append_event", new_callable=AsyncMock),
        ):
            created = await store.create(
                agent_server.CreateSessionRequest(title="Policy chat"),
            )
            self.assertEqual(created["provider_jobs_access"], "full")
            persisted = json.loads(sessions_path.read_text(encoding="utf-8"))
            self.assertEqual(
                persisted[created["id"]]["provider_jobs_access"],
                "full",
            )

            updated = await store.update(
                created["id"],
                {"provider_jobs_access": "read_only"},
            )
            self.assertEqual(updated["provider_jobs_access"], "read_only")
            persisted = json.loads(sessions_path.read_text(encoding="utf-8"))
            self.assertEqual(
                persisted[created["id"]]["provider_jobs_access"],
                "read_only",
            )

            with self.assertRaises(HTTPException) as raised:
                await store.update(
                    created["id"],
                    {"provider_jobs_access": "unexpected"},
                )
        self.assertEqual(raised.exception.status_code, 400)
        self.assertEqual(
            agent_server.effective_provider_jobs_access({}),
            "full",
        )
        self.assertEqual(
            agent_server.public_session({"id": "legacy"})["provider_jobs_access"],
            "full",
        )
        with self.assertRaises(ValueError):
            agent_server.UpdateSessionRequest(provider_jobs_access="unexpected")

    async def test_legacy_session_load_migrates_default_policy_durably(self) -> None:
        sessions_path = self.root / "legacy-sessions.json"
        sessions_path.write_text(json.dumps({
            "legacy": {
                "id": "legacy",
                "title": "Legacy",
                "backend": "claude",
                "cwd": "/tmp",
            },
        }), encoding="utf-8")
        store = agent_server.SessionStore()
        with (
            patch.object(agent_server, "SESSIONS_FILE", sessions_path),
            patch.object(agent_server, "ensure_dirs"),
            patch.object(
                agent_server,
                "read_abandoned_fork_thread_ids",
                return_value=set(),
            ),
            patch.object(agent_server, "rebuild_codex_subagent_indexes"),
        ):
            await store.load()

        self.assertEqual(
            store.sessions["legacy"]["provider_jobs_access"],
            "full",
        )
        persisted = json.loads(sessions_path.read_text(encoding="utf-8"))
        self.assertEqual(
            persisted["legacy"]["provider_jobs_access"],
            "full",
        )

    async def test_failed_policy_save_restores_previous_authorization(self) -> None:
        store = agent_server.SessionStore()
        store.sessions = {
            "source": {
                "id": "source",
                "backend": "codex",
                "provider_jobs_access": "blocked",
            },
        }
        with patch.object(
            store,
            "save",
            AsyncMock(side_effect=OSError("disk unavailable")),
        ):
            with self.assertRaises(OSError):
                await store.update(
                    "source",
                    {"provider_jobs_access": "full"},
                )
        self.assertEqual(
            store.sessions["source"]["provider_jobs_access"],
            "blocked",
        )

    async def test_cancelled_policy_expansion_is_rolled_back_on_disk(self) -> None:
        store = agent_server.SessionStore()
        store.sessions = {
            "source": {
                "id": "source",
                "backend": "codex",
                "provider_jobs_access": "blocked",
            },
        }
        sessions_path = self.root / "cancelled-policy-sessions.json"
        first_write_started = threading.Event()
        release_first_write = threading.Event()
        written_policies: list[str] = []
        real_write = agent_server.write_sessions_json_text

        def blocked_write(
            path: Path,
            text: str,
            *,
            durable: bool,
        ) -> None:
            policy = json.loads(text)["source"]["provider_jobs_access"]
            written_policies.append(policy)
            if len(written_policies) == 1:
                first_write_started.set()
                self.assertTrue(release_first_write.wait(timeout=2))
            real_write(path, text, durable=durable)

        with (
            patch.object(agent_server, "SESSIONS_FILE", sessions_path),
            patch.object(agent_server, "ensure_dirs"),
            patch.object(
                agent_server,
                "write_sessions_json_text",
                side_effect=blocked_write,
            ),
        ):
            update_task = asyncio.create_task(store.update(
                "source",
                {"provider_jobs_access": "full"},
            ))
            try:
                self.assertTrue(
                    await asyncio.to_thread(first_write_started.wait, 1)
                )
                update_task.cancel()
                await asyncio.sleep(0)
                self.assertFalse(update_task.done())
                # A second cancellation must not interrupt the rollback join.
                update_task.cancel()
            finally:
                release_first_write.set()
            with self.assertRaises(asyncio.CancelledError):
                await update_task
            await store.flush_pending_save()

        self.assertEqual(written_policies, ["full", "blocked"])
        self.assertEqual(
            store.sessions["source"]["provider_jobs_access"],
            "blocked",
        )
        persisted = json.loads(sessions_path.read_text(encoding="utf-8"))
        self.assertEqual(
            persisted["source"]["provider_jobs_access"],
            "blocked",
        )

    def test_standalone_scheduled_context_inherits_provider_jobs_policy(self) -> None:
        isolated = agent_server.standalone_provider_session({
            "id": "source",
            "backend": "codex",
            "provider_jobs_access": "read_only",
            "codex_thread_id": "thread-parent",
        })
        self.assertEqual(isolated["provider_jobs_access"], "read_only")
        self.assertIsNone(isolated["codex_thread_id"])

    async def test_health_advertises_versioned_policy_contract(self) -> None:
        with patch.object(
            agent_server,
            "tmux_capability",
            return_value={
                "available": False,
                "required": False,
                "message": "tmux unavailable",
                "action": None,
            },
        ):
            response = await agent_server.health()
        self.assertEqual(response["api_contract_version"], 30)
        # Cross-chat messaging was removed in contract version 30.
        self.assertNotIn("cross_chat_handoffs_v1", response["capabilities"])
        for feature in ("chat_references", "route_mentions", "route_hint_mentions"):
            self.assertNotIn(feature, response["capabilities"]["scheduled_jobs"]["features"])
        self.assertEqual(
            response["capabilities"]["provider_jobs_access_control_v1"],
            {
                "available": True,
                "required": False,
                "message": "Per-chat provider scheduled-jobs access control is available.",
                "action": None,
                "version": 1,
                "modes": ["full", "read_only", "blocked"],
                "default": "full",
            },
        )

    async def test_capability_issuance_is_bounded_by_policy(self) -> None:
        _full_token, full = await self.issue("full", "run_full")
        self.assertIn("jobs", full["actions"])
        self.assertEqual(full["provider_jobs_access"], "full")

        _read_token, read_only = await self.issue("read_only", "run_read")
        self.assertIn("jobs", read_only["actions"])
        self.assertEqual(read_only["provider_jobs_access"], "read_only")

        _blocked_token, blocked = await self.issue("blocked", "run_blocked")
        self.assertNotIn("jobs", blocked["actions"])
        self.assertEqual(blocked["provider_jobs_access"], "blocked")

    def test_only_retired_peer_text_cannot_manage_jobs(self) -> None:
        self.assertTrue(agent_server.provider_turn_may_manage_jobs(None))
        self.assertTrue(agent_server.provider_turn_may_manage_jobs("scheduled_job"))
        self.assertFalse(
            agent_server.provider_turn_may_manage_jobs(
                "secure_peer_handoff_delivery"
            )
        )
        self.assertIn(
            "Manage durable scheduled jobs only when explicitly asked",
            agent_server.CODEX_PROMPT_PRELUDE,
        )

    async def test_provider_capability_cannot_authorize_human_jobs_or_run_now(self) -> None:
        token, _capability = await self.issue("full", "run_human_bypass")
        for method, path in (
            ("GET", "/api/jobs"),
            ("POST", "/api/jobs/job_one/run"),
        ):
            request = Request({
                "type": "http",
                "app": agent_server.app,
                "method": method,
                "path": path,
                "headers": [
                    (b"x-hyadex-provider-capability", token.encode("utf-8")),
                ],
                "query_string": b"",
                "scheme": "http",
                "server": ("127.0.0.1", 7870),
                "client": ("127.0.0.1", 41000),
            })
            call_next = AsyncMock()
            response = await agent_server.require_agent_token(request, call_next)
            self.assertEqual(response.status_code, 401)
            call_next.assert_not_awaited()

    async def test_no_auth_mode_advertises_policy_unavailable_and_omits_jobs_authority(self) -> None:
        agent_server.AGENT_TOKEN = ""
        with patch.object(
            agent_server,
            "tmux_capability",
            return_value={
                "available": False,
                "required": False,
                "message": "tmux unavailable",
                "action": None,
            },
        ):
            response = await agent_server.health()
        capability = response["capabilities"]["provider_jobs_access_control_v1"]
        self.assertFalse(capability["available"])
        self.assertIn("authenticated", capability["message"])
        authority_path = await agent_server.issue_provider_authority("source", "run_no_auth")
        payload = json.loads(authority_path.read_text(encoding="utf-8"))
        token_hash = agent_server.hashlib.sha256(
            payload["provider_capability"].encode("utf-8")
        ).hexdigest()
        self.assertNotIn("jobs", agent_server.PROVIDER_AUTHORITIES[token_hash]["actions"])

    async def test_policy_tightening_is_immediate_during_live_turn(self) -> None:
        token, _capability = await self.issue("full", "run_live")
        agent_server.RUNS.current_turns = {"source": {"run_id": "run_live"}}
        request = self.provider_request(token)

        await agent_server.authorize_provider_jobs_operation(
            request,
            session_id="source",
            operation="write",
        )

        async def update(_session_id: str, policy_patch: dict) -> dict:
            agent_server.STORE.sessions["source"].update(policy_patch)
            return agent_server.STORE.sessions["source"]

        with patch.object(
            agent_server.STORE,
            "update",
            AsyncMock(side_effect=update),
        ):
            response = await agent_server.update_session(
                "source",
                agent_server.UpdateSessionRequest(
                    provider_jobs_access="read_only",
                ),
            )
            self.assertEqual(
                response["session"]["provider_jobs_access"],
                "read_only",
            )
            await agent_server.authorize_provider_jobs_operation(
                request,
                session_id="source",
                operation="read",
            )
            with self.assertRaises(HTTPException) as raised:
                await agent_server.authorize_provider_jobs_operation(
                    request,
                    session_id="source",
                    operation="write",
                )
            self.assertEqual(raised.exception.status_code, 403)
            self.assertIn("read-only", str(raised.exception.detail))

            await agent_server.update_session(
                "source",
                agent_server.UpdateSessionRequest(
                    provider_jobs_access="blocked",
                ),
            )
            with self.assertRaises(HTTPException) as raised:
                await agent_server.authorize_provider_jobs_operation(
                    request,
                    session_id="source",
                    operation="read",
                )
            self.assertEqual(raised.exception.status_code, 403)
            self.assertIn("blocked", str(raised.exception.detail))

    async def test_issue_time_read_only_ceiling_cannot_be_loosened_mid_turn(self) -> None:
        token, _capability = await self.issue("read_only", "run_read_live")
        agent_server.RUNS.current_turns = {
            "source": {"run_id": "run_read_live"},
        }
        agent_server.STORE.sessions["source"]["provider_jobs_access"] = "full"
        request = self.provider_request(token)
        await agent_server.authorize_provider_jobs_operation(
            request,
            session_id="source",
            operation="read",
        )
        with self.assertRaises(HTTPException) as raised:
            await agent_server.authorize_provider_jobs_operation(
                request,
                session_id="source",
                operation="write",
            )
        self.assertEqual(raised.exception.status_code, 403)

    async def test_policy_patch_serializes_with_live_agent_job_mutation(self) -> None:
        token, _capability = await self.issue("full", "run_policy_race")
        agent_server.RUNS.current_turns = {
            "source": {"run_id": "run_policy_race"},
        }
        request = self.provider_request(token, method="POST")
        update_entered = agent_server.asyncio.Event()
        allow_update = agent_server.asyncio.Event()

        async def gated_update(_session_id: str, policy_patch: dict) -> dict:
            update_entered.set()
            await allow_update.wait()
            agent_server.STORE.sessions["source"].update(policy_patch)
            return agent_server.STORE.sessions["source"]

        create = AsyncMock(return_value={})
        with (
            patch.object(
                agent_server.STORE,
                "update",
                AsyncMock(side_effect=gated_update),
            ),
            patch.object(agent_server.JOBS, "create", create),
        ):
            update_task = agent_server.asyncio.create_task(
                agent_server.update_session(
                    "source",
                    agent_server.UpdateSessionRequest(
                        provider_jobs_access="blocked",
                    ),
                )
            )
            await update_entered.wait()
            create_task = agent_server.asyncio.create_task(
                agent_server.create_agent_session_job(
                    request,
                    "source",
                    agent_server.AgentCreateScopedJobRequest(
                        title="Must not race",
                        prompt="Run",
                        interval_seconds=60,
                    ),
                )
            )
            await agent_server.asyncio.sleep(0)
            self.assertFalse(create_task.done())
            allow_update.set()
            await update_task
            with self.assertRaises(HTTPException) as raised:
                await create_task

        self.assertEqual(raised.exception.status_code, 403)
        create.assert_not_awaited()

    async def test_every_agent_jobs_route_declares_read_or_write_policy(self) -> None:
        request = self.provider_request("unused")
        authorize = AsyncMock(return_value={})
        with (
            patch.object(
                agent_server,
                "authorize_provider_jobs_operation",
                authorize,
            ),
            patch.object(
                agent_server,
                "list_session_jobs",
                AsyncMock(return_value={"jobs": []}),
            ),
            patch.object(
                agent_server,
                "get_session_job_runs",
                AsyncMock(return_value={"runs": []}),
            ),
            patch.object(agent_server.JOBS, "create", AsyncMock(return_value={})),
            patch.object(agent_server.JOBS, "update", AsyncMock(return_value={})),
            patch.object(agent_server.JOBS, "delete", AsyncMock(return_value=True)),
        ):
            await agent_server.list_agent_session_jobs(request, "source")
            await agent_server.get_agent_session_job_runs(
                request,
                "source",
                "job_1",
                before_seq=None,
                limit=20,
            )
            await agent_server.create_agent_session_job(
                request,
                "source",
                agent_server.AgentCreateScopedJobRequest(
                    title="Create",
                    prompt="Run",
                    interval_seconds=60,
                ),
            )
            await agent_server.update_agent_session_job(
                request,
                "source",
                "job_1",
                agent_server.AgentUpdateJobRequest(enabled=False),
            )
            await agent_server.delete_agent_session_job(
                request,
                "source",
                "job_1",
            )

        self.assertEqual(
            [call.kwargs["operation"] for call in authorize.await_args_list],
            ["read", "read", "write", "write", "write"],
        )

    async def test_agent_job_projections_hide_internal_chat_reference_ids(self) -> None:
        secret_target_id = "internal-target-session-secret"
        token, _capability = await self.issue("full", "run_projection")
        agent_server.RUNS.current_turns = {
            "source": {"run_id": "run_projection"},
        }
        # Jobs saved before @Chat targets were retired still carry them.
        direct_reference = {
            "session_id": secret_target_id,
            "display_title_snapshot": f"Mobile {secret_target_id}",
            "source_text_start": 0,
            "source_text_end": len(f"@Mobile {secret_target_id}"),
            "action": "direct_message",
        }
        route_reference = {
            "session_id": "second-internal-target-session-secret",
            "display_title_snapshot": "Ops",
            "source_text_start": 18,
            "source_text_end": 23,
            "action": "route",
        }
        raw_job = {
            "id": "job_secret_target",
            "session_id": "source",
            "title": "Routed job",
            "prompt": f"@Mobile {secret_target_id} check now",
            "chat_references": [direct_reference, route_reference],
            "schedule_kind": "interval",
            "interval_seconds": 300,
            "timezone": "UTC",
            "loop": True,
            "enabled": True,
            "updated_at": "2026-08-26T00:00:00Z",
        }
        agent_server.JOBS.jobs[raw_job["id"]] = raw_job

        listed = await agent_server.list_agent_session_jobs(
            self.provider_request(token),
            "source",
        )
        with (
            patch.object(
                agent_server.JOBS,
                "create",
                AsyncMock(return_value=raw_job),
            ),
            patch.object(
                agent_server.JOBS,
                "update",
                AsyncMock(return_value=raw_job),
            ),
            patch.object(
                agent_server,
                "append_provider_job_mutation_event",
                new_callable=AsyncMock,
            ),
        ):
            created = await agent_server.create_agent_session_job(
                self.provider_request(token, method="POST"),
                "source",
                agent_server.AgentCreateScopedJobRequest(
                    title="Routed job",
                    prompt="Run",
                    interval_seconds=300,
                ),
            )
            updated = await agent_server.update_agent_session_job(
                self.provider_request(token, method="PATCH"),
                "source",
                raw_job["id"],
                agent_server.AgentUpdateJobRequest(title="Renamed"),
            )

        projections = [listed["jobs"][0], created["job"], updated["job"]]
        for projection in projections:
            with self.subTest(projection=projection):
                self.assertNotIn("chat_references", projection)
                self.assertNotIn(secret_target_id, json.dumps(projection))
                self.assertNotIn("second-internal-target-session-secret", json.dumps(projection))
                self.assertEqual(projection["chat_target_count"], 2)

    async def test_agent_job_runs_hide_target_ids_in_running_and_error_events(self) -> None:
        secret_target_id = "internal-target-session-secret"
        field_only_target_id = "field-only-target-session-secret"
        envelope_id = "handoff_private_scheduled_delivery"
        exchange_id = "exchange_private_scheduled_delivery"
        leg_id = "leg_private_scheduled_delivery"
        token, _capability = await self.issue("read_only", "run_history_projection")
        agent_server.RUNS.current_turns = {
            "source": {"run_id": "run_history_projection"},
        }
        reference = {
            "session_id": secret_target_id,
            "display_title_snapshot": "Mobile",
            "source_text_start": 0,
            "source_text_end": 7,
            "action": "direct_message",
        }
        raw_job = {
            "id": "job_secret_target",
            "session_id": "source",
            "title": "Routed job",
            "prompt": "@Mobile check now",
            "chat_references": [reference],
            "schedule_kind": "interval",
            "interval_seconds": 300,
            "timezone": "UTC",
        }
        raw_response = {
            "session_id": "source",
            "job_id": raw_job["id"],
            "runs": [
                {
                    "type": "turn_started",
                    "job_id": raw_job["id"],
                    "chat_references": [reference],
                    "target_session_id": secret_target_id,
                    "cross_chat_envelope_id": envelope_id,
                    "cross_chat_exchange_ids": [exchange_id],
                    "cross_chat_direct_message_ids": [envelope_id],
                    "delivery": {
                        "id": leg_id,
                        "parent_leg_id": leg_id,
                    },
                    "message": (
                        f"Started {envelope_id} / {exchange_id} / {leg_id}"
                    ),
                    "job_status": "running",
                },
                {
                    "type": "job_deferred",
                    "job_id": raw_job["id"],
                    "job": raw_job,
                    "message": f"Deferred target {secret_target_id}",
                    "job_status": "deferred",
                },
                {
                    "type": "job_error",
                    "job_id": raw_job["id"],
                    "target_session_id": field_only_target_id,
                    "error": f"Could not reach {field_only_target_id}",
                    "job_status": "failed",
                },
            ],
            "total": 3,
        }

        with patch.object(
            agent_server,
            "get_session_job_runs",
            AsyncMock(return_value=raw_response),
        ):
            response = await agent_server.get_agent_session_job_runs(
                self.provider_request(token),
                "source",
                raw_job["id"],
                before_seq=None,
                limit=20,
            )

        self.assertEqual(response["total"], 3)
        self.assertNotIn(secret_target_id, json.dumps(response))
        self.assertNotIn(field_only_target_id, json.dumps(response))
        for private_id in (envelope_id, exchange_id, leg_id):
            self.assertNotIn(private_id, json.dumps(response))
        for run in response["runs"]:
            with self.subTest(run=run):
                self.assertNotIn("chat_references", run)
                self.assertNotIn("target_session_id", run)
                self.assertNotIn("cross_chat_envelope_id", run)
                self.assertNotIn("cross_chat_exchange_ids", run)
                self.assertNotIn("cross_chat_direct_message_ids", run)
        self.assertEqual(response["runs"][0]["chat_target_count"], 1)
        for run in response["runs"][1:2]:
            self.assertNotIn("chat_references", run["job"])
            self.assertEqual(run["job"]["chat_target_count"], 1)

    async def test_human_jobs_routes_ignore_provider_policy(self) -> None:
        agent_server.STORE.sessions["source"]["provider_jobs_access"] = "blocked"
        job = {
            "id": "job_human",
            "session_id": "source",
            "title": "Human-created",
        }
        with patch.object(
            agent_server.JOBS,
            "create",
            AsyncMock(return_value=job),
        ) as create:
            response = await agent_server.create_job(
                agent_server.CreateJobRequest(
                    session_id="source",
                    title="Human-created",
                    prompt="Run",
                    interval_seconds=60,
                ),
            )
        self.assertEqual(response["job"]["id"], "job_human")
        create.assert_awaited_once()

if __name__ == "__main__":
    unittest.main()
