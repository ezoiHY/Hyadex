"""Retired browser sharing exposes no routes or exception to native auth."""
import logging
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

import agent_server as server


class RetiredSharingTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.object(server, "AGENT_TOKEN", "native-test-token"))
        self.client = TestClient(server.app)
        self.addCleanup(self.client.close)

    def test_browser_and_management_routes_are_absent(self):
        prefixes = ("/share/", "/shared-chat/", "/interactive-chat/",
                    "/api/admin/chat-shares", "/api/admin/interactive-chat-shares")
        self.assertFalse([route.path for route in server.app.routes
                          if getattr(route, "path", "").startswith(prefixes)])
        paths = ("/share/old-private-token", "/shared-chat/share_" + "a" * 32,
                 "/interactive-chat/interactive_" + "b" * 32,
                 "/interactive-chat/assets/index.js",
                 "/api/admin/chat-shares/chat", "/api/admin/interactive-chat-shares/chat")
        for path in paths:
            for method in ("GET", "HEAD", "POST", "DELETE"):
                with self.subTest(path=path, method=method):
                    response = self.client.request(method, path, headers={
                        "x-hyadex-token": "native-test-token",
                    })
                    self.assertEqual(response.status_code, 404)

    def test_old_credentials_do_not_authorize_native_api(self):
        for path in ("/api/sessions", "/api/admin/chat-shares/chat",
                     "/api/admin/interactive-chat-shares/chat"):
            response = self.client.get(path + "?token=old-share-token", headers={
                "Cookie": "Hyadex-View=old-share-token; Hyadex-Interactive=old-share-token",
                "Origin": "https://guest.example",
            })
            self.assertEqual(response.status_code, 401)
            self.assertNotIn("access-control-allow-origin", response.headers)

    def test_old_link_tokens_stay_redacted_in_404_logs(self):
        record = logging.LogRecord("uvicorn.access", logging.INFO, "", 0,
            '%s - "%s %s HTTP/%s" %d',
            ("127.0.0.1", "GET", "/share/old-private-token/media/0?token=another-secret", "1.1", 404), None)
        self.assertTrue(server.PollingAccessLogFilter().filter(record))
        self.assertNotIn("old-private-token", record.getMessage())
        self.assertNotIn("another-secret", record.getMessage())
        self.assertIn("/share/<redacted>/media/0", record.getMessage())

    def test_saved_accepted_turn_retains_its_attribution(self):
        event = {"type": "turn_queued", "queued_id": "accepted-before-retirement",
                 "backend": "codex", "prompt": "Saved request", "file_ids": [],
                 "shared_chat_id": "interactive_" + "a" * 32,
                 "shared_chat_request_id": "saved-request", "author_label": "Shared chat"}
        recovered = server.queued_turn_from_event(event, {"id": "chat", "backend": "codex"}, 1)
        for key in ("shared_chat_id", "shared_chat_request_id", "author_label", "prompt"):
            self.assertEqual(recovered[key], event[key])

    def test_new_turns_cannot_claim_retired_share_attribution(self):
        for field, value in (("shared_chat_id", "interactive_" + "a" * 32),
                             ("shared_chat_request_id", "new-request")):
            for receipted in (False, True):
                body = {"prompt": "Never admit", field: value}
                if receipted:
                    body["client_request_id"] = "new-native-request"
                with self.subTest(field=field, receipted=receipted):
                    response = self.client.post("/api/sessions/missing/turns", json=body,
                        headers={"x-hyadex-token": "native-test-token"})
                    self.assertEqual(response.status_code, 422)


class NativeSessionAvailabilityTests(unittest.TestCase):
    def test_side_chat_availability_keeps_lifecycle_guards(self):
        with patch.dict(server.STORE.sessions, {"chat": {"id": "chat"}}, clear=True), \
             patch.object(server, "DELETING_SESSIONS", set()), \
             patch.object(server, "DELETED_SESSION_TOMBSTONES", set()):
            self.assertTrue(server.native_session_available("chat"))
            self.assertFalse(server.native_session_available("../chat"))
            for field in ("_history_import_initializing", "_fork_initializing"):
                with patch.dict(server.STORE.sessions["chat"], {field: True}):
                    self.assertFalse(server.native_session_available("chat"))
            for name in ("DELETING_SESSIONS", "DELETED_SESSION_TOMBSTONES"):
                with patch.object(server, name, {"chat"}):
                    self.assertFalse(server.native_session_available("chat"))
