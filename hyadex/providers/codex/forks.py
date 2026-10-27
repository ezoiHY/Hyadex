"""Codex thread forks: forking, binding and retiring forked threads."""
from __future__ import annotations

import asyncio
from collections import deque
import logging.handlers
from typing import Any

from hyadex.core.config import (
    DEFAULT_BACKEND,
    DEFAULT_CWD,
    FORK_INTERNAL_PURPOSES,
    MAX_FORK_MEMORY_CHARS,
)
from hyadex.core.event_log import (
    is_native_goal_steer_event,
    project_provider_history_event_for_egress,
)
from hyadex.core.server_logging import concise_error_message
from hyadex.core.sessions import (
    ABANDONED_FORK_PROVIDER_THREADS,
    iter_session_events,
    write_abandoned_fork_thread_ids,
)
from hyadex.core.util import compact_memory_text
from hyadex.providers.claude.history import session_provider_id
from hyadex.providers.codex.app_server import CodexAppServerProtocolError

logger = logging.getLogger("hyadex-server")

ABANDONED_FORK_PROVIDER_THREADS_LOCK = asyncio.Lock()


def build_fork_memory(
    parent: dict[str, Any],
    parent_id: str,
    *,
    reason: str | None = None,
    exclude_run_id: str | None = None,
) -> str:
    provider_id = session_provider_id(parent)
    header = [
        "[Hyadex memory fork]",
        "This is a fresh provider thread seeded from a compact memory dump because the original provider-level fork was unavailable.",
        "Use this memory as background context. Do not treat it as a new user request.",
        "",
        f"Parent Hyadex session: {parent_id}",
        f"Parent title: {parent.get('title') or 'Untitled'}",
        f"Backend: {parent.get('backend') or DEFAULT_BACKEND}",
        f"Working directory: {parent.get('cwd') or DEFAULT_CWD}",
    ]
    if provider_id:
        header.append(f"Original provider session/thread: {provider_id}")
    if reason:
        header.append(f"Fork fallback reason: {compact_memory_text(reason, 800)}")

    lines: list[str] = header + ["", "Recent rough conversation:"]
    # Select the most recent useful conversational events, not merely the last
    # raw events. A single tool-heavy turn can contain thousands of tool events
    # and previously displaced every user/assistant message from the fallback.
    internal_run_ids = {
        str(event.get("run_id"))
        for event in iter_session_events(parent_id)
        if event.get("purpose") in FORK_INTERNAL_PURPOSES
        and str(event.get("run_id") or "").strip()
    }
    events: deque[dict[str, Any]] = deque(maxlen=160)
    for event in iter_session_events(parent_id):
        event = project_provider_history_event_for_egress(event, parent_id)
        run_id = str(event.get("run_id") or "").strip()
        event_type = str(event.get("type") or "")
        if (
            (exclude_run_id and run_id == exclude_run_id)
            or event.get("purpose") in FORK_INTERNAL_PURPOSES
            or (run_id and run_id in internal_run_ids)
        ):
            continue
        if event_type in {"turn_started", "assistant_text", "turn_finished", "artifact_created"} or is_native_goal_steer_event(event):
            events.append(event)
        elif event_type == "reasoning_summary" and event.get("phase") == "commentary":
            events.append(event)
    assistant_runs = {
        event.get("run_id")
        for event in events
        if event.get("type") == "assistant_text" and str(event.get("text") or "").strip()
    }

    for event in events:
        event_type = event.get("type")
        if event_type == "turn_started" or is_native_goal_steer_event(event):
            text = compact_memory_text(event.get("prompt") or "")
            if text:
                lines.append(f"\nUser:\n{text}")
        elif event_type == "assistant_text":
            text = compact_memory_text(event.get("text") or "")
            if text:
                lines.append(f"\nAssistant:\n{text}")
        elif event_type == "turn_finished" and event.get("run_id") not in assistant_runs:
            text = compact_memory_text(event.get("result_text") or "")
            if text:
                lines.append(f"\nAssistant:\n{text}")
        elif event_type == "reasoning_summary":
            text = compact_memory_text(event.get("text") or "", 900)
            if text:
                lines.append(f"\nAssistant commentary:\n{text}")
        elif event_type == "artifact_created":
            artifact = event.get("artifact") if isinstance(event.get("artifact"), dict) else {}
            title = artifact.get("title") or artifact.get("filename") or artifact.get("id") or "artifact"
            note = compact_memory_text(artifact.get("text") or "", 600)
            artifact_line = f"\nArtifact: {title}"
            if artifact.get("content_type"):
                artifact_line += f" ({artifact.get('content_type')})"
            if note:
                artifact_line += f"\n{note}"
            lines.append(artifact_line)

    memory = "\n".join(lines).strip()
    if len(memory) > MAX_FORK_MEMORY_CHARS:
        trim_prefix = (
            "[Hyadex memory fork]\n[Older memory trimmed]\n"
        )
        tail_budget = max(0, MAX_FORK_MEMORY_CHARS - len(trim_prefix))
        memory = trim_prefix + memory[-tail_budget:].lstrip()
        memory = memory[:MAX_FORK_MEMORY_CHARS]
    return memory


def codex_raw_developer_message(text: str) -> dict[str, Any]:
    return {
        "type": "message",
        "role": "developer",
        "content": [{"type": "input_text", "text": text}],
    }


class CodexForkCleanupError(CodexAppServerProtocolError):
    """A created provider fork has no crash-safe cleanup owner yet."""

    def __init__(self, thread_id: str) -> None:
        self.thread_id = str(thread_id or "").strip()
        super().__init__(
            "thread/fork could not establish crash-safe cleanup ownership",
            request_sent=True,
            safe_to_retry=False,
        )


async def persist_abandoned_fork_provider_thread(thread_id: str) -> bool:
    """Durably fence a provider fork that could not be retired immediately."""

    clean_thread_id = str(thread_id or "").strip()
    if not clean_thread_id:
        return True
    async with ABANDONED_FORK_PROVIDER_THREADS_LOCK:
        pending = set(ABANDONED_FORK_PROVIDER_THREADS)
        pending.add(clean_thread_id)
        try:
            await asyncio.to_thread(
                write_abandoned_fork_thread_ids,
                pending,
            )
        except OSError as exc:
            # Retain the in-memory fence even though a crash would lose it.
            # A staged local child, when present, remains durable and lets the
            # next startup retry this ledger write before removing the child.
            ABANDONED_FORK_PROVIDER_THREADS.add(clean_thread_id)
            logger.warning(
                "could not persist abandoned provider fork thread=%s: %s",
                clean_thread_id,
                concise_error_message(exc),
            )
            return False
        ABANDONED_FORK_PROVIDER_THREADS.clear()
        ABANDONED_FORK_PROVIDER_THREADS.update(pending)
        return True


async def forget_abandoned_fork_provider_thread(thread_id: str) -> bool:
    """Remove a cleanup fence only after another durable owner exists."""

    clean_thread_id = str(thread_id or "").strip()
    if not clean_thread_id:
        return True
    async with ABANDONED_FORK_PROVIDER_THREADS_LOCK:
        if clean_thread_id not in ABANDONED_FORK_PROVIDER_THREADS:
            return True
        remaining = ABANDONED_FORK_PROVIDER_THREADS - {clean_thread_id}
        try:
            await asyncio.to_thread(
                write_abandoned_fork_thread_ids,
                remaining,
            )
        except OSError as exc:
            logger.warning(
                "could not clear abandoned provider fork thread=%s: %s",
                clean_thread_id,
                concise_error_message(exc),
            )
            return False
        ABANDONED_FORK_PROVIDER_THREADS.clear()
        ABANDONED_FORK_PROVIDER_THREADS.update(remaining)
        return True
