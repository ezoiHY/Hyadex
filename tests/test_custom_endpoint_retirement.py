"""Chats saved against a removed custom API endpoint keep history but never run.

Running them on the native login would send their history to another account.
"""
from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from fastapi import HTTPException

from tests.session_store_fixture import make_namespace


class CustomEndpointRetirementTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="custom-endpoint-retirement-")
        self.addCleanup(temp.cleanup)
        self.ns = make_namespace(Path(temp.name))

    def test_saved_custom_chats_allow_organizing_but_refuse_runtime_changes(self):
        for backend, field in (("codex", "codex_provider"), ("claude", "provider_connection")):
            session = {"id": "saved", "backend": backend, field: "custom", "model": "gateway-model"}
            with self.subTest(field=field):
                self.assertEqual(self.ns["retired_session_runtime_message"](session),
                                 self.ns["REMOVED_ENDPOINT_MESSAGE"])
                for patch in ({"title": "Renamed"}, {"folder": "Archive"}, {"archived": True}):
                    preview = self.ns["preview_session_runtime_update"](session, patch)
                    self.assertEqual(preview["model"], "gateway-model")
                for patch in ({"model": "native-model"}, {"effort": "high"}, {"backend": "codex"}):
                    with self.assertRaises(HTTPException) as raised:
                        self.ns["preview_session_runtime_update"](session, patch)
                    self.assertEqual(raised.exception.status_code, 410)

    def test_native_and_default_chats_are_unaffected(self):
        for session in ({"backend": "codex"}, {"backend": "claude", "provider_connection": "default"},
                        {"backend": "codex", "codex_provider": "default"}):
            with self.subTest(session=session):
                self.assertIsNone(self.ns["retired_session_runtime_message"](session))


if __name__ == "__main__":
    unittest.main()
