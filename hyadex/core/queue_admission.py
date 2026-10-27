"""Queue admission fences: promotion fences and waiting for startup queue recovery."""
from __future__ import annotations

import asyncio
import logging.handlers
import time
from typing import Any

from hyadex.core.config import QUEUE_FENCE_LOG_INTERVAL_SECONDS
from hyadex.core.restart_status import MANAGED_SERVER_RESTART_ACTIVE_DETAIL
from hyadex.core.runs import RUNS
from hyadex.core.server import SERVER
from hyadex.core.session_titles import server_lifecycle_admission_blocker
from hyadex.core.state import (
    QUEUE_FENCE_LOGGED_AT,
    QUEUE_RECOVERY_ADMISSION_WAIT_SECONDS,
    QUEUE_RECOVERY_READY,
)
from hyadex.core.util import TransientAdmissionWait
from hyadex.providers.claude.runtime import stop_cleanup_in_progress

logger = logging.getLogger("hyadex-server")


def queue_promotion_fence(session_id: str) -> str | None:
    """Name the process-wide or per-chat fence that makes promotion a no-op.

    Reconciliation used to schedule a promotion that then returned silently
    from ``_start_next_queued_turn_locked``; with two clients polling health
    that produced a per-second loop with no indication of the real blocker.
    """

    if stop_cleanup_in_progress(session_id):
        return "explicit_stop"
    blocker = server_lifecycle_admission_blocker()
    if blocker == MANAGED_SERVER_RESTART_ACTIVE_DETAIL:
        return "server_restart"
    if blocker:
        return "server_lifecycle"
    return None


def log_queue_promotion_fence(session_id: str, fence: str, reason: str) -> None:
    """Log a fenced promotion at most once per minute per chat and fence."""

    now = time.monotonic()
    key = (session_id, fence)
    last = QUEUE_FENCE_LOGGED_AT.get(key)
    if last is not None and now - last < QUEUE_FENCE_LOG_INTERVAL_SECONDS:
        return
    QUEUE_FENCE_LOGGED_AT[key] = now
    if len(QUEUE_FENCE_LOGGED_AT) > 4096:
        stale = [
            entry_key
            for entry_key, logged_at in QUEUE_FENCE_LOGGED_AT.items()
            if now - logged_at >= QUEUE_FENCE_LOG_INTERVAL_SECONDS
        ]
        for entry_key in stale:
            QUEUE_FENCE_LOGGED_AT.pop(entry_key, None)
    logger.warning(
        "queue promotion fenced session=%s fence=%s reason=%s "
        "visible_queue_count=%s",
        session_id,
        fence,
        reason,
        len(RUNS.queued.get(session_id) or ()),
    )


def queue_recovery_readiness() -> dict[str, Any]:
    recovery = SERVER.queue_recovery_task
    if recovery is None or (recovery.done() and not recovery.cancelled() and recovery.exception() is None):
        return {"ready": True, "state": "ready", "message": None}
    if not recovery.done():
        return {
            "ready": False, "state": "recovering",
            "message": "Queued messages are being restored after restart. Retry shortly; do not resend accepted messages.",
        }
    return {
        "ready": False, "state": "failed",
        "message": "Queued message recovery failed. Chat admission remains closed to preserve saved messages. Restart the managed server; if it repeats, inspect server logs.",
    }


async def wait_for_queue_recovery_admission(session_id: str | None = None) -> None:
    """Wait briefly for the one startup queue scan, then fail retryably.

    The shared recovery task is shielded because disconnecting one request
    must never cancel the only owner of the durable queue rebuild. A failed
    scan remains authoritative until process restart rather than opening an
    ordering hole with only a partial in-memory queue.
    """

    if session_id is not None and session_id in QUEUE_RECOVERY_READY:
        return
    recovery = SERVER.queue_recovery_task
    if recovery is None or recovery is asyncio.current_task():
        return
    # asyncio.wait does not propagate the task's own TimeoutError and never
    # cancels the shared recovery when this request times out/disconnects.
    done, _ = await asyncio.wait(
        {recovery}, timeout=max(0.0, QUEUE_RECOVERY_ADMISSION_WAIT_SECONDS),
    )
    if not done:
        raise TransientAdmissionWait(
            status_code=503,
            detail="queued turn recovery is still in progress; retry this request shortly",
        )
    try:
        recovery.result()
    except asyncio.CancelledError:
        if SERVER.shutting_down:
            raise
        raise TransientAdmissionWait(
            status_code=503,
            detail="queued turn recovery did not complete; restart the managed server; if it repeats, inspect server logs",
        ) from None
    except Exception as exc:
        raise TransientAdmissionWait(
            status_code=503,
            detail="queued turn recovery did not complete; restart the managed server; if it repeats, inspect server logs",
        ) from exc
