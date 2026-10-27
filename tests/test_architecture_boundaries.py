"""Structural rules for agent_server.py that keep its parts decoupled.

These are cheap source checks; they fail when a change reintroduces a
coupling that was removed on purpose.
"""
import ast
from pathlib import Path
import re
import unittest
from tests import server_source

ROOT = Path(__file__).resolve().parents[1]
SOURCE = server_source.text()
TREE = ast.parse(SOURCE)


def top_level(name: str) -> ast.AST:
    return next(node for node in TREE.body if getattr(node, "name", None) == name)


def segment(node: ast.AST) -> str:
    return ast.get_source_segment(SOURCE, node) or ""


class ArchitectureBoundaryTests(unittest.TestCase):
    def test_event_writers_do_not_know_their_consumers(self):
        # Live sockets and group discussions subscribe to EVENTS; the log only
        # writes, sequences and publishes.
        for name in ("append_event", "append_durable_event_batch_locked"):
            with self.subTest(writer=name):
                body = segment(top_level(name))
                self.assertNotRegex(body, r"\bGROUPS\b|\bAUTOPILOT\b|HUB\.broadcast")
                self.assertIn("EVENTS.deliver", body)
                self.assertIn("EVENTS.observe", body)

    def test_session_store_lock_stays_inside_the_store(self):
        store = top_level("SessionStore")
        outside = SOURCE.replace(segment(store), "")
        self.assertNotRegex(outside, r"STORE\._lock")
        for path in (ROOT / "hyadex").rglob("*.py"):
            with self.subTest(module=str(path.relative_to(ROOT))):
                self.assertNotRegex(path.read_text(), r"STORE\._lock")

    def test_auth_middleware_has_no_route_paths(self):
        # Policies are declared on routes (RouteAuth); the middleware only
        # enforces the declaration of the route a request reaches.
        body = segment(top_level("require_agent_token"))
        self.assertEqual(re.findall(r'"/api/[^"]+"', body), [])

    def test_agent_and_admin_routes_declare_their_auth(self):
        import agent_server
        from tests.test_route_inventory import registered_routes

        for method, path in sorted(registered_routes(agent_server.app)):
            if method in {"HEAD", "WS"} or not path.startswith(("/api/agent/", "/api/admin/")):
                continue
            with self.subTest(route=f"{method} {path}"):
                auth = agent_server.route_auth_for_scope(agent_server.app, method, path)
                expected = ({"agent_helper", "provider_mcp"} if path.startswith("/api/agent/")
                            else {"native_admin", "restart"})
                self.assertIn(auth.kind, expected)

    def test_runtime_state_lives_only_on_state_objects(self):
        # RunState owns turn lifecycle state, ServerState the server's own
        # lifecycle and ProviderState the shared provider managers. A test patching an old module global would patch nothing
        # the server reads.
        from hyadex.core import runs, server
        from hyadex.providers import state as providers

        names = "|".join([*runs.LEGACY_NAMES, *server.LEGACY_NAMES, *providers.LEGACY_NAMES])
        definitions = re.findall(rf"^({names})\s*[:=]", SOURCE, re.M)
        self.assertEqual(definitions, [])
        pattern = re.compile(rf"(?:agent_server|server)\.({names})\b|patch\.object\(\s*(?:agent_server|server)\s*,\s*[\"']({names})[\"']")
        for path in (ROOT / "tests").glob("*.py"):
            with self.subTest(test=path.name):
                self.assertEqual(pattern.findall(path.read_text()), [])


if __name__ == "__main__":
    unittest.main()
