"""Structural rules for agent_server.py that keep its parts decoupled.

These are cheap source checks; they fail when a change reintroduces a
coupling that was removed on purpose.
"""
import ast
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / "agent_server.py").read_text()
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


if __name__ == "__main__":
    unittest.main()
