"""Native address discovery without starting providers or inspecting real networks."""
import unittest
from unittest.mock import patch

from fastapi import APIRouter
from fastapi.testclient import TestClient
from hyadex.api.app import ApplicationBindings, create_app
import agent_server
from hyadex.ops import instances as server_instances
from tests.runtime_state import patch_server_multiple


class ConnectionAddressTests(unittest.TestCase):
    def setUp(self):
        overrides = patch_server_multiple(agent_server, AGENT_TOKEN="synthetic-native-token",
                                   SERVER_BIND_ADDRESS="0.0.0.0", SERVER_PORT=7852,
                                   server_identity=lambda: "fixture-server")
        overrides.__enter__()
        self.addCleanup(overrides.close)
        self.ns = agent_server.__dict__
        router = APIRouter()
        router.add_api_route("/api/connection-addresses", agent_server.connection_addresses)
        self.client = TestClient(create_app(ApplicationBindings(router, agent_server.require_agent_token)))
        self.addCleanup(self.client.close)
        self.headers = {"X-Hyadex-Token": self.ns["AGENT_TOKEN"]}

    def test_discovery_requires_native_token_and_returns_binding_aware_networks_without_credentials(self):
        with patch.object(server_instances, "candidate_addresses", return_value=["http://127.0.0.1:7852", "http://192.168.1.2:7852"]), patch.object(server_instances, "tailscale_status", return_value={"status": "connected", "ipv4": "100.64.0.2"}):
            response = self.client.get("/api/connection-addresses", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"version": 1, "server_identity": "fixture-server", "urls": ["http://100.64.0.2:7852", "http://192.168.1.2:7852"]})
        self.assertNotIn(self.ns["AGENT_TOKEN"], response.text)

    def test_rejects_browser_query_bearer_and_duplicate_credentials_before_discovery(self):
        with patch.object(server_instances, "candidate_addresses") as discovery:
            for headers in ({}, {**self.headers, "Origin": "https://example.com"},
                            {**self.headers, "Sec-Fetch-Mode": "cors"},
                            {"Authorization": "Bearer " + self.ns["AGENT_TOKEN"]},
                            {**self.headers, "X-ZenithDock-Token": self.ns["AGENT_TOKEN"]}):
                self.assertIn(self.client.get("/api/connection-addresses", headers=headers).status_code, (401, 403))
            self.assertEqual(self.client.get("/api/connection-addresses?token=fixture", headers=self.headers).status_code, 401)
            discovery.assert_not_called()

    def test_loopback_binding_never_advertises_other_interfaces(self):
        agent_server.SERVER.bind_address = "127.0.0.1"
        with patch.object(server_instances, "tailscale_status", return_value={"status": "connected", "ipv4": "100.64.0.2"}):
            response = self.client.get("/api/connection-addresses", headers=self.headers)
        self.assertEqual(response.json()["urls"], [])
