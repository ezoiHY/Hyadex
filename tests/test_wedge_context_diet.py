"""Context-diet regression tests.

HyadexServer used to append 1.7-4.3k chars of mostly static helper-CLI
instructions to every Codex/Claude user prompt and steer. Codex retains prior
user messages across compactions, so that boilerplate became the dominant
post-compaction context floor. These tests pin the new contract: static text
lives once in the thread-level developer instructions.
"""

import unittest
from pathlib import Path
from unittest.mock import patch

import agent_server


class ThreadInstructionTests(unittest.TestCase):
    def thread_instructions(self) -> tuple[str, str]:
        session = {"id": "chat-1", "backend": agent_server.BACKEND_CODEX, "cwd": "/repo"}
        with patch.object(
            agent_server,
            "codex_user_developer_instructions",
            return_value="",
        ):
            codex = agent_server.codex_thread_instructions("chat-1", session)
        claude = agent_server.session_system_prompt(
            "chat-1",
            {"id": "chat-1", "backend": agent_server.BACKEND_CLAUDE, "cwd": "/repo"},
            Path("/tmp/manifest.json"),
        )
        return codex, claude

    def test_static_usage_rules_appear_exactly_once(self) -> None:
        codex, claude = self.thread_instructions()

        for instructions in (codex, claude):
            self.assertEqual(
                instructions.count(agent_server.PROVIDER_AUTHORITY_USAGE_INSTRUCTIONS.strip()),
                1,
            )
            self.assertIn("never user content", instructions)
            self.assertNotIn("--authority-file", instructions)
            self.assertNotIn("[Hyadex provider authority]", instructions)
            # Cross-chat messaging was removed; no delivery protocol remains.
            self.assertNotIn("[End delivery]", instructions)
            self.assertNotIn("cross-chat", instructions.lower())
            # Static text must stay generic: no concrete authority path or chat id.
            self.assertNotIn("--chat-id chat-1", instructions)

    def test_policy_version_migrates_resumed_codex_threads(self) -> None:
        self.assertEqual(agent_server.CODEX_THREAD_POLICY_VERSION, "11")

    def test_static_addendum_is_format_safe(self) -> None:
        # Both preludes are rendered with str.format, so the appended static
        # text must not contain literal braces.
        self.assertNotIn("{", agent_server.PROVIDER_THREAD_INSTRUCTION_ADDENDUM)
        self.assertNotIn("}", agent_server.PROVIDER_THREAD_INSTRUCTION_ADDENDUM)
        agent_server.CODEX_PROMPT_PRELUDE.format(
            manifest_path="/tmp/manifest.json",
            terminal_session="hx_sess_1",
            chat_id="chat-1",
        )
        agent_server.CLAUDE_PROMPT_PRELUDE.format()


if __name__ == "__main__":
    unittest.main()
