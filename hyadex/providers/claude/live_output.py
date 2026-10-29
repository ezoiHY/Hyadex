"""Claude's thinking and text while they are written.

The Agent SDK runs with partial messages, so each content block arrives as
``StreamEvent`` deltas before its completed ``AssistantMessage``. Deltas of the
main conversation feed the live lanes: thinking the reasoning summary stream
(as Codex reasoning does), text the assistant text stream. The completed block
stays the durable record and clears its live item. Thinking that never
completes is kept as a ``reasoning_summary`` marked ``partial``, as Codex keeps
reasoning; text is kept as a ``partial`` ``assistant_text`` when the run ended
early (Stop, an interrupt, a failure). Subagent deltas (``parent_tool_use_id``)
are not streamed.
"""
from __future__ import annotations

from typing import Any
import uuid

from hyadex.core.config import BACKEND_CLAUDE
from hyadex.features.files import run_event_metadata
from hyadex.providers.claude.subagents import claude_sdk_field, claude_sdk_type
from hyadex.providers.codex.reasoning_stream import (
    clear_reasoning_summary_stream,
    finish_reasoning_summary_stream,
    update_reasoning_summary_stream,
)
from hyadex.providers.text_stream import (
    clear_assistant_text_stream,
    finish_assistant_text_stream,
    update_assistant_text_stream,
)

_TEXT_BLOCKS = {"TextBlock", "text"}
_THINKING_BLOCKS = {"ThinkingBlock", "thinking"}


class ClaudeLiveOutput:
    """The content blocks of one run that are still being written."""

    def __init__(self, session_id: str, run_id: str) -> None:
        self.session_id = session_id
        self.run_id = run_id
        self._message_id = ""
        # Block index in the current message -> (kind, live item id).
        self._blocks: dict[int, tuple[str, str]] = {}
        # Live item ids still waiting for their completed block, oldest first.
        self._open: dict[str, list[str]] = {"text": [], "thinking": []}
        self._completed_text: set[str] = set()
        self._completed_thinking: set[str | tuple[str, str]] = set()

    async def observe(self, message: Any) -> bool:
        """Stream one partial message; whether ``message`` was a stream event."""
        if claude_sdk_type(message) != "StreamEvent":
            return False
        if claude_sdk_field(message, "parent_tool_use_id"):
            return True
        event = claude_sdk_field(message, "event", {})
        if not isinstance(event, dict):
            return True
        kind = event.get("type")
        index = event.get("index")
        if kind == "message_start":
            started = event.get("message") if isinstance(event.get("message"), dict) else {}
            self._message_id = str(started.get("id") or f"msg_{uuid.uuid4().hex[:12]}")
            self._blocks.clear()
        elif kind == "content_block_start" and isinstance(index, int):
            block = event.get("content_block") if isinstance(event.get("content_block"), dict) else {}
            block_type = block.get("type")
            if block_type in self._open:
                item_id = f"{self._message_id or 'msg'}:{index}"
                self._blocks[index] = (block_type, item_id)
                self._open[block_type].append(item_id)
        elif kind == "content_block_delta" and isinstance(index, int) and index in self._blocks:
            block_type, item_id = self._blocks[index]
            delta = event.get("delta") if isinstance(event.get("delta"), dict) else {}
            if block_type == "text" and delta.get("type") == "text_delta":
                await update_assistant_text_stream(
                    self.session_id, self.run_id, item_id, delta.get("text"),
                    {"backend": BACKEND_CLAUDE, **run_event_metadata(self.run_id)},
                )
            elif block_type == "thinking" and delta.get("type") == "thinking_delta":
                await update_reasoning_summary_stream(
                    self.session_id, self.run_id, item_id,
                    {"summaryIndex": 0, "delta": delta.get("thinking")},
                    {"backend": BACKEND_CLAUDE},
                )
        return True

    async def complete(self, message: Any) -> None:
        """Clear the live items that a completed ``AssistantMessage`` now records."""
        if claude_sdk_type(message) != "AssistantMessage" or claude_sdk_field(message, "parent_tool_use_id"):
            return
        content = claude_sdk_field(message, "content", [])
        for block in content if isinstance(content, list) else []:
            block_type = claude_sdk_type(block)
            kind = "text" if block_type in _TEXT_BLOCKS else "thinking" if block_type in _THINKING_BLOCKS else ""
            if not kind or not self._open[kind]:
                continue
            # Claude writes one block at a time, so a completed block is the
            # oldest one still open.
            item_id = self._open[kind].pop(0)
            if kind == "text":
                self._completed_text.add(item_id)
                await clear_assistant_text_stream(self.session_id, self.run_id, item_id)
            else:
                self._completed_thinking.add(item_id)
                await clear_reasoning_summary_stream(self.session_id, self.run_id, item_id)

    async def finish(self, *, ended_early: bool) -> None:
        """Keep thinking, and text if the run ended early, still being written."""
        await finish_reasoning_summary_stream(self.session_id, self.run_id, self._completed_thinking)
        await finish_assistant_text_stream(self.session_id, self.run_id, self._completed_text, keep=ended_early)
