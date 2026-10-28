"""Claude MCP status/control (v1): admission, projection and capability flags."""
import asyncio
from contextlib import ExitStack
import json
import unittest
from unittest.mock import AsyncMock, patch

from tests import hyadex_server as hs
from tests.runtime_state import patch_server
from tests.test_claude_sdk_client import SlowReconnectMCPFactory
from hyadex.providers.claude.sdk import ClaudeSDKControlTimeout
from hyadex.providers.claude.sdk_manager import ClaudeSDKSupervisorManager

RAW_MCP_STATUS = {
    "mcpServers": [
        {
            "name": hs.CLAUDE_PROVIDER_MCP_SERVER_NAME,
            "status": "failed",
        },
        {
            "name": "hyadex",
            "status": "connected",
            "scope": "user",
        },
        {
            "name": "dayone",
            "status": "failed",
            "error": "Bearer top-secret-token at https://private.example/path?key=secret",
            "scope": "user",
            "serverInfo": {
                "name": "https://private.example/server",
                "version": "Bearer top-secret-token",
            },
            "config": {
                "command": "/private/bin/dayone",
                "env": {"PRIVATE_TOKEN": "top-secret-token"},
                "headers": {"Authorization": "Bearer top-secret-token"},
            },
            "tools": [
                {
                    "name": "private-tool",
                    "description": "top-secret-description",
                }
            ],
        },
        {
            "name": "calendar",
            "status": "connected",
            "scope": "project",
            "serverInfo": {"name": "calendar-server", "version": "1.2.3"},
            "tools": [{"name": "events"}, {"name": "availability"}],
        },
        {
            "name": "future",
            "status": "future-provider-state",
            "scope": "secret-scope",
        },
    ]
}


class FakeMCPManager:
    def __init__(self) -> None:
        self.get_calls: list[dict[str, object]] = []
        self.mutate_calls: list[dict[str, object]] = []
        self.started = asyncio.Event()
        self.release: asyncio.Event | None = None
        self.error: Exception | None = None

    async def get_mcp_status(self, session_id: str, **kwargs: object):
        self.get_calls.append({"session_id": session_id, **kwargs})
        self.started.set()
        if self.release is not None:
            await self.release.wait()
        if self.error is not None:
            raise self.error
        return RAW_MCP_STATUS, "claudemcp_exact-owner-generation"

    async def mutate_mcp_server(self, session_id: str, **kwargs: object):
        self.mutate_calls.append({"session_id": session_id, **kwargs})
        self.started.set()
        if self.release is not None:
            await self.release.wait()
        if self.error is not None:
            raise self.error
        return RAW_MCP_STATUS, "claudemcp_exact-owner-generation"


class ClaudeMCPManagementTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.session_id = "claude-mcp-chat"
        self.session = {
            "id": self.session_id,
            "title": "MCP chat",
            "backend": hs.BACKEND_CLAUDE,
            "cwd": "/tmp",
        }
        self.state = ExitStack()
        self.state.enter_context(patch.object(hs.STORE, "sessions", {self.session_id: self.session}))
        for name, value in (
            ("ACTIVE", {}),
            ("BUSY_SESSIONS", set()),
            ("CURRENT_TURNS", {}),
            ("SESSION_TURN_TASKS", {}),
            ("SESSION_LIFECYCLE_LOCKS", {}),
            ("SERVER_MAINTENANCE_SESSIONS", set()),
            ("DELETING_SESSIONS", set()),
            ("DELETED_SESSION_TOMBSTONES", set()),
        ):
            self.state.enter_context(patch_server(hs, name, value))

    async def asyncTearDown(self) -> None:
        self.state.close()

    def mcp_patches(self, manager: FakeMCPManager):
        return (
            patch.object(hs, "CLAUDE_TRANSPORT", hs.CLAUDE_TRANSPORT_AGENT_SDK),
            patch.object(hs, "claude_sdk_dependency_available", return_value=True),
            patch.object(
                hs,
                "build_claude_sdk_options",
                return_value=({"cwd": "/tmp"}, "profile-a", "/usr/bin/claude"),
            ),
            patch.object(
                hs,
                "claude_sdk_manager",
                AsyncMock(return_value=manager),
            ),
            patch.object(hs, "server_lifecycle_admission_blocker", return_value=None),
        )

    async def call_with_patches(
        self,
        manager: FakeMCPManager,
        request: hs.ClaudeMCPControlRequest | None = None,
    ) -> dict[str, object]:
        patches = self.mcp_patches(manager)
        for selected in patches:
            selected.start()
        try:
            return await hs.manage_claude_mcp(self.session_id, request)
        finally:
            for selected in reversed(patches):
                selected.stop()

    async def test_get_returns_v1_allowlisted_snapshot_without_provider_secrets(self) -> None:
        manager = FakeMCPManager()

        response = await self.call_with_patches(manager)

        self.assertEqual(response["version"], 1)
        self.assertTrue(response["available"])
        self.assertEqual(response["transport"], "agent-sdk")
        self.assertEqual(response["generation"], "claudemcp_exact-owner-generation")
        self.assertTrue(response["session_loaded"])
        self.assertFalse(response["truncated"])
        self.assertIsNone(response["reason"])
        self.assertIsNone(response["action"])
        servers = {item["name"]: item for item in response["servers"]}
        self.assertNotIn(hs.CLAUDE_PROVIDER_MCP_SERVER_NAME, servers)
        self.assertIn("hyadex", servers)
        self.assertEqual(
            servers["dayone"]["error"],
            "Claude could not connect to this MCP server.",
        )
        self.assertEqual(servers["dayone"]["tool_count"], 1)
        self.assertIsNone(servers["dayone"]["server_info"])
        self.assertEqual(servers["calendar"]["server_info"], {
            "name": "calendar-server",
            "version": "1.2.3",
        })
        self.assertEqual(servers["future"]["status"], "unknown")
        self.assertIsNone(servers["future"]["scope"])
        encoded = json.dumps(response)
        for secret in (
            "top-secret-token",
            "private.example",
            "/private/bin/dayone",
            "Authorization",
            "PRIVATE_TOKEN",
            "private-tool",
            "top-secret-description",
            "secret-scope",
        ):
            self.assertNotIn(secret, encoded)
        self.assertEqual(manager.get_calls, [{
            "session_id": self.session_id,
            "options": {"cwd": "/tmp"},
            "configuration_key": "profile-a",
        }])
        self.assertNotIn(self.session_id, hs.RUNS.maintenance)

    async def test_post_binds_action_to_required_exact_generation(self) -> None:
        manager = FakeMCPManager()
        request = hs.ClaudeMCPControlRequest(
            version=1,
            action="disable",
            server_name="calendar",
            expected_generation="claudemcp_exact-owner-generation",
        )

        response = await self.call_with_patches(manager, request)

        self.assertEqual(response["action"], {
            "type": "disable",
            "server_name": "calendar",
        })
        self.assertEqual(manager.mutate_calls, [{
            "session_id": self.session_id,
            "action": "disable",
            "server_name": "calendar",
            "expected_generation": "claudemcp_exact-owner-generation",
            "options": {"cwd": "/tmp"},
            "configuration_key": "profile-a",
        }])

    async def test_reconnect_all_is_one_server_side_control(self) -> None:
        manager = FakeMCPManager()
        request = hs.ClaudeMCPControlRequest(
            version=1,
            action="reconnect_all",
            expected_generation="claudemcp_exact-owner-generation",
        )

        response = await self.call_with_patches(manager, request)

        self.assertEqual(response["action"], {
            "type": "reconnect_all",
            "server_name": None,
        })
        self.assertEqual(len(manager.mutate_calls), 1)
        self.assertIsNone(manager.mutate_calls[0]["server_name"])

    async def test_reconnect_all_on_profile_with_many_needs_auth_connectors_succeeds(self) -> None:
        # A real profile lists many claude.ai connectors waiting for sign-in.
        # Reconnecting them cannot sign them in, so only failed servers are
        # retried, and their serialized retries get a per-server budget.
        factory = SlowReconnectMCPFactory(reconnect_delay=0.15)
        manager = ClaudeSDKSupervisorManager(
            client_factory=factory,
            idle_ttl_seconds=None,
            disconnect_timeout_seconds=0.05,
            control_timeout_seconds=0.25,
        )
        try:
            status = await self.call_with_patches(manager)
            client = factory.clients[0]
            client.mcp_servers = [
                {"name": "dayone", "status": "failed", "scope": "user"},
                {"name": "notes", "status": "failed", "scope": "project"},
                {"name": "off", "status": "disabled", "scope": "user"},
            ] + [
                {
                    "name": f"claude.ai Connector {index:02d}",
                    "status": "needs-auth",
                    "scope": "claudeai",
                }
                for index in range(16)
            ]

            response = await self.call_with_patches(
                manager,
                hs.ClaudeMCPControlRequest(
                    version=1,
                    action="reconnect_all",
                    expected_generation=str(status["generation"]),
                ),
            )

            self.assertEqual(
                [call for call in client.calls if call[0] == "reconnect_mcp_server"],
                [
                    ("reconnect_mcp_server", "dayone"),
                    ("reconnect_mcp_server", "notes"),
                ],
            )
            servers = {item["name"]: item for item in response["servers"]}
            self.assertEqual(servers["dayone"]["status"], "connected")
            self.assertEqual(servers["notes"]["status"], "connected")
            self.assertEqual(servers["off"]["status"], "disabled")
            self.assertEqual(
                {
                    item["status"]
                    for name, item in servers.items()
                    if name.startswith("claude.ai ")
                },
                {"needs-auth"},
            )
            self.assertEqual(response["generation"], status["generation"])
            self.assertNotIn(self.session_id, hs.RUNS.maintenance)
        finally:
            await manager.close_all()

    async def test_active_and_starting_turns_reject_status_before_sdk_access(self) -> None:
        for active_kind in ("busy", "active", "current", "starting"):
            with self.subTest(active_kind=active_kind):
                hs.RUNS.active.clear()
                hs.RUNS.busy.clear()
                hs.RUNS.current_turns.clear()
                hs.RUNS.turn_tasks.clear()
                task: asyncio.Task[None] | None = None
                if active_kind == "busy":
                    hs.RUNS.busy.add(self.session_id)
                elif active_kind == "active":
                    hs.RUNS.active[self.session_id] = {"run_id": "run-1"}
                elif active_kind == "current":
                    hs.RUNS.current_turns[self.session_id] = {"run_id": None}
                else:
                    task = asyncio.create_task(asyncio.Event().wait())
                    hs.RUNS.turn_tasks[self.session_id] = {task}
                manager = FakeMCPManager()
                try:
                    with self.assertRaises(hs.HTTPException) as raised:
                        await self.call_with_patches(manager)
                    self.assertEqual(raised.exception.status_code, 409)
                    self.assertEqual(
                        raised.exception.detail["code"],
                        "claude_mcp_turn_active",
                    )
                    self.assertFalse(manager.get_calls)
                finally:
                    if task is not None:
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)

    async def test_operation_reserves_maintenance_until_bounded_sdk_call_settles(self) -> None:
        manager = FakeMCPManager()
        manager.release = asyncio.Event()
        patches = self.mcp_patches(manager)
        for selected in patches:
            selected.start()
        try:
            operation = asyncio.create_task(
                hs.manage_claude_mcp(self.session_id)
            )
            await asyncio.wait_for(manager.started.wait(), 1)
            self.assertIn(
                self.session_id,
                hs.RUNS.maintenance,
            )
            # Turn admission and managed-update admission both inspect this
            # exact set under ACTIVE_LOCK before reserving work/restart.
            self.assertTrue(
                hs.RUNS.busy
                | hs.RUNS.maintenance
            )
            manager.release.set()
            await asyncio.wait_for(operation, 1)
            self.assertNotIn(
                self.session_id,
                hs.RUNS.maintenance,
            )
        finally:
            for selected in reversed(patches):
                selected.stop()

    async def test_print_and_missing_sdk_status_are_non_mutating_capability_responses(self) -> None:
        for transport, sdk_available, reason_code in (
            (hs.CLAUDE_TRANSPORT_PRINT, True, "claude_mcp_print_transport"),
            (hs.CLAUDE_TRANSPORT_AGENT_SDK, False, "claude_mcp_sdk_unavailable"),
        ):
            with self.subTest(transport=transport, sdk_available=sdk_available):
                with patch.object(hs, "CLAUDE_TRANSPORT", transport), patch.object(
                    hs,
                    "claude_sdk_dependency_available",
                    return_value=sdk_available,
                ):
                    response = await hs.manage_claude_mcp(self.session_id)
                self.assertFalse(response["available"])
                self.assertEqual(response["transport"], "print")
                self.assertIsNone(response["generation"])
                self.assertEqual(response["servers"], [])
                self.assertFalse(response["truncated"])
                self.assertEqual(response["reason"]["code"], reason_code)

    def test_projection_deduplicates_sorts_and_caps_server_rows(self) -> None:
        raw = {
            "mcpServers": [
                {"name": f"server-{index:03d}", "status": "connected"}
                for index in range(104, -1, -1)
            ]
            + [
                {
                    "name": "server-104",
                    "status": "failed",
                    "error": "duplicate must not win",
                }
            ]
        }

        response = hs.claude_mcp_snapshot(
            raw,
            generation="claudemcp_generation",
        )

        names = [item["name"] for item in response["servers"]]
        self.assertEqual(len(names), hs.CLAUDE_MCP_MAX_SERVERS)
        self.assertEqual(len(set(names)), len(names))
        self.assertEqual(names, sorted(names, key=lambda name: (name.casefold(), name)))
        self.assertTrue(response["truncated"])
        server_104 = next(
            item for item in response["servers"] if item["name"] == "server-104"
        )
        self.assertEqual(server_104["status"], "connected")

    def test_projection_rejects_spoofed_names_and_accepts_normal_unicode(self) -> None:
        accepted_names = [
            "Caf\u00e9",
            "join\u200cer",
            "join\u200der",
            "\u65e5\u5386",
            "\u062a\u0642\u0648\u064a\u0645",
        ]
        rejected_names = [
            " calendar ",
            "spoof\u202e",
            "control\u0085",
            "hidden\u200b",
            "Cafe\u0301",
        ]
        raw = {
            "mcpServers": [
                {"name": name, "status": "connected"}
                for name in accepted_names + rejected_names
            ]
        }

        response = hs.claude_mcp_snapshot(
            raw,
            generation="claudemcp_generation",
        )

        self.assertEqual(
            {item["name"] for item in response["servers"]},
            set(accepted_names),
        )
        self.assertTrue(response["truncated"])
        for value in rejected_names:
            self.assertNotIn(value, {item["name"] for item in response["servers"]})

        for metadata in rejected_names:
            with self.subTest(metadata=metadata):
                self.assertIsNone(
                    hs.claude_mcp_display_metadata(metadata, 240)
                )
        for metadata in accepted_names:
            with self.subTest(metadata=metadata):
                self.assertEqual(
                    hs.claude_mcp_display_metadata(metadata, 240),
                    metadata,
                )

    def test_projection_marks_duplicates_incomplete_and_never_clamps_tool_count(self) -> None:
        raw = {
            "mcpServers": [
                {
                    "name": "calendar",
                    "status": "connected",
                    "tools": [None] * (hs.CLAUDE_MCP_MAX_TOOL_COUNT + 1),
                },
                {"name": "calendar", "status": "failed"},
            ]
        }

        response = hs.claude_mcp_snapshot(
            raw,
            generation="claudemcp_generation",
        )

        self.assertTrue(response["truncated"])
        self.assertEqual(len(response["servers"]), 1)
        self.assertEqual(response["servers"][0]["status"], "connected")
        self.assertIsNone(response["servers"][0]["tool_count"])

    async def test_noncanonical_request_names_are_rejected_before_manager_control(self) -> None:
        manager = FakeMCPManager()
        for server_name in (
            " calendar ",
            "spoof\u202e",
            "control\u0085",
            "hidden\u200b",
            "Cafe\u0301",
        ):
            with self.subTest(server_name=server_name):
                request = hs.ClaudeMCPControlRequest(
                    version=1,
                    action="disable",
                    server_name=server_name,
                    expected_generation="generation",
                )
                with self.assertRaises(hs.HTTPException) as raised:
                    await self.call_with_patches(manager, request)
                self.assertEqual(raised.exception.status_code, 400)
                self.assertEqual(
                    raised.exception.detail["code"],
                    "claude_mcp_invalid_request",
                )

        reconnect_all = hs.ClaudeMCPControlRequest(
            version=1,
            action="reconnect_all",
            server_name="",
            expected_generation="generation",
        )
        with self.assertRaises(hs.HTTPException) as raised:
            await self.call_with_patches(manager, reconnect_all)
        self.assertEqual(raised.exception.status_code, 400)
        self.assertFalse(manager.mutate_calls)

    async def test_internal_provider_mcp_cannot_be_managed_from_public_api(self) -> None:
        manager = FakeMCPManager()
        request = hs.ClaudeMCPControlRequest(
            version=1,
            action="disable",
            server_name=hs.CLAUDE_PROVIDER_MCP_SERVER_NAME,
            expected_generation="generation",
        )

        with self.assertRaises(hs.HTTPException) as raised:
            await self.call_with_patches(manager, request)

        self.assertEqual(raised.exception.status_code, 404)
        self.assertEqual(
            raised.exception.detail["code"],
            "claude_mcp_server_not_found",
        )
        self.assertFalse(manager.mutate_calls)

    async def test_mutation_unavailable_wrong_backend_and_invalid_shape_fail_closed(self) -> None:
        request = hs.ClaudeMCPControlRequest(
            version=1,
            action="enable",
            server_name="calendar",
            expected_generation="generation",
        )
        with patch.object(hs, "CLAUDE_TRANSPORT", hs.CLAUDE_TRANSPORT_PRINT), patch.object(
            hs,
            "claude_sdk_dependency_available",
            return_value=True,
        ):
            with self.assertRaises(hs.HTTPException) as unavailable:
                await hs.manage_claude_mcp(self.session_id, request)
        self.assertEqual(unavailable.exception.status_code, 409)
        self.assertEqual(unavailable.exception.detail["code"], "claude_mcp_unavailable")

        self.session["backend"] = hs.BACKEND_CODEX
        with self.assertRaises(hs.HTTPException) as wrong_backend:
            await hs.manage_claude_mcp(self.session_id)
        self.assertEqual(wrong_backend.exception.status_code, 409)
        self.assertEqual(wrong_backend.exception.detail["code"], "claude_mcp_wrong_backend")
        self.session["backend"] = hs.BACKEND_CLAUDE

        manager = FakeMCPManager()
        invalid = hs.ClaudeMCPControlRequest(
            version=1,
            action="reconnect_all",
            server_name="calendar",
            expected_generation="generation",
        )
        with self.assertRaises(hs.HTTPException) as invalid_request:
            await self.call_with_patches(manager, invalid)
        self.assertEqual(invalid_request.exception.status_code, 400)
        self.assertFalse(manager.mutate_calls)

    async def test_sdk_failure_response_never_echoes_raw_exception(self) -> None:
        manager = FakeMCPManager()
        manager.error = RuntimeError(
            "Authorization: Bearer private-token https://private.example"
        )

        with self.assertRaises(hs.HTTPException) as raised:
            await self.call_with_patches(manager)

        self.assertEqual(raised.exception.status_code, 503)
        encoded = json.dumps(raised.exception.detail)
        self.assertNotIn("private-token", encoded)
        self.assertNotIn("private.example", encoded)
        self.assertEqual(
            raised.exception.detail["code"],
            "claude_mcp_connection_failed",
        )

    async def test_timeout_is_generic_and_maintenance_is_released(self) -> None:
        manager = FakeMCPManager()
        manager.error = ClaudeSDKControlTimeout("secret timeout detail")

        with self.assertRaises(hs.HTTPException) as raised:
            await self.call_with_patches(manager)

        self.assertEqual(raised.exception.status_code, 503)
        self.assertEqual(
            raised.exception.detail["code"],
            "claude_mcp_connection_failed",
        )
        self.assertNotIn("secret", json.dumps(raised.exception.detail))
        self.assertNotIn(self.session_id, hs.RUNS.maintenance)

    async def test_routes_are_standard_authenticated_api_not_agent_helpers(self) -> None:
        path = f"/api/sessions/{self.session_id}/claude/mcp"
        self.assertFalse(hs.is_agent_helper_route("GET", path))
        self.assertFalse(hs.is_agent_helper_route("POST", path))
        route_methods = {
            method
            for route in hs.app.routes
            if getattr(route, "path", None) == "/api/sessions/{session_id}/claude/mcp"
            for method in getattr(route, "methods", set())
        }
        self.assertEqual(route_methods, {"GET", "POST"})

    async def test_health_and_runtime_advertise_capability_version(self) -> None:
        with patch.object(hs, "claude_sdk_dependency_available", return_value=True), patch.object(
            hs,
            "CLAUDE_TRANSPORT",
            hs.CLAUDE_TRANSPORT_AGENT_SDK,
        ):
            runtime = await hs.claude_runtime_snapshot(self.session_id)
            health = await hs.health()

        self.assertTrue(runtime["features"]["mcp_management"])
        capability = health["capabilities"]["claude_controls"]
        self.assertEqual(capability["version"], 3)
        self.assertTrue(capability["features"]["mcp_management"])
        self.assertEqual(health["api_contract_version"], hs.API_CONTRACT_VERSION)

if __name__ == "__main__":
    unittest.main()
