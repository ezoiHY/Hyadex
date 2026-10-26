"""Synthetic credentials only. No native login or model requests."""
import json
import ast
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from fastapi import HTTPException
import provider_connections as connections




class RuntimeBindingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = connections.ConnectionStore(Path(self.temp.name) / "private")
        self.input = {"base_url": "https://gateway.invalid/api", "api_key": "synthetic-first-key",
                      "model": "vendor/model", "protocol": "anthropic", "auth_header": "bearer", "expected_revision": 0}

    def chat(self, backend="claude"):
        self.store.write(backend, 0, self.input, "verified")
        session = {"backend": backend, "provider_connection": "custom"}
        selected = self.store.bind(session)
        return {**session, "provider_connection_revision": selected["credential_id"], "model": selected["model"]}

    def test_binding_survives_replacement_but_forget_revokes_without_native_fallback(self):
        chat = self.chat()
        self.store.write("claude", 1, {**self.input, "expected_revision": 1, "api_key": "synthetic-second-key"}, "verified")
        self.assertEqual(self.store.for_session(chat)["api_key"], "synthetic-first-key")
        self.store.write("claude", 2, None)
        with self.assertRaisesRegex(HTTPException, "disconnected"): self.store.for_session(chat)
        with self.assertRaises(HTTPException): self.store.bind({"backend": "claude", "provider_connection": "custom"})
        self.store.write("claude", 3, {**self.input, "expected_revision": 3}, "verified")
        reloaded = connections.ConnectionStore(self.store.root)
        self.assertEqual(reloaded.for_session(chat)["api_key"], self.input["api_key"])
        self.assertTrue(reloaded.catalog("claude", session=chat)["available"])
        self.assertTrue(reloaded.bind({"backend": "claude", "provider_connection": "custom"}))
        reloaded.write("claude", 4, {**self.input, "api_key": "another-account", "expected_revision": 4}, "verified")
        with self.assertRaisesRegex(HTTPException, "disconnected"): reloaded.for_session(chat)
        reloaded.write("claude", 5, {**self.input, "base_url": "https://different.invalid", "expected_revision": 5}, "verified")
        with self.assertRaisesRegex(HTTPException, "disconnected"): reloaded.for_session(chat)

    def test_legacy_forget_revokes_and_inflight_output_still_redacts(self):
        for backend in ("claude",):
            chat = self.chat(backend)
            (self.store.root / f"{backend}.json").write_text(json.dumps({"revision": 2, "configured": False}))
            with self.assertRaisesRegex(HTTPException, "disconnected"): self.store.for_session(chat)
            self.assertEqual(self.store.redact(chat, {"text": "reply synthetic-first-key"}), {"text": "reply <api-key>"})
            self.store.write(backend, 2, {**self.input, "expected_revision": 2}, "verified")
            self.assertEqual(self.store.for_session(chat)["api_key"], self.input["api_key"])
            self.store.write(backend, 3, None)
            with self.assertRaisesRegex(HTTPException, "disconnected"): self.store.for_session(chat)

    def test_failed_forget_keeps_original_connection(self):
        chat = self.chat()
        with patch.object(connections.os, "replace", side_effect=OSError("synthetic disk failure")):
            with self.assertRaises(HTTPException): self.store.write("claude", 1, None)
        self.assertEqual(self.store.for_session(chat)["api_key"], "synthetic-first-key")

    def test_missing_invalid_cross_backend_and_symlink_bindings_fail_closed(self):
        chat = self.chat()
        for bad in [{**chat, "provider_connection_revision": None}, {**chat, "provider_connection_revision": "../claude"}, {**chat, "backend": "opencode"}]:
            with self.assertRaises(HTTPException): self.store.for_session(bad)
        record = self.store.root / f"claude-{chat['provider_connection_revision']}.json"
        record.unlink()
        record.symlink_to(self.store.root / "claude.json")
        with self.assertRaises(HTTPException): self.store.for_session(chat)

    def test_native_chat_has_no_overrides_or_writes(self):
        self.assertEqual(self.store.for_session({"backend": "claude"}), {})
        self.assertEqual(self.store.claude_overrides({"backend": "claude"}), ({}, None))
        self.assertFalse(self.store.root.exists())

    def test_summary_never_duplicates_cached_endpoint_inventory_or_probes(self):
        chat = self.chat()
        inventory = [{"value": f"vendor/model-{i}", "label": "Large catalog label" * 30} for i in range(500)]
        self.store.catalog_cache[("claude", 1)] = (connections.time.monotonic(), inventory)
        with patch.object(connections, "discover_models", side_effect=AssertionError("No discovery on summary")):
            summary = self.store.catalog("claude", session=chat, summary=True)
            self.assertTrue(summary["configured"])
            self.assertEqual(summary["model"], chat["model"])
            self.assertNotIn("models", summary)
            self.assertLess(len(json.dumps(summary)), 250)
            self.assertGreater(len(self.store.catalog("claude", session=chat)["models"]), 499)

    def test_custom_api_key_is_removed_from_error_and_event_projection(self):
        chat = self.chat()
        self.assertEqual(self.store.redact(chat, {"error": "rejected synthetic-first-key", "blocks": ["synthetic-first-key"]}),
                         {"error": "rejected <api-key>", "blocks": ["<api-key>"]})

    def test_claude_private_settings_pin_endpoint_auth_model_and_subagent_limit(self):
        chat = {**self.chat(), "subagent_limit": 2}
        env, path = self.store.claude_overrides(chat)
        self.assertEqual(Path(path).stat().st_mode & 0o777, 0o600)
        self.assertNotIn("synthetic-first-key", path)
        self.assertEqual(json.loads(Path(path).read_text())["env"], env)
        self.assertEqual(env["ANTHROPIC_AUTH_TOKEN"], "synthetic-first-key")
        self.assertEqual(env["ANTHROPIC_API_KEY"], "")
        self.assertEqual(env["CLAUDE_CODE_OAUTH_TOKEN"], "")
        self.assertEqual(env["ANTHROPIC_DEFAULT_HAIKU_MODEL"], "vendor/model")
        self.assertEqual(env["CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS"], "2")


    def test_custom_without_model_cannot_borrow_native_default(self):
        self.input["model"] = None
        chat = self.chat()
        with self.assertRaisesRegex(HTTPException, "Choose a model"):
            self.store.claude_overrides(chat)


    def test_claude_receives_root_for_anthropic_api_prefix(self):
        self.input["base_url"] += "/v1"
        claude = self.chat("claude")
        self.assertEqual(self.store.claude_overrides(claude)[0]["ANTHROPIC_BASE_URL"], "https://gateway.invalid/api")

    def test_public_catalog_has_no_keys_and_session_projection_never_networks(self):
        chat = self.chat()
        with patch.object(connections, "discover_models", return_value=[{"value": "catalog/model", "label": "catalog/model"}]) as discover:
            self.assertTrue(self.store.catalog("claude")["available"])
            self.assertEqual(discover.call_count, 1)
            self.store.catalog("claude")
            self.store.catalog("claude", session=chat)
            self.assertEqual(discover.call_count, 1)
        self.assertNotIn("synthetic-first-key", json.dumps(self.store.public("claude")))
        self.assertEqual(self.store.public("claude")["scope"], "per_chat")


    def test_custom_claude_success_is_not_proof_of_native_login(self):
        with patch.object(Path, "home", return_value=Path(self.temp.name)), patch.dict(connections.os.environ, {}, clear=True), patch.object(connections.sys, "platform", "linux"):
            self.assertFalse(connections.native_credentials_present("claude", {"installed": True, "authenticated": True}))
