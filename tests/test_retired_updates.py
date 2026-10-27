"""Retired in-app updates cannot expose or execute an installation workflow."""
import os
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from pydantic import ValidationError

from tests import hyadex_server as server
class RetiredUpdatesTests(unittest.TestCase):
    def test_update_routes_are_absent(self):
        with patch.object(server, 'AGENT_TOKEN', 'native-test-token'), \
             patch.dict(os.environ, {'HYADEX_MANAGED_UPDATES': '1'}):
            client = TestClient(server.app)
            try:
                for suffix in ('', '/check', '/start', '/cancel', '/ensure'):
                    response = client.request('GET' if not suffix else 'POST',
                        '/api/admin/update' + suffix, json={},
                        headers={'x-hyadex-token': 'native-test-token'})
                    self.assertEqual(response.status_code, 404, suffix)
            finally:
                client.close()

    def test_force_update_request_is_not_reinterpreted_as_restart(self):
        with self.assertRaises(ValidationError):
            server.ServerRestartRequest(
                request_id='12345678-1234-1234-1234-123456789012',
                expected_server_identity='server', expected_server_instance_id='instance',
                confirmed=True, force=True, force_confirmed=True,
                expected_update_schedule_id='a' * 32)

    def test_force_update_http_body_is_rejected_before_restart(self):
        with patch.object(server, "AGENT_TOKEN", "native-test-token"), \
             patch.object(server, "start_forced_server_restart_signal_thread") as signal:
            client = TestClient(server.app)
            try:
                response = client.post("/api/admin/restart", headers={"x-hyadex-token": "native-test-token"},
                    json={"request_id": "12345678-1234-1234-1234-123456789012",
                          "expected_server_identity": "server", "expected_server_instance_id": "instance",
                          "confirmed": True, "force": True, "force_confirmed": True,
                          "expected_update_schedule_id": "a" * 32})
                self.assertEqual(response.status_code, 422)
                signal.assert_not_called()
            finally:
                client.close()
