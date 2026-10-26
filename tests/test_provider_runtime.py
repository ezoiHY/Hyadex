"""Provider control data stays bounded and separate from exact user content."""
import unittest

from hyadex.providers import runtime


class ProviderRuntimeTests(unittest.TestCase):
    def test_payload_keeps_exact_user_text_and_copies_control_environment(self):
        text = "  /literal\n用户原文\n"
        environment = {"HYADEX_CHAT_ID": "disposable-chat"}
        payload = runtime.ProviderTurnPayload(text, "runtime-only context", environment)
        environment["HYADEX_CHAT_ID"] = "another-chat"
        self.assertEqual(payload.user_prompt, text)
        self.assertEqual(payload.runtime_env, {"HYADEX_CHAT_ID": "disposable-chat"})
        runtime.assert_provider_user_message_unchanged(text, text)
        with self.assertRaises(runtime.ProviderRuntimeContextError):
            runtime.assert_provider_user_message_unchanged(text.strip(), text)

    def test_context_checks_encoded_bytes_and_rejects_invalid_unicode(self):
        for value in ("📦" * 8193, "\ud800", None):
            with self.subTest(value_type=type(value).__name__), self.assertRaises(runtime.ProviderRuntimeContextError):
                runtime.validate_provider_runtime_context(value)

    def test_runtime_environment_rejects_server_secret_and_unrelated_variables(self):
        for value in ({"HYADEX_AGENT_TOKEN": "synthetic"}, {"PATH": "/synthetic"},
                      {"HYADEX_CHAT_ID": "bad\x00value"}, {"HYADEX_CHAT_ID": "\ud800"}):
            with self.subTest(names=list(value)), self.assertRaises(runtime.ProviderRuntimeContextError):
                runtime.validate_provider_runtime_env(value)

    def test_scrub_removes_old_and_current_authority_but_keeps_ordinary_environment(self):
        private = {"ZENITHBOT_AGENT_TOKEN": "old", "ZENITHDOCK_AGENT_TOKEN": "old",
                   "HYADEX_AGENT_TOKEN": "new", "HYADEX_PROVIDER_AUTHORITY_FILE": "/synthetic",
                   "HYADEX_CROSS_CHAT_HANDLE_12": "handle", "HYADEX_PROVIDER_TEAM_MAIL_PREBOUND": "retired"}
        environment = {"PATH": "/ordinary", **private}
        runtime.scrub_provider_runtime_environment(environment)
        self.assertEqual(environment, {"PATH": "/ordinary"})
        environment = {"PATH": "/ordinary", **private}
        runtime.scrub_provider_runtime_environment(environment, shadow=True)
        self.assertEqual(environment, {"PATH": "/ordinary", **{name: "" for name in private}})
