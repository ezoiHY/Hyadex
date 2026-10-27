"""Stopping idle Claude background subagents."""
from __future__ import annotations

import asyncio
import logging.handlers
from typing import Any

from hyadex.core.config import (
    BACKEND_CLAUDE,
    CLAUDE_STOP_FENCE_ATTEMPT_TIMEOUT_SECONDS,
    DEFAULT_BACKEND,
    SUBAGENT_SNAPSHOT_STATE_LIMIT,
)
from hyadex.core.history_sync import append_durable_event
from hyadex.core.server_logging import concise_error_message
from hyadex.core.sessions import register_session_task, STORE
from hyadex.providers.state import PROVIDERS
from hyadex.providers.subagents import (
    build_claude_subagent_snapshot,
    claude_stop_fence_attempt_lock,
    CLAUDE_STOP_FENCE_ATTEMPT_TASKS,
    discard_unused_claude_stop_fence_attempt_lock,
    empty_subagent_stop_result,
    evict_claude_sdk_chat,
)

logger = logging.getLogger("hyadex-server")


async def stop_idle_claude_background_subagents(
    session_id: str,
    *,
    emit_event: bool,
) -> dict[str, Any]:
    """Retire one idle Claude supervisor when its children remain active.

    A chat-scoped SDK process may legitimately stay connected after its parent
    ResultMessage. Explicit Stop is stronger: if the durable snapshot still
    has live background children, retire only this chat's supervisor and add
    a durable fence so those historical child states cannot block a later
    worker retirement or restart.
    """

    result = empty_subagent_stop_result()
    session = STORE.sessions.get(session_id) or {}
    if str(session.get("backend") or DEFAULT_BACKEND) != BACKEND_CLAUDE:
        return result
    manager = PROVIDERS.claude_sdk_manager
    loaded = False
    if manager is not None:
        try:
            loaded = manager.is_loaded(session_id)
        except Exception as exc:
            # Do not claim success or hide historical work when ownership of
            # a potentially live process cannot be determined.
            result["pending"] = ["claude-background-work"]
            result["errors"].append(concise_error_message(exc))
            result["fence_committed"] = False
            return result
    try:
        snapshot = await asyncio.to_thread(
            build_claude_subagent_snapshot,
            session_id,
            SUBAGENT_SNAPSHOT_STATE_LIMIT,
        )
        active = [
            item
            for item in snapshot.get("subagents") or []
            if isinstance(item, dict)
            and str(item.get("subagent_status") or "") in {"starting", "running"}
        ]
        active_count = max(0, int(snapshot.get("active_count") or 0))
    except Exception as exc:
        # An explicit Stop must fail closed if a live chat process has
        # unreadable child state. Evict the chat and fence all historical
        # active children instead of leaving unknown work behind.
        active = []
        active_count = 1
        result["errors"].append(concise_error_message(exc))
    if not active_count:
        return result

    result["fence_committed"] = False

    subagent_ids = sorted({
        str(item.get("subagent_id") or "").strip()
        for item in active
        if str(item.get("subagent_id") or "").strip()
    })
    run_ids = sorted({
        str(item.get("run_id") or "").strip()
        for item in active
        if str(item.get("run_id") or "").strip()
    })
    result["descendants"] = active_count
    result["requested"] = subagent_ids
    if loaded and not await evict_claude_sdk_chat(
        session_id,
        force=True,
        manager=manager,
    ):
        result["pending"] = subagent_ids or ["claude-background-work"]
        result["errors"].append("Claude SDK chat eviction did not complete")
        return result

    try:
        # This is a recovery fence, not merely a visible timeline row. Commit
        # it while managed-update admission is still excluded even when the
        # caller suppresses ordinary Stop UI events.
        await append_durable_event(session_id, "claude_subagents_stopped", {
            "backend": BACKEND_CLAUDE,
            "run_ids": run_ids,
            "subagent_ids": subagent_ids,
            "all": True,
            "message": "Stopped background Claude agents with the parent chat.",
        })
        result["fence_committed"] = True
    except Exception as exc:
        # The process is already gone, so no child can continue. Keep update
        # admission fenced until a later Stop durably records that fact.
        result["pending"] = ["claude-stop-fence"]
        result["errors"].append(concise_error_message(exc))
    result["interrupted"] = subagent_ids
    return result


async def stop_idle_claude_background_subagents_bounded(
    session_id: str,
    *,
    emit_event: bool,
) -> dict[str, Any]:
    """Run one serialized fence attempt without trusting cancellation."""

    async def attempt() -> dict[str, Any]:
        async with claude_stop_fence_attempt_lock(session_id):
            return await stop_idle_claude_background_subagents(
                session_id,
                emit_event=emit_event,
            )

    task = asyncio.create_task(
        attempt(),
        name=f"claude-stop-fence-attempt:{session_id}",
    )
    register_session_task(
        CLAUDE_STOP_FENCE_ATTEMPT_TASKS,
        session_id,
        task,
    )
    def attempt_finished(completed: asyncio.Task[Any]) -> None:
        if not completed.cancelled():
            completed.exception()
        discard_unused_claude_stop_fence_attempt_lock(session_id)

    task.add_done_callback(attempt_finished)
    try:
        done, _pending = await asyncio.wait(
            {task},
            timeout=CLAUDE_STOP_FENCE_ATTEMPT_TIMEOUT_SECONDS,
        )
    except BaseException:
        task.cancel()
        raise
    if done:
        return task.result()

    task.cancel()
    logger.error(
        "Claude stop fence attempt timed out after %.0fs session=%s",
        CLAUDE_STOP_FENCE_ATTEMPT_TIMEOUT_SECONDS,
        session_id,
    )
    return {
        **empty_subagent_stop_result(),
        "pending": ["claude-stop-fence"],
        "errors": ["Claude stop fence persistence timed out"],
        "fence_committed": False,
    }
