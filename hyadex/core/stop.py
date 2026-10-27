"""Explicit Stop: the operation that stops a chat and settles its queue, and its admission."""
from __future__ import annotations

import asyncio
from collections import deque
from contextlib import suppress
import json
import logging.handlers
from pathlib import Path
import time
from typing import Any
import uuid

from fastapi import HTTPException

from hyadex.core.config import (
    BACKEND_CLAUDE,
    BACKEND_CODEX,
    CLAUDE_TRANSPORT_AGENT_SDK,
    CODEX_TRANSPORT_APP_SERVER,
    DEFAULT_BACKEND,
    EXPLICIT_STOP_FENCE_RELEASE_TIMEOUT_SECONDS,
    EXPLICIT_STOP_OPERATION_TIMEOUT_SECONDS,
    HARD_TERMINALIZABLE_TURN_TRANSPORTS,
    RETIRED_TURN_PURPOSES,
    STOP_CONFIRM_TIMEOUT_SECONDS,
)
from hyadex.core.history_sync import (
    append_durable_event,
    fence_native_steer_delivery,
    requeue_native_steer_after_safe_rejection,
)
from hyadex.core.paths import events_path
from hyadex.core.processes import terminate_process_tree
from hyadex.core.queue import (
    codex_goal_followup_requires_native,
    NonNativeForceSendRequiresLifecycleLock,
    pause_queued_turns_after_explicit_stop,
)
from hyadex.core.queue_admission import wait_for_queue_recovery_admission
from hyadex.core.stop_turn import stop_turn
from hyadex.core.runs import RUNS
from hyadex.core.server_logging import concise_error_message
from hyadex.core.session_codex import existing_codex_app_server_manager
from hyadex.core.lifecycle_admission import server_lifecycle_admission_blocker
from hyadex.core.sessions import (
    append_event,
    CLAUDE_STOP_FENCE_SESSIONS,
    ensure_session_not_deleting,
    register_session_task,
    revoke_provider_authority,
    session_lifecycle_lock,
    STORE,
)
from hyadex.core.state import (
    RUN_NOW_COMPLETED_MAX,
    RUN_NOW_COMPLETED_RESULTS,
    RUN_NOW_COMPLETED_TTL_SECONDS,
)
from hyadex.core.steering import (
    await_native_steer_result,
    codex_goal_steer_selection_is_plain,
    deferred_force_send_outcome,
    force_send_conflict_detail,
    prepare_steered_turn,
    withdraw_unaccepted_native_steer,
)
from hyadex.core.stop_fence import schedule_claude_stop_fence_retry
from hyadex.core.turn_start import (
    reconcile_idle_queue_session,
    release_turn_slot,
    schedule_next_queued_turn,
    schedule_steered_turn_slot_waiter,
)
from hyadex.core.util import bounded_lock
from hyadex.features.usage import join_task_despite_caller_cancellation
from hyadex.providers.authority import NativeSteerHandoffError
from hyadex.providers.claude.background import stop_idle_claude_background_subagents_bounded
from hyadex.providers.claude.history import session_provider_id
from hyadex.providers.claude.interactions import cancel_claude_interactions
from hyadex.providers.claude.runtime import (
    detached_stop_in_progress,
    interrupt_claude_sdk_run_bounded,
    queued_claude_runtime_matches_active,
    stop_cleanup_in_progress,
)
from hyadex.providers.codex.goals import (
    pause_active_codex_goal_for_stop,
    quarantine_codex_goal_thread,
    settle_idle_codex_goal_for_stop,
)
from hyadex.providers.codex.interactions import cancel_codex_interactions
from hyadex.providers.codex.managers import CODEX_NATIVE_ACTION_TASKS, release_codex_control_slot
from hyadex.providers.codex.runtime import queued_codex_runtime_matches_active
from hyadex.providers.registry import normalize_steering_lineage, queued_turn_uses_retired_routing
from hyadex.providers.subagents import empty_subagent_stop_result
from hyadex.providers.codex.subagents import stop_codex_descendant_subagents
from hyadex.providers.claude.subagents import (
    discard_unused_claude_stop_fence_attempt_lock,
    evict_claude_sdk_chat,
)

logger = logging.getLogger("hyadex-server")


async def join_cancelled_tasks(*tasks: Any) -> None:
    """Await already-cancelled tasks without letting any of them raise."""

    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


async def run_explicit_stop_operation(
    session_id: str,
    admission_ready: asyncio.Event,
) -> dict[str, Any]:
    """Finish one explicit Stop after its short serialized admission edge.

    The operation is bounded by ``EXPLICIT_STOP_OPERATION_TIMEOUT_SECONDS``.
    A Stop whose provider interrupt or subagent teardown never settles used to
    leave its HTTP operation pending forever. On expiry that outer operation
    returns a visible timeout, while the exact inner teardown stays keyed as an
    admission/delete/restart fence until its session-wide effects are finished.
    """

    stop_task = asyncio.create_task(
        stop_turn(
            session_id,
            schedule_queue=False,
            _admission_ready=admission_ready,
        )
    )
    RUNS.detached_stop_tasks.add(stop_task)
    stop_task.add_done_callback(RUNS.detached_stop_tasks.discard)
    register_session_task(RUNS.detached_stop_tasks_by_session, session_id, stop_task)
    outer_fence_released = False
    queue_wake_scheduled = False

    def wake_queue_once() -> None:
        nonlocal queue_wake_scheduled

        if queue_wake_scheduled:
            return
        queue_wake_scheduled = True
        schedule_next_queued_turn(session_id)

    def detached_stop_finished(completed: asyncio.Task[Any]) -> None:
        if not completed.cancelled():
            with suppress(BaseException):
                completed.exception()
        # Normal completion is woken exactly once by the outer finalizer. A
        # child that outlives that outer fence owns the one wake after its
        # keyed session-wide effects have finished.
        if outer_fence_released:
            wake_queue_once()

    stop_task.add_done_callback(detached_stop_finished)
    try:
        done, _pending = await asyncio.wait(
            {stop_task},
            timeout=max(1.0, float(EXPLICIT_STOP_OPERATION_TIMEOUT_SECONDS)),
        )
        if done:
            return stop_task.result()
        logger.error(
            "explicit Stop did not finish within %.0fs session=%s; returning "
            "a timeout while teardown retains the admission fence",
            EXPLICIT_STOP_OPERATION_TIMEOUT_SECONDS,
            session_id,
        )
        with suppress(Exception):
            await append_event(session_id, "error", {
                "stop_timeout": True,
                "message": (
                    "Stop did not finish within "
                    f"{int(EXPLICIT_STOP_OPERATION_TIMEOUT_SECONDS)} seconds. "
                    "New messages remain queued and server restart waits "
                    "until the agent process finishes shutting down. Use "
                    "Force Restart if cleanup does not finish."
                ),
            })
        return {
            "ok": False,
            "stopped": False,
            "pending": True,
            "timed_out": True,
            "message": (
                "Stop is taking longer than expected; new turns remain "
                "queued while the agent process finishes shutting down. Use "
                "Force Restart if cleanup does not finish."
            ),
        }
    finally:
        # An admission exception/cancellation must also wake the endpoint;
        # otherwise it would retain the lifecycle lock while this finalizer
        # waits to acquire that same lock.
        admission_ready.set()
        # Clearing the narrow fence is itself serialized with turn admission.
        # A queued successor can therefore either observe this operation or
        # own the next turn slot, never slip between cleanup and fence release.
        # The lock wait is bounded: a wedged lifecycle lock must not keep the
        # fence alive after the operation itself has ended.
        async with bounded_lock(
            session_lifecycle_lock(session_id),
            EXPLICIT_STOP_FENCE_RELEASE_TIMEOUT_SECONDS,
        ) as lock_held:
            if not lock_held:
                logger.error(
                    "explicit Stop fence release could not take the lifecycle "
                    "lock session=%s; releasing the fence anyway",
                    session_id,
                )
            current_task = asyncio.current_task()
            if RUNS.explicit_stops.get(session_id) is current_task:
                RUNS.explicit_stops.pop(session_id, None)
        outer_fence_released = True
        # A prompt child completion is now safe to wake after the outer fence
        # is gone. A still-live child owns that one wake through its callback.
        if stop_task.done():
            wake_queue_once()


async def request_explicit_stop(session_id: str) -> dict[str, Any]:
    # Serialize only Stop admission with turn admission. Provider interrupts,
    # subagent teardown, and bounded terminal confirmation continue under the
    # narrower EXPLICIT_STOP_OPERATIONS fence so unrelated route reads and
    # queue writes do not wait behind slow cleanup.
    async with session_lifecycle_lock(session_id):
        lifecycle_blocker = server_lifecycle_admission_blocker()
        if lifecycle_blocker:
            raise HTTPException(status_code=503, detail=lifecycle_blocker)
        operation = RUNS.explicit_stops.get(session_id)
        if operation is None or operation.done():
            if detached_stop_in_progress(session_id):
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "Explicit Stop cleanup is still finishing; retry "
                        "shortly."
                    ),
                )
            admission_ready = asyncio.Event()
            operation = asyncio.create_task(
                run_explicit_stop_operation(session_id, admission_ready)
            )
            RUNS.explicit_stops[session_id] = operation
            # stop_turn sets this immediately after it has fenced the observed
            # owner under ACTIVE_LOCK. The explicit-operation entry itself
            # prevents queued promotion while the durable queue pause and all
            # provider cleanup continue outside this broad lock. It is also
            # set on admission failure so this lock can never be stranded.
            await admission_ready.wait()

    # Every waiter is observational. A renderer timeout or HTTP disconnect
    # must not cancel teardown after Stop has fenced the provider owner.
    return await asyncio.shield(operation)


