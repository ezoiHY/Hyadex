"""Routes that clients depend on must stay registered.

The Mac client, the in-chat helper commands and the service manager are the
only callers of this server. Removing or renaming one of these routes breaks a
shipped caller, so the change has to update that caller and this list together.
"""
from __future__ import annotations

import unittest

from tests import hyadex_server
MAC_CLIENT = {
    ("GET", "/api/health"),
    ("GET", "/api/runtime/catalog"),
    ("GET", "/api/runtime/usage"),
    ("GET", "/api/sessions"),
    ("POST", "/api/sessions"),
    ("GET", "/api/sessions/{session_id}"),
    ("PATCH", "/api/sessions/{session_id}"),
    ("WS", "/api/sessions/{session_id}/events"),
    ("POST", "/api/sessions/{session_id}/turns"),
    ("POST", "/api/sessions/{session_id}/stop"),
    ("POST", "/api/sessions/{session_id}/read"),
    ("DELETE", "/api/sessions/{session_id}/queue/{queued_id}"),
    ("GET", "/api/sessions/{session_id}/claude/runtime"),
    ("GET", "/api/sessions/{session_id}/codex/runtime"),
    ("POST", "/api/sessions/{session_id}/claude/interactions/{interaction_id}/resolve"),
    ("POST", "/api/sessions/{session_id}/codex/interactions/{interaction_id}/resolve"),
    ("GET", "/api/sessions/{session_id}/diffs/{run_id}"),
    ("GET", "/api/sessions/{session_id}/files/{file_id}"),
    ("GET", "/api/sessions/{session_id}/links/file"),
    ("GET", "/api/groups"),
    ("POST", "/api/groups"),
    ("GET", "/api/groups/{group_id}"),
    ("GET", "/api/groups/{group_id}/events"),
    ("WS", "/api/groups/{group_id}/events/stream"),
    ("POST", "/api/groups/{group_id}/messages"),
    ("POST", "/api/groups/{group_id}/participants"),
    ("DELETE", "/api/groups/{group_id}/participants/{handle}"),
    ("POST", "/api/groups/{group_id}/stop"),
    ("GET", "/api/autopilot/runs"),
    ("POST", "/api/autopilot/runs"),
    ("GET", "/api/autopilot/runs/{run_id}"),
    ("POST", "/api/autopilot/runs/{run_id}/approve"),
    ("POST", "/api/autopilot/runs/{run_id}/pause"),
    ("POST", "/api/autopilot/runs/{run_id}/resume"),
    ("POST", "/api/autopilot/runs/{run_id}/abort"),
    ("POST", "/api/autopilot/runs/{run_id}/feedback"),
    ("GET", "/api/jobs"),
    ("PATCH", "/api/jobs/{job_id}"),
    ("POST", "/api/jobs/{job_id}/run"),
}

# hyadex_jobs, hyadex_publish, hyadex_emergency and hyadex_notify, run by
# agents inside a chat.
CHAT_HELPERS = {
    ("GET", "/api/agent/sessions/{session_id}/jobs"),
    ("POST", "/api/agent/sessions/{session_id}/jobs"),
    ("PATCH", "/api/agent/sessions/{session_id}/jobs/{job_id}"),
    ("DELETE", "/api/agent/sessions/{session_id}/jobs/{job_id}"),
    ("GET", "/api/agent/sessions/{session_id}/jobs/{job_id}/runs"),
    ("POST", "/api/agent/sessions/{session_id}/artifacts"),
    ("POST", "/api/agent/sessions/{session_id}/emergency-alerts"),
    ("POST", "/api/agent/sessions/{session_id}/notifications"),
}

# Kept for client features that are planned next.
PLANNED = {
    ("POST", "/api/sessions/{session_id}/files"),
    ("DELETE", "/api/sessions/{session_id}"),
    ("PATCH", "/api/sessions/{session_id}/queue/{queued_id}"),
    ("POST", "/api/sessions/{session_id}/queue/{queued_id}/move"),
    ("POST", "/api/sessions/{session_id}/queue/{queued_id}/run-now"),
    ("POST", "/api/jobs"),
    ("DELETE", "/api/jobs/{job_id}"),
    ("PATCH", "/api/groups/{group_id}"),
    ("DELETE", "/api/groups/{group_id}"),
    ("PUT", "/api/groups/{group_id}/goal"),
    ("DELETE", "/api/autopilot/runs/{run_id}"),
    ("GET", "/api/sessions/{session_id}/terminal"),
    ("DELETE", "/api/sessions/{session_id}/terminal"),
    ("POST", "/api/sessions/{session_id}/terminal/open"),
    ("POST", "/api/sessions/{session_id}/terminal/input"),
    ("POST", "/api/sessions/{session_id}/terminal/resize"),
    ("POST", "/api/sessions/{session_id}/terminal/action"),
    ("GET", "/api/sessions/{session_id}/terminal/windows"),
    ("WS", "/api/sessions/{session_id}/terminal/ws"),
}


def registered_routes(app) -> set[tuple[str, str]]:
    routes: set[tuple[str, str]] = set()

    def walk(items, prefix: str = "") -> None:
        for route in items:
            kind = type(route).__name__
            if kind == "_IncludedRouter":
                walk(route.original_router.routes, prefix + (getattr(route.include_context, "prefix", "") or ""))
            elif hasattr(route, "routes") and not hasattr(route, "endpoint"):
                walk(route.routes, prefix + getattr(route, "path", ""))
            elif "WebSocket" in kind:
                routes.add(("WS", prefix + route.path))
            else:
                for method in getattr(route, "methods", None) or ():
                    routes.add((method, prefix + route.path))

    walk(app.routes)
    return routes


class RouteInventoryTests(unittest.TestCase):
    def test_client_routes_remain_registered(self) -> None:
        routes = registered_routes(hyadex_server.app)
        for name, required in (("Mac client", MAC_CLIENT), ("chat helpers", CHAT_HELPERS),
                               ("planned client features", PLANNED)):
            with self.subTest(callers=name):
                self.assertEqual(sorted(required - routes), [])


if __name__ == "__main__":
    unittest.main()
