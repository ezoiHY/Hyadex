"""Public listener and credential regressions against the actual ASGI app."""
from __future__ import annotations

import base64
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import agent_server as server


class NetworkSecurityTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.object(server, "AGENT_TOKEN", "test-native-token"))
        self.client = TestClient(server.app)
        self.addCleanup(self.client.close)

    def test_headers_authorize_but_url_credentials_do_not(self):
        for headers in ({"Authorization": "Bearer test-native-token"},
                        {"x-hyadex-token": "test-native-token"}):
            self.assertEqual(self.client.get("/api/sessions", headers=headers).status_code, 200)
        for path in ("/api/sessions", "/api/sessions?token=test-native-token"):
            self.assertEqual(self.client.get(path).status_code, 401)

    def test_server_api_does_not_grant_cross_origin_access(self):
        for origin in ("https://untrusted.example", "http://localhost:1234", "null"):
            with self.subTest(origin=origin):
                response = self.client.get("/api/sessions", headers={
                    "Authorization": "Bearer test-native-token", "Origin": origin,
                })
                self.assertEqual(response.status_code, 200)
                self.assertNotIn("access-control-allow-origin", response.headers)
                preflight = self.client.options("/api/sessions", headers={
                    "Origin": origin, "Access-Control-Request-Method": "POST",
                    "Access-Control-Request-Headers": "authorization,content-type",
                })
                self.assertEqual(preflight.status_code, 400)
                self.assertNotIn("access-control-allow-origin", preflight.headers)

    def test_websocket_requires_url_free_credentials(self):
        endpoint = "/api/emergency-alerts/events"
        with self.assertRaises(WebSocketDisconnect):
            with self.client.websocket_connect(endpoint + "?token=test-native-token") as socket:
                socket.receive_json()
        encoded = base64.urlsafe_b64encode(b"test-native-token").decode().rstrip("=")
        with self.client.websocket_connect(endpoint, subprotocols=[
            server.EMERGENCY_WEBSOCKET_PROTOCOL, "hyadex-token." + encoded,
        ]) as socket:
            self.assertEqual(socket.accepted_subprotocol, server.EMERGENCY_WEBSOCKET_PROTOCOL)

    def test_no_token_is_allowed_only_on_loopback(self):
        for token in ("", "   "):
            with patch.object(server, "AGENT_TOKEN", token):
                for address in ("127.0.0.1", "127.2.3.4", "::1", "::ffff:127.0.0.1", "localhost"):
                    with patch.object(server.SERVER, "bind_address", address):
                        server.validate_listener_auth()
                for address in ("0.0.0.0", "::", "192.0.2.10", "server.example"):
                    with patch.object(server.SERVER, "bind_address", address):
                        with self.assertRaisesRegex(RuntimeError, "HYADEX_AGENT_TOKEN"):
                            server.validate_listener_auth()

    def test_token_allows_non_loopback(self):
        with patch.object(server.SERVER, "bind_address", "0.0.0.0"):
            server.validate_listener_auth()

    def test_health_resource_diagnostics_match_admission_defaults(self):
        with patch.object(server, "MIN_START_AVAILABLE_MEM_MB", 2048), \
             patch.object(server, "JOB_MIN_AVAILABLE_MEM_MB", 1536), \
             patch.object(server, "MAX_ACTIVE_AGENT_RUNS", 0), \
             patch.object(server, "JOB_MAX_ACTIVE_RUNS", 0):
            for available in (None, 1536, 2047, 2048, 3174):
                result = server.scheduled_job_resource_diagnostics({"available_mem_mb": available}, 0)
                self.assertEqual(result["effective_min_available_mem_mb"], 2048)
                self.assertEqual(result["resource_blocked"], available is not None and available < 2048)
                if result["resource_blocked"]:
                    self.assertIn("2 GiB (2048 MiB)", result["resource_blocking_reason"])


class ListenerLifespanTests(unittest.IsolatedAsyncioTestCase):
    async def test_asgi_startup_rejects_exposure_before_loading_state(self):
        with patch.object(server.SERVER, "bind_address", "0.0.0.0"), \
             patch.object(server, "AGENT_TOKEN", ""), \
             patch.object(server.STORE, "load") as load:
            with self.assertRaisesRegex(RuntimeError, "HYADEX_AGENT_TOKEN"):
                async with server.lifespan(server.app):
                    self.fail("unprotected public listener started")
            load.assert_not_called()
