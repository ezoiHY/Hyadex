import unittest

from tests import hyadex_server
class AgentPromptFormatTests(unittest.TestCase):
    def test_both_agent_prompts_explain_renderable_latex_delimiters(self) -> None:
        claude_prompt = hyadex_server.SYSTEM_PROMPT.format(
            manifest_path="/tmp/manifest.json",
            terminal_session="hx_sess_123",
        )
        codex_prompt = hyadex_server.CODEX_PROMPT_PRELUDE.format(
            manifest_path="/tmp/manifest.json",
            terminal_session="hx_sess_123",
            chat_id="sess_123",
        )

        for prompt in (claude_prompt, codex_prompt):
            self.assertIn("`$...$`", prompt)
            self.assertIn("`$$...$$`", prompt)

    def test_provider_prompts_carry_no_cross_chat_protocol(self) -> None:
        claude_prompt = hyadex_server.CLAUDE_PROMPT_PRELUDE.format()
        codex_prompt = hyadex_server.CODEX_PROMPT_PRELUDE.format(
            manifest_path="/tmp/manifest.json",
            terminal_session="hx_sess_123",
            chat_id="sess_123",
        )

        # Cross-chat messaging was removed; its wait protocol must not linger.
        for prompt in (claude_prompt, codex_prompt):
            compact = " ".join(prompt.split())
            self.assertNotIn("pending=true", compact)
            self.assertNotIn("Chats `wait`", compact)
            self.assertNotIn("cross-chat", compact.lower())
            self.assertIn("Jobs, Publish, Emergency, Notify, and Ask", compact)


if __name__ == "__main__":
    unittest.main()
