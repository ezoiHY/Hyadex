"""Resume a queue that an explicit Stop held.

Stop keeps queued messages but holds each one (``_paused_after_stop``) so
neither the scheduler nor a restart runs them unasked; held rows keep their
place without blocking the rows behind them. Force Send releases one item;
this releases every Stop hold at once so they run in queue order. Items
fenced for uncertain native delivery stay held: they may already have reached
the provider.
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import HTTPException

from hyadex.core.history_sync import append_durable_event
from hyadex.core.lifecycle_admission import server_lifecycle_admission_blocker
from hyadex.core.queue_admission import wait_for_queue_recovery_admission
from hyadex.core.runs import RUNS
from hyadex.core.server_logging import concise_error_message
from hyadex.core.session_settings import retired_session_runtime_message
from hyadex.core.sessions import (
    ensure_session_not_deleting,
    session_lifecycle_lock,
    STORE,
)
from hyadex.core.turn_start import reconcile_idle_queue_session
from hyadex.providers.claude.runtime import stop_cleanup_in_progress

logger = logging.getLogger("hyadex-server")

QUEUE_RESUMED_EVENT = "turn_queue_resumed"
QUEUE_RESUMED_MESSAGE = "Queued messages held after Stop were resumed."


def stop_cleanup_conflict() -> HTTPException:
    return HTTPException(
        status_code=409,
        detail="Stop is still finishing for this chat; retry resuming the queue shortly",
    )


async def resume_queued_turns_after_stop(session_id: str) -> dict[str, Any]:
    """Durably release every Stop hold, then let an idle chat continue.

    Returns the released ids, the ids still held for uncertain delivery and
    the visible queue length. Resuming a queue with nothing held is a no-op.
    """

    await wait_for_queue_recovery_admission(session_id)
    ensure_session_not_deleting(session_id)
    # Lock order matches Stop, Force Send and deletion: lifecycle, then the
    # Force Send request lock, then the queue lock. Holding the lifecycle lock
    # keeps a new Stop from being admitted between these checks and the
    # release below.
    async with session_lifecycle_lock(session_id):
        ensure_session_not_deleting(session_id)
        session = STORE.sessions.get(session_id)
        if not session:
            raise HTTPException(status_code=404, detail="session not found")
        if retired := retired_session_runtime_message(session):
            # The scheduler would permanently discard rows it cannot start.
            raise HTTPException(status_code=410, detail=retired)
        if session.get("archived"):
            raise HTTPException(
                status_code=409,
                detail="archived chats cannot resume queued messages",
            )
        if stop_cleanup_in_progress(session_id):
            # Stop writes its hold during cleanup; releasing earlier would be
            # undone by that write, or would release a newer Stop's hold.
            raise stop_cleanup_conflict()

        async with RUNS.run_now_request_lock:
            ensure_session_not_deleting(session_id)
            request = RUNS.run_now_requests.get(session_id)
            if request is not None and not request[1].done():
                # A failed Force Send restores its item's earlier hold, so a
                # release now could be silently undone.
                raise HTTPException(
                    status_code=409,
                    detail="Force Send is still being applied; retry resuming the queue shortly",
                )
            async with RUNS.queue_lock:
                lifecycle_blocker = server_lifecycle_admission_blocker()
                if lifecycle_blocker:
                    raise HTTPException(status_code=503, detail=lifecycle_blocker)
                if stop_cleanup_in_progress(session_id):
                    raise stop_cleanup_conflict()
                items = list(RUNS.queued.get(session_id) or ())
                promoted = RUNS.run_now.get(session_id)
                if promoted is not None:
                    items.insert(0, promoted)
                released: list[dict[str, Any]] = []
                held: list[str] = []
                for item in items:
                    queued_id = str(item.get("queued_id") or "").strip()
                    if not queued_id:
                        continue
                    if item.get("_native_delivery_fenced") is True:
                        held.append(queued_id)
                    elif item.get("_paused_after_stop") is True:
                        released.append(item)
                resumed = [str(item["queued_id"]).strip() for item in released]
                if resumed:
                    # Persist before releasing, so a restart cannot hold rows
                    # this server already ran, and fail closed if it cannot.
                    try:
                        await append_durable_event(session_id, QUEUE_RESUMED_EVENT, {
                            "queued_ids": resumed,
                            "message": QUEUE_RESUMED_MESSAGE,
                        })
                    except Exception as exc:
                        logger.error(
                            "could not persist queue resume session=%s error=%s",
                            session_id,
                            concise_error_message(exc),
                        )
                        raise HTTPException(
                            status_code=503,
                            detail=(
                                "The queue could not be resumed because the change "
                                "could not be saved; queued messages remain held"
                            ),
                        ) from exc
                    for item in released:
                        item["_paused_after_stop"] = False
                        if item.get("_durable") is False:
                            # Only a Stop whose hold write failed leaves a
                            # visible row non-durable (enqueue persists under
                            # this lock). With the resume persisted, recovery
                            # rebuilds the same unheld row.
                            item["_durable"] = True
                remaining = len(RUNS.queued.get(session_id) or ()) + (
                    1 if RUNS.run_now.get(session_id) is not None else 0
                )

    if resumed:
        logger.info(
            "queue resumed after stop session=%s resumed=%d held=%d",
            session_id,
            len(resumed),
            len(held),
        )
    # Start the first runnable row only when the chat is idle and nothing else
    # owns promotion; a running turn schedules its successor when it finishes.
    await reconcile_idle_queue_session(
        session_id,
        schedule=True,
        reason="queue_resume",
    )
    return {
        "ok": True,
        "resumed": resumed,
        "held": held,
        "remaining": remaining,
    }
