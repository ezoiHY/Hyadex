"""A Codex subagent's approvals and questions reach its parent chat's user."""
from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from tests import hyadex_server


class CodexSubagentInteractionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.active = {
            "chat": {"provider_thread_id": "parent", "interactive_app_server": True,
                     "codex_approval_policy": "on-request"},
        }
        self.patches = [
            patch.dict(hyadex_server.RUNS.active, self.active, clear=True),
            patch.dict(hyadex_server.CODEX_SUBAGENT_SESSION_INDEX, {"child": "chat", "finished": "chat",
                                                                    "other-child": "other"}, clear=True),
            patch.dict(hyadex_server.CODEX_SUBAGENT_LIVE_GENERATIONS, {"child": 1, "other-child": 1}, clear=True),
            patch.dict(hyadex_server.STORE.sessions, {"chat": {"id": "chat", "codex_approval_policy": "never"}},
                       clear=True),
        ]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)

    def test_live_child_of_the_running_turn_is_interactive(self) -> None:
        interactive = hyadex_server.codex_request_is_interactive
        self.assertTrue(interactive("chat", "parent"))
        self.assertTrue(interactive("chat", "child"))
        # Not live, another chat's child, or no interactive turn: still declined.
        self.assertFalse(interactive("chat", "finished"))
        self.assertFalse(interactive("chat", "other-child"))
        self.active["chat"]["interactive_app_server"] = False
        self.assertFalse(interactive("chat", "child"))
        hyadex_server.RUNS.active.pop("chat")
        self.assertFalse(interactive("chat", "child"))

    def test_child_uses_the_policy_of_the_turn_that_spawned_it(self) -> None:
        policy = hyadex_server.active_codex_approval_policy
        self.assertEqual(policy("chat", "child"), "on-request")
        # A thread that is not part of the running turn follows the chat's settings.
        self.assertEqual(policy("chat", "finished"), "never")

    async def test_child_command_approval_is_shown_and_answered(self) -> None:
        append_event = AsyncMock()
        with (
            patch.dict(hyadex_server.CODEX_PENDING_INTERACTIONS, {}, clear=True),
            patch.object(hyadex_server, "append_event", append_event),
            patch.object(hyadex_server, "update_codex_pending_session_metadata", AsyncMock()),
            patch.object(hyadex_server, "existing_codex_app_server_manager_for_thread", return_value=None),
        ):
            request = asyncio.create_task(hyadex_server.handle_codex_server_request(
                9, "item/commandExecution/requestApproval",
                {"threadId": "child", "turnId": "child-turn", "itemId": "cmd", "command": "make test",
                 "availableDecisions": ["accept", "cancel"]},
            ))
            for _ in range(50):
                if hyadex_server.CODEX_PENDING_INTERACTIONS:
                    break
                await asyncio.sleep(0)
            interaction_id, pending = next(iter(hyadex_server.CODEX_PENDING_INTERACTIONS.items()))
            self.assertEqual(pending["session_id"], "chat")
            self.assertEqual(append_event.await_args.args[:2], ("chat", "codex_interaction_requested"))
            await hyadex_server.resolve_codex_interaction("chat", interaction_id, {"decision": "accept"})
            self.assertEqual(await asyncio.wait_for(request, 2), {"decision": "accept"})


if __name__ == "__main__":
    unittest.main()
