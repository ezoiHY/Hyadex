import asyncio
import json
import tempfile
import unittest
from collections import deque
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from fastapi import HTTPException, Request
from fastapi.testclient import TestClient

from tests import hyadex_server
class CodexGoalsAdminTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.previous_enabled = hyadex_server.CODEX_GOALS_ENABLED
        self.previous_reconfiguring = hyadex_server.CODEX_GOALS_RECONFIGURING
        self.previous_manager = hyadex_server.PROVIDERS.codex_manager
        self.previous_sessions = hyadex_server.STORE.sessions
        self.previous_busy = hyadex_server.RUNS.busy
        self.previous_active = hyadex_server.RUNS.active
        self.previous_current = hyadex_server.RUNS.current_turns
        self.previous_maintenance = hyadex_server.RUNS.maintenance
        self.previous_queued = hyadex_server.RUNS.queued
        self.previous_run_now = hyadex_server.RUNS.run_now
        self.previous_pending = hyadex_server.CODEX_PENDING_INTERACTIONS
        self.previous_actions = hyadex_server.CODEX_NATIVE_ACTION_TASKS
        self.previous_subagents = hyadex_server.CODEX_SUBAGENT_STATE
        self.previous_subagent_index = hyadex_server.CODEX_SUBAGENT_SESSION_INDEX
        self.previous_live_generations = (
            hyadex_server.CODEX_SUBAGENT_LIVE_GENERATIONS
        )

        hyadex_server.CODEX_GOALS_ENABLED = True
        hyadex_server.CODEX_GOALS_RECONFIGURING = False
        hyadex_server.PROVIDERS.codex_manager = None
        hyadex_server.STORE.sessions = {
            "chat": {
                "id": "chat",
                "backend": hyadex_server.BACKEND_CODEX,
                "codex_thread_id": "thread",
                "codex_goal": {
                    "objective": "Finish the work",
                    "status": "active",
                },
            }
        }
        hyadex_server.RUNS.busy = set()
        hyadex_server.RUNS.active = {}
        hyadex_server.RUNS.current_turns = {}
        hyadex_server.RUNS.maintenance = set()
        hyadex_server.RUNS.queued = {}
        hyadex_server.RUNS.run_now = {}
        hyadex_server.CODEX_PENDING_INTERACTIONS = {}
        hyadex_server.CODEX_NATIVE_ACTION_TASKS = {}
        hyadex_server.CODEX_SUBAGENT_STATE = {}
        hyadex_server.CODEX_SUBAGENT_SESSION_INDEX = {}
        hyadex_server.CODEX_SUBAGENT_LIVE_GENERATIONS = {}

    async def asyncTearDown(self) -> None:
        hyadex_server.CODEX_GOALS_ENABLED = self.previous_enabled
        hyadex_server.CODEX_GOALS_RECONFIGURING = self.previous_reconfiguring
        hyadex_server.PROVIDERS.codex_manager = self.previous_manager
        hyadex_server.STORE.sessions = self.previous_sessions
        hyadex_server.RUNS.busy = self.previous_busy
        hyadex_server.RUNS.active = self.previous_active
        hyadex_server.RUNS.current_turns = self.previous_current
        hyadex_server.RUNS.maintenance = self.previous_maintenance
        hyadex_server.RUNS.queued = self.previous_queued
        hyadex_server.RUNS.run_now = self.previous_run_now
        hyadex_server.CODEX_PENDING_INTERACTIONS = self.previous_pending
        hyadex_server.CODEX_NATIVE_ACTION_TASKS = self.previous_actions
        hyadex_server.CODEX_SUBAGENT_STATE = self.previous_subagents
        hyadex_server.CODEX_SUBAGENT_SESSION_INDEX = self.previous_subagent_index
        hyadex_server.CODEX_SUBAGENT_LIVE_GENERATIONS = (
            self.previous_live_generations
        )

    async def test_admin_endpoint_direct_invocation_repeats_native_auth(self) -> None:
        request = Request({
            "type": "http",
            "method": "PUT",
            "path": "/api/admin/codex/goals",
            "headers": [
                (b"x-hyadex-token", b"test-secret"),
                (b"x-zenithdock-token", b"test-secret"),
            ],
            "query_string": b"",
            "scheme": "http",
            "server": ("127.0.0.1", 7870),
            "client": ("127.0.0.1", 41000),
        })
        with patch.object(hyadex_server, "AGENT_TOKEN", "test-secret"):
            with self.assertRaises(HTTPException) as raised:
                await hyadex_server.put_codex_goals_admin_endpoint(
                    hyadex_server.CodexGoalsAdminRequest(enabled=False),
                    request,
                )

        self.assertEqual(raised.exception.status_code, 401)

    def test_admin_routes_reject_query_and_browser_credentials_before_parsing(self) -> None:
        with patch.object(hyadex_server, "AGENT_TOKEN", "test-secret"):
            client = TestClient(hyadex_server.app)
            cases = (
                ("GET", "/api/admin/codex/goals?token=test-secret", {}, None, 401),
                (
                    "PUT",
                    "/api/admin/codex/goals?token=test-secret",
                    {"Content-Type": "application/json"},
                    b"{",
                    401,
                ),
                (
                    "GET",
                    "/api/admin/codex/goals",
                    {
                        "Origin": "https://attacker.example",
                        "X-Hyadex-Token": "test-secret",
                    },
                    None,
                    403,
                ),
                (
                    "PUT",
                    "/api/admin/codex/goals",
                    {
                        "Origin": "https://attacker.example",
                        "X-Hyadex-Token": "test-secret",
                        "Content-Type": "application/json",
                    },
                    b"{",
                    403,
                ),
            )
            for method, path, headers, body, expected in cases:
                with self.subTest(method=method, path=path, expected=expected):
                    response = client.request(
                        method,
                        path,
                        headers=headers,
                        content=body,
                    )

                    self.assertEqual(response.status_code, expected)

    def test_admin_routes_require_exactly_one_supported_token_header(self) -> None:
        cases = (
            [("Authorization", "Bearer test-secret")],
            [
                ("X-Hyadex-Token", "test-secret"),
                ("X-Hyadex-Token", "test-secret"),
            ],
            [
                ("X-Hyadex-Token", "test-secret"),
                ("X-ZenithDock-Token", "test-secret"),
            ],
        )
        with patch.object(hyadex_server, "AGENT_TOKEN", "test-secret"):
            client = TestClient(hyadex_server.app)
            for headers in cases:
                with self.subTest(headers=headers):
                    response = client.get(
                        "/api/admin/codex/goals",
                        headers=headers,
                    )

                    self.assertEqual(response.status_code, 401)

    def test_admin_put_has_exact_small_json_transport_contract(self) -> None:
        cases = (
            (
                {
                    "X-Hyadex-Token": "test-secret",
                    "Content-Type": "text/plain",
                },
                b'{"enabled":true}',
                415,
            ),
            (
                {
                    "X-Hyadex-Token": "test-secret",
                    "Content-Type": "application/json",
                    "Transfer-Encoding": "chunked",
                },
                b'{"enabled":true}',
                400,
            ),
            (
                {
                    "X-Hyadex-Token": "test-secret",
                    "Content-Type": "application/json",
                    # This endpoint has one boolean field; 256 bytes leaves
                    # ample wire-format headroom without permitting an
                    # attacker-controlled generic JSON allocation.
                    "Content-Length": "257",
                },
                b'{"enabled":true}',
                413,
            ),
        )
        with patch.object(hyadex_server, "AGENT_TOKEN", "test-secret"):
            client = TestClient(hyadex_server.app)
            for headers, body, expected in cases:
                with self.subTest(headers=headers, expected=expected):
                    response = client.put(
                        "/api/admin/codex/goals",
                        headers=headers,
                        content=body,
                    )

                    self.assertEqual(response.status_code, expected)

    def test_admin_routes_keep_both_current_native_client_headers(self) -> None:
        with patch.object(hyadex_server, "AGENT_TOKEN", "test-secret"):
            client = TestClient(hyadex_server.app)
            for name in ("X-Hyadex-Token", "X-ZenithDock-Token"):
                with self.subTest(header=name):
                    response = client.get(
                        "/api/admin/codex/goals",
                        headers={name: "test-secret"},
                    )

                    self.assertEqual(response.status_code, 200)

    def test_goals_ignore_retired_environment_and_persisted_switch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "codex-settings.json"
            with patch.dict("os.environ", {"CODEX_GOALS_ENABLED": "false"}):
                self.assertTrue(hyadex_server.CODEX_GOALS_DEFAULT_ENABLED)
                self.assertTrue(hyadex_server.read_codex_goals_enabled(path))
                self.assertFalse(path.exists())
                for content in ('{"goals_enabled":false,"other":"preserved"}', '{'):
                    path.write_text(content)
                    self.assertTrue(hyadex_server.read_codex_goals_enabled(path))
                    self.assertEqual(path.read_text(), content)

    async def test_legacy_goal_switch_cannot_change_settings_or_chat_goals(self) -> None:
        hyadex_server.STORE.sessions["paused"] = {
            "id": "paused", "backend": hyadex_server.BACKEND_CODEX,
            "codex_goal": {"objective": "Wait for explicit resume", "status": "paused"},
        }
        before_sessions = json.dumps(hyadex_server.STORE.sessions, sort_keys=True)
        with tempfile.TemporaryDirectory() as temporary:
            settings_file = Path(temporary) / "codex-settings.json"
            contents = '{"goals_enabled":false,"thread_config":{"agents":{"max_threads":4}}}'
            settings_file.write_text(contents)
            with (
                patch.object(hyadex_server, "CODEX_SETTINGS_FILE", settings_file),
                patch.object(hyadex_server, "settle_idle_codex_goal_for_stop", AsyncMock()) as settle,
                patch.object(hyadex_server, "close_codex_app_server_manager", AsyncMock()) as close_manager,
                patch.object(hyadex_server, "AGENT_TOKEN", "test-secret"),
            ):
                client = TestClient(hyadex_server.app)
                headers = {"X-Hyadex-Token": "test-secret"}
                read = client.get("/api/admin/codex/goals", headers=headers)
                self.assertEqual(read.status_code, 200)
                self.assertTrue(read.json()["enabled"])
                self.assertFalse(read.json()["configurable"])
                enabled = client.put("/api/admin/codex/goals", headers=headers, json={"enabled": True})
                self.assertEqual(enabled.status_code, 200)
                self.assertEqual(enabled.json(), read.json())
                disabled = client.put("/api/admin/codex/goals", headers=headers, json={"enabled": False})
                self.assertEqual(disabled.status_code, 409)
                self.assertIn("cannot be disabled server-wide", disabled.json()["detail"])
                settle.assert_not_awaited()
                close_manager.assert_not_awaited()
            self.assertEqual(settings_file.read_text(), contents)
        self.assertTrue(hyadex_server.CODEX_GOALS_ENABLED)
        self.assertEqual(json.dumps(hyadex_server.STORE.sessions, sort_keys=True), before_sessions)

    async def test_disabling_does_not_load_ordinary_codex_threads(self) -> None:
        for index in range(500):
            hyadex_server.STORE.sessions[f"ordinary-{index}"] = {
                "id": f"ordinary-{index}",
                "backend": hyadex_server.BACKEND_CODEX,
                "codex_thread_id": f"thread-{index}",
                "codex_goal": None,
            }
        hyadex_server.STORE.sessions["chat"]["codex_goal"]["status"] = "paused"
        settle = AsyncMock(return_value={})

        with patch.object(
            hyadex_server,
            "settle_idle_codex_goal_for_stop",
            settle,
        ):
            result = await hyadex_server.pause_idle_codex_goals_before_disable()

        self.assertEqual(result, {
            "paused_goal_count": 0,
            "fenced_goal_count": 0,
        })
        settle.assert_awaited_once_with("chat")

    async def test_disabling_checks_only_loaded_uncached_provider_threads(self) -> None:
        hyadex_server.STORE.sessions["chat"]["codex_goal"] = None
        hyadex_server.STORE.sessions["ordinary"] = {
            "id": "ordinary",
            "backend": hyadex_server.BACKEND_CODEX,
            "codex_thread_id": "unloaded-thread",
            "codex_goal": None,
        }
        manager = Mock()
        manager.is_thread_loaded.side_effect = (
            lambda thread_id: thread_id == "thread"
        )
        hyadex_server.PROVIDERS.codex_manager = manager
        settle = AsyncMock(return_value={})

        with patch.object(
            hyadex_server,
            "settle_idle_codex_goal_for_stop",
            settle,
        ):
            await hyadex_server.pause_idle_codex_goals_before_disable()

        settle.assert_awaited_once_with("chat")

    async def test_goal_mutation_is_rejected_when_disabled(self) -> None:
        hyadex_server.CODEX_GOALS_ENABLED = False
        acquire = AsyncMock()
        with patch.object(
            hyadex_server,
            "acquire_codex_control_thread",
            acquire,
        ):
            with self.assertRaises(HTTPException) as raised:
                await hyadex_server._put_codex_goal_locked(
                    "chat",
                    hyadex_server.CodexGoalRequest(
                        objective="A new objective",
                    ),
                )

        self.assertEqual(raised.exception.status_code, 409)
        self.assertIn("disabled", str(raised.exception.detail))
        acquire.assert_not_awaited()

    async def test_goal_budget_does_not_block_turns_when_goals_are_disabled(
        self,
    ) -> None:
        session = hyadex_server.STORE.sessions["chat"]
        session["codex_goal_time_budget_seconds"] = 1
        session["codex_goal_time_budget_exhausted"] = True
        hyadex_server.CODEX_GOALS_ENABLED = False

        self.assertFalse(
            hyadex_server.provider_context_goal_is_exhausted(session, "chat")
        )

    async def test_health_advertises_the_effective_goal_capability(self) -> None:
        hyadex_server.CODEX_GOALS_ENABLED = False
        with (
            patch.object(hyadex_server, "host_pressure_snapshot", return_value={}),
            patch.object(
                hyadex_server,
                "tmux_capability",
                return_value={"available": False},
            ),
            patch.object(hyadex_server, "runtime_diagnostics_snapshot", return_value={}),
        ):
            response = await hyadex_server.health()

        self.assertFalse(
            response["capabilities"]["codex_controls"]["features"]["goals"]
        )

    async def test_disabled_manager_launches_with_native_goal_feature_off(self) -> None:
        hyadex_server.CODEX_GOALS_ENABLED = False
        manager = Mock()
        manager.add_notification_handler = Mock()
        with patch.object(
            hyadex_server,
            "CodexAppServerManager",
            return_value=manager,
        ) as manager_type:
            created = await hyadex_server.codex_app_server_manager()

        self.assertIs(created, manager)
        self.assertEqual(
            manager_type.call_args.kwargs["app_server_args"],
            ("--disable", "goals"),
        )

    def test_exec_command_applies_native_goal_feature_off(self) -> None:
        hyadex_server.CODEX_GOALS_ENABLED = False

        command = hyadex_server.build_codex_cmd(
            "chat",
            hyadex_server.STORE.sessions["chat"],
            "Do the work",
            Path("/tmp/current.json"),
        )

        self.assertEqual(command[:4], [
            hyadex_server.CODEX_BIN,
            "exec",
            "--disable",
            "goals",
        ])

    def test_exec_command_omits_goal_override_when_enabled(self) -> None:
        command = hyadex_server.build_codex_cmd(
            "chat",
            hyadex_server.STORE.sessions["chat"],
            "Do the work",
            Path("/tmp/current.json"),
        )

        self.assertNotIn("goals", command)

    async def test_manager_creation_waits_for_provider_reconfiguration(self) -> None:
        entered = asyncio.Event()
        release = asyncio.Event()

        async def reconfigure_provider() -> None:
            async with hyadex_server.CODEX_GOALS_CONFIG_LOCK:
                entered.set()
                await release.wait()

        manager = Mock()
        manager.add_notification_handler = Mock()
        with patch.object(hyadex_server, "CodexAppServerManager", return_value=manager) as manager_type:
            reconfigure = asyncio.create_task(reconfigure_provider())
            create_manager = None
            try:
                await asyncio.wait_for(entered.wait(), timeout=2)
                create_manager = asyncio.create_task(hyadex_server.codex_app_server_manager())
                await asyncio.sleep(0)
                self.assertFalse(create_manager.done())
                release.set()
                await asyncio.wait_for(reconfigure, timeout=2)
                self.assertIs(await asyncio.wait_for(create_manager, timeout=2), manager)
            finally:
                release.set()
                await asyncio.gather(reconfigure, return_exceptions=True)
                if create_manager is not None and not create_manager.done():
                    create_manager.cancel()
                    await asyncio.gather(create_manager, return_exceptions=True)
        self.assertEqual(manager_type.call_args.kwargs["app_server_args"], ())

    async def test_compaction_metadata_closes_and_rejects_late_duplicate_start(
        self,
    ) -> None:
        start = {
            "seq": 10,
            "type": "codex_compaction_started",
            "ts": "2026-08-02T12:00:00Z",
            "operation_id": "compact-1",
            "thread_id": "thread",
            "run_id": "compact-1",
        }
        completed = {
            **start,
            "seq": 11,
            "type": "codex_compaction_completed",
            "ts": "2026-08-02T12:00:01Z",
            "status": "completed",
        }
        late_duplicate = {
            **start,
            "seq": 12,
            "ts": "2026-08-02T12:00:02Z",
        }
        with patch.object(hyadex_server.STORE, "save", AsyncMock()):
            await hyadex_server.update_session_event_metadata("chat", start)
            self.assertIn(
                "codex:compaction:compact-1",
                hyadex_server.STORE.sessions["chat"][
                    "_active_codex_compactions"
                ],
            )
            await hyadex_server.update_session_event_metadata("chat", completed)
            await hyadex_server.update_session_event_metadata(
                "chat",
                late_duplicate,
            )

        session = hyadex_server.STORE.sessions["chat"]
        self.assertNotIn("_active_codex_compactions", session)
        self.assertIn(
            "codex:compaction:compact-1",
            session["_codex_compaction_terminal_keys"],
        )

    async def test_start_only_compaction_is_recovered_after_restart(self) -> None:
        hyadex_server.STORE.sessions["chat"]["_active_codex_compactions"] = {
            "codex:compaction:compact-1": {
                "lifecycle_key": "codex:compaction:compact-1",
                "operation_id": "compact-1",
                "thread_id": "thread",
                "run_id": "compact-1",
                "started_seq": 10,
                "started_at": "2026-08-02T12:00:00Z",
            },
        }
        recorded: list[dict[str, object]] = []

        async def append_recovery(
            session_id: str,
            event_type: str,
            payload: dict[str, object],
        ) -> dict[str, object]:
            event = {
                "seq": 11,
                "type": event_type,
                "ts": "2026-08-02T12:00:01Z",
                **payload,
            }
            recorded.append(event)
            await hyadex_server.update_session_event_metadata(session_id, event)
            return event

        with (
            patch.object(hyadex_server, "append_event", side_effect=append_recovery),
            patch.object(hyadex_server.STORE, "save", AsyncMock()),
        ):
            recovered = (
                await hyadex_server.recover_abandoned_codex_compactions_after_start()
            )

        self.assertEqual(recovered, 1)
        self.assertEqual(recorded[0]["type"], "codex_compaction_completed")
        self.assertEqual(recorded[0]["status"], "interrupted")
        self.assertTrue(recorded[0]["recovered_after_restart"])
        self.assertNotIn(
            "_active_codex_compactions",
            hyadex_server.STORE.sessions["chat"],
        )


if __name__ == "__main__":
    unittest.main()
