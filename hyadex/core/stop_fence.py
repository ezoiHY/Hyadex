"""Retrying Claude stop fences."""
from __future__ import annotations

import asyncio
import logging.handlers
from typing import Any

from hyadex.core.config import BACKEND_CLAUDE, DEFAULT_BACKEND
from hyadex.core.turn_start import schedule_next_queued_turn
from hyadex.core.runs import RUNS
from hyadex.core.server import SERVER
from hyadex.core.server_logging import concise_error_message
from hyadex.core.sessions import CLAUDE_STOP_FENCE_SESSIONS, STORE
from hyadex.providers.claude.background import stop_idle_claude_background_subagents_bounded
from hyadex.providers.claude.runtime import (
    CLAUDE_STOP_FENCE_RETRY_DELAYS_SECONDS,
    CLAUDE_STOP_FENCE_RETRY_TASKS,
    stop_cleanup_in_progress,
)
from hyadex.providers.claude.subagents import discard_unused_claude_stop_fence_attempt_lock

logger = logging.getLogger("hyadex-server")


async def retry_claude_stop_fence(session_id: str) -> None:
    """Retry an idle Claude child-stop marker until it is durable or obsolete."""

    attempt = 0
    while True:
        delay = CLAUDE_STOP_FENCE_RETRY_DELAYS_SECONDS[
            min(attempt, len(CLAUDE_STOP_FENCE_RETRY_DELAYS_SECONDS) - 1)
        ]
        attempt += 1
        await asyncio.sleep(max(0.0, float(delay)))

        session = STORE.sessions.get(session_id)
        async with RUNS.active_lock:
            if session_id not in CLAUDE_STOP_FENCE_SESSIONS:
                return
            if (
                not isinstance(session, dict)
                or str(session.get("backend") or DEFAULT_BACKEND)
                != BACKEND_CLAUDE
            ):
                CLAUDE_STOP_FENCE_SESSIONS.discard(session_id)
                RUNS.maintenance.discard(session_id)
                discard_unused_claude_stop_fence_attempt_lock(session_id)
                return
            if (
                session_id in RUNS.active
                or session_id in RUNS.busy
                or stop_cleanup_in_progress(session_id)
            ):
                continue
            # Reassert the fail-closed admission lease in case an unrelated
            # late cleanup discarded the coarse maintenance membership.
            RUNS.maintenance.add(session_id)

        try:
            result = await stop_idle_claude_background_subagents_bounded(
                session_id,
                emit_event=False,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "Claude stop fence retry raised session=%s attempt=%s error=%s",
                session_id,
                attempt,
                concise_error_message(exc),
            )
            continue
        if not bool(result.get("fence_committed", False)):
            logger.warning(
                "Claude stop fence retry deferred session=%s attempt=%s errors=%s",
                session_id,
                attempt,
                result.get("errors") or [],
            )
            continue

        released = False
        async with RUNS.active_lock:
            if (
                session_id in CLAUDE_STOP_FENCE_SESSIONS
                and session_id not in RUNS.active
                and session_id not in RUNS.busy
                and not stop_cleanup_in_progress(session_id)
            ):
                CLAUDE_STOP_FENCE_SESSIONS.discard(session_id)
                RUNS.maintenance.discard(session_id)
                released = True
        if released:
            discard_unused_claude_stop_fence_attempt_lock(session_id)
            logger.info(
                "Claude stop fence recovered session=%s attempts=%s",
                session_id,
                attempt,
            )
            schedule_next_queued_turn(session_id)
            return


def schedule_claude_stop_fence_retry(session_id: str) -> None:
    """Start the single retry owner for one failed Claude stop fence."""

    if SERVER.shutting_down:
        return
    current = CLAUDE_STOP_FENCE_RETRY_TASKS.get(session_id)
    if current is not None and not current.done():
        return
    task = asyncio.create_task(
        retry_claude_stop_fence(session_id),
        name=f"claude-stop-fence-retry:{session_id}",
    )
    CLAUDE_STOP_FENCE_RETRY_TASKS[session_id] = task

    def retry_finished(completed: asyncio.Task[Any]) -> None:
        if CLAUDE_STOP_FENCE_RETRY_TASKS.get(session_id) is completed:
            CLAUDE_STOP_FENCE_RETRY_TASKS.pop(session_id, None)
        if completed.cancelled():
            return
        error = completed.exception()
        if error is not None:
            logger.error(
                "Claude stop fence retry failed session=%s",
                session_id,
                exc_info=(type(error), error, error.__traceback__),
            )

    task.add_done_callback(retry_finished)
