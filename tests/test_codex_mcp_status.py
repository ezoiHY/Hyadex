"""Codex MCP status (read-only, Claude MCP v1 envelope) with a fake app-server manager."""
from contextlib import ExitStack
import json
import unittest
from unittest.mock import AsyncMock, patch

from tests import hyadex_server as hs


SECRET_ROW = {
    "name": "dayone",
    "runtimeStatus": "failed",
    "authStatus": "bearerToken",
    "httpOrigin": "https://private.example",
    "serverInfo": {"name": "https://private.example/server", "version": "Bearer top-secret-token"},
    "toolsError": "Authorization: Bearer top-secret-token",
    "tools": {"private-tool": {"name": "private-tool", "description": "top-secret-description"}},
    "resources": [],
    "resourceTemplates": [],
}


def row(name, runtime_status, auth="unsupported", tools=None, **extra):
    return {
        "name": name,
        "runtimeStatus": runtime_status,
        "authStatus": auth,
        "tools": tools if tools is not None else {},
        "resources": [],
        "resourceTemplates": [],
        **extra,
    }


class FakeCodexManager:
    def __init__(self, pages, *, loaded=True, error=None):
        self.pages = list(pages)
        self.calls: list[tuple[str, dict, float | None]] = []
        self.loaded = loaded
        self.error = error

    def is_thread_loaded(self, thread_id):
        return self.loaded

    async def request(self, method, params, *, timeout=None):
        self.calls.append((method, dict(params), timeout))
        if self.error is not None:
            raise self.error
        return self.pages.pop(0)


class CodexMCPStatusTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.session_id = "codex-mcp-chat"
        self.session = {
            "id": self.session_id,
            "backend": hs.BACKEND_CODEX,
            "codex_thread_id": "thread-1",
            "cwd": "/tmp",
        }
        self.state = ExitStack()
        self.state.enter_context(patch.object(hs.STORE, "sessions", {self.session_id: self.session}))
        self.state.enter_context(patch.object(hs, "DELETING_SESSIONS", set()))
        self.state.enter_context(patch.object(hs, "DELETED_SESSION_TOMBSTONES", set()))
        self.state.enter_context(patch.object(hs, "CODEX_TRANSPORT", hs.CODEX_TRANSPORT_APP_SERVER))

    async def asyncTearDown(self) -> None:
        self.state.close()

    async def snapshot(self, manager):
        with patch.object(hs, "codex_app_server_manager", AsyncMock(return_value=manager)), patch.object(
            hs, "existing_codex_app_server_manager", return_value=manager,
        ):
            return await hs.codex_mcp_snapshot(self.session_id)

    async def test_status_vocabulary_and_secret_allowlist(self) -> None:
        manager = FakeCodexManager([{"data": [
            row(hs.CODEX_PROVIDER_MCP_NAME, "connected"),
            SECRET_ROW,
            row("calendar", "connected", tools={"a": {}, "b": {}},
                serverInfo={"name": "calendar-server", "version": "1.2.3"}),
            row("starting", "starting"),
            row("not-started", "notStarted"),
            row("auth", "authenticationRequired"),
            row("logged-out", "connected", auth="notLoggedIn"),
            row("cancelled", "cancelled"),
            row("off", "disabled", auth="notLoggedIn"),
            row("nothing", None),
            row("future", "futureState"),
        ], "nextCursor": None}])

        response = await self.snapshot(manager)

        self.assertEqual(manager.calls, [(
            "mcpServerStatus/list",
            {"detail": "toolsAndAuthOnly", "threadId": "thread-1"},
            hs.CODEX_MCP_STATUS_TIMEOUT_SECONDS,
        )])
        self.assertEqual(response["version"], 1)
        self.assertTrue(response["available"])
        self.assertEqual(response["transport"], "app-server")
        self.assertTrue(response["generation"].startswith("codexmcp_"))
        self.assertTrue(response["session_loaded"])
        self.assertFalse(response["truncated"])
        self.assertIsNone(response["reason"])
        self.assertIsNone(response["action"])
        servers = {item["name"]: item for item in response["servers"]}
        self.assertNotIn(hs.CODEX_PROVIDER_MCP_NAME, servers)
        self.assertEqual({name: item["status"] for name, item in servers.items()}, {
            "dayone": "failed",
            "calendar": "connected",
            "starting": "pending",
            "not-started": "pending",
            "auth": "needs-auth",
            "logged-out": "needs-auth",
            "cancelled": "failed",
            "off": "disabled",
            "nothing": "unknown",
            "future": "unknown",
        })
        self.assertFalse(servers["off"]["enabled"])
        self.assertTrue(servers["calendar"]["enabled"])
        self.assertIsNone(servers["nothing"]["error"])
        self.assertIsNotNone(servers["future"]["error"])
        self.assertEqual(servers["dayone"]["error"], "Codex could not connect to this MCP server.")
        self.assertIsNone(servers["dayone"]["server_info"])
        self.assertEqual(servers["dayone"]["tool_count"], 1)
        self.assertEqual(servers["calendar"]["tool_count"], 2)
        self.assertEqual(servers["calendar"]["server_info"], {"name": "calendar-server", "version": "1.2.3"})
        self.assertEqual(set(servers["calendar"]), {
            "name", "status", "enabled", "error", "scope", "server_info", "tool_count",
        })
        self.assertTrue(all(item["scope"] is None for item in servers.values()))
        encoded = json.dumps(response)
        for secret in ("private.example", "top-secret", "private-tool", "Authorization"):
            self.assertNotIn(secret, encoded)

    async def test_follows_cursor_and_caps_server_count(self) -> None:
        pages = [
            {"data": [row(f"server-{i:03d}", "connected") for i in range(start, start + 60)],
             "nextCursor": f"cursor-{start}"}
            for start in (0, 60)
        ]
        manager = FakeCodexManager(pages)

        response = await self.snapshot(manager)

        self.assertEqual([call[1].get("cursor") for call in manager.calls], [None, "cursor-0"])
        self.assertEqual(len(response["servers"]), hs.CLAUDE_MCP_MAX_SERVERS)
        self.assertTrue(response["truncated"])

    async def test_unloaded_thread_is_not_named_and_not_loaded(self) -> None:
        manager = FakeCodexManager([{"data": [row("calendar", None)]}], loaded=False)

        response = await self.snapshot(manager)

        self.assertNotIn("threadId", manager.calls[0][1])
        self.assertFalse(response["session_loaded"])
        self.assertEqual(response["servers"][0]["status"], "unknown")

    async def test_rpc_failure_and_exec_transport_are_unavailable_not_5xx(self) -> None:
        manager = FakeCodexManager([], error=RuntimeError("Bearer top-secret-token"))
        response = await self.snapshot(manager)
        self.assertFalse(response["available"])
        self.assertIsNone(response["generation"])
        self.assertEqual(response["servers"], [])
        self.assertEqual(response["reason"]["code"], "codex_mcp_connection_failed")
        self.assertTrue(response["reason"]["retryable"])
        self.assertNotIn("top-secret", json.dumps(response))

        malformed = FakeCodexManager([{"servers": []}])
        self.assertFalse((await self.snapshot(malformed))["available"])

        with patch.object(hs, "CODEX_TRANSPORT", hs.CODEX_TRANSPORT_EXEC):
            exec_manager = FakeCodexManager([])
            response = await self.snapshot(exec_manager)
        self.assertFalse(response["available"])
        self.assertEqual(response["transport"], "exec")
        self.assertEqual(response["reason"]["code"], "codex_mcp_exec_transport")
        self.assertFalse(exec_manager.calls)

    async def test_wrong_backend_and_missing_session(self) -> None:
        self.session["backend"] = hs.BACKEND_CLAUDE
        with self.assertRaises(hs.HTTPException) as wrong:
            await hs.codex_mcp_snapshot(self.session_id)
        self.assertEqual(wrong.exception.status_code, 409)
        self.assertEqual(wrong.exception.detail["code"], "codex_mcp_wrong_backend")
        with self.assertRaises(hs.HTTPException) as missing:
            await hs.codex_mcp_snapshot("missing")
        self.assertEqual(missing.exception.status_code, 404)

    def test_route_is_standard_authenticated_get(self) -> None:
        path = f"/api/sessions/{self.session_id}/codex/mcp"
        self.assertFalse(hs.is_agent_helper_route("GET", path))
        methods = {
            method
            for route in hs.app.routes
            if getattr(route, "path", None) == "/api/sessions/{session_id}/codex/mcp"
            for method in getattr(route, "methods", set())
        }
        self.assertEqual(methods, {"GET"})


if __name__ == "__main__":
    unittest.main()
