"""Claude's live thinking and text: completed blocks pair with their own streamed items."""
from __future__ import annotations

import unittest
from unittest import mock

from hyadex.providers.claude import live_output
from hyadex.providers.claude.live_output import ClaudeLiveOutput


def stream(message_id: str, blocks: list[tuple[str, str]]) -> list[dict]:
    """Stream events for one message: (kind, text) per block, in order."""
    events = [{"type": "StreamEvent", "event": {"type": "message_start", "message": {"id": message_id}}}]
    for index, (kind, text) in enumerate(blocks):
        events.append({"type": "StreamEvent", "event": {
            "type": "content_block_start", "index": index, "content_block": {"type": kind}}})
        if text:
            delta = {"type": "thinking_delta", "thinking": text} if kind == "thinking" else {"type": "text_delta", "text": text}
            events.append({"type": "StreamEvent", "event": {"type": "content_block_delta", "index": index, "delta": delta}})
    return events


def completed(message_id: str, kind: str, text: str) -> dict:
    block = {"type": "thinking", "thinking": text} if kind == "thinking" else {"type": "text", "text": text}
    return {"type": "AssistantMessage", "message_id": message_id, "content": [block]}


class ClaudeLiveOutputTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.cleared: list[str] = []
        self.finished_with: set = set()

        async def clear(session_id, run_id, item_id=None, phase=None):
            self.cleared.append(item_id)

        async def finish(session_id, run_id, completed_ids):
            self.finished_with = set(completed_ids)

        async def noop(*args, **kwargs):
            return None

        for name, value in (("update_reasoning_summary_stream", noop), ("clear_reasoning_summary_stream", clear),
                            ("finish_reasoning_summary_stream", finish), ("update_assistant_text_stream", noop),
                            ("clear_assistant_text_stream", noop), ("finish_assistant_text_stream", noop)):
            patcher = mock.patch.object(live_output, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.output = ClaudeLiveOutput("chat", "run")

    async def feed(self, events):
        for event in events:
            await self.output.observe(event)

    async def test_a_thinking_block_left_out_of_its_message_does_not_shift_later_ones(self):
        # The first message streams a thinking block its completed message leaves out.
        await self.feed(stream("msg_a", [("thinking", ""), ("tool_use", "")]))
        await self.feed(stream("msg_b", [("thinking", "Ready for the next question."), ("text", "Done.")]))
        await self.output.complete(completed("msg_b", "thinking", "Ready for the next question."))
        await self.output.complete(completed("msg_b", "text", "Done."))
        await self.output.finish(ended_early=False)
        # The finished thought is cleared as complete; only the empty stray stays open.
        self.assertIn("msg_b:0", self.cleared)
        self.assertIn("msg_b:0", self.output._completed_thinking)
        self.assertEqual(self.output._open["thinking"], ["msg_a:0"])

    async def test_messages_without_ids_still_pair_in_order(self):
        await self.feed(stream("", [("thinking", "Plan.")]))
        await self.output.complete({"type": "AssistantMessage", "content": [{"type": "thinking", "thinking": "Plan."}]})
        self.assertEqual(self.output._open["thinking"], [])


if __name__ == "__main__":
    unittest.main()
