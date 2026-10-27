"""Starting turns: admission into a chat's provider slot, starting the next queued turn and supervising the provider task."""
from __future__ import annotations

import asyncio
from collections import deque
from contextlib import suppress
import errno
import logging.handlers
import time
from typing import Any, Literal
import uuid

from fastapi import HTTPException

from hyadex.core.config import (
    BACKEND_CODEX,
    CLAUDE_SDK_INTERACTIVE_CLIENT_CAPABILITY,
    CODEX_INTERACTIVE_CLIENT_CAPABILITY,
    DEFAULT_BACKEND,
    HEALTH_QUEUE_RECONCILE_INTERVAL_SECONDS,
    JOB_BUSY_RETRY_SECONDS,
    RETIRED_TURN_PURPOSES,
)
from hyadex.core.history_sync import append_durable_event
from hyadex.core.paths import codex_manifest_path, now_iso
from hyadex.core.queue import (
    queued_turn_unqueued_payload,
    request_fields_set,
    requeue_turn_front,
    should_schedule_queue_after_finish,
    terminally_discard_queued_turn,
)
from hyadex.core.queue_admission import (
    log_queue_promotion_fence,
    queue_promotion_fence,
    wait_for_queue_recovery_admission,
)
from hyadex.core.queue_recovery import (
    restore_missing_durable_force_send_row,
    scan_queued_turns_from_events,
)
from hyadex.core.runs import RUNS
from hyadex.core.server import SERVER
from hyadex.core.server_logging import concise_error_message
from hyadex.core.session_settings import (
    codex_runtime_settings,
    effective_provider_jobs_access,
    preview_session_runtime_update,
    retired_session_runtime_message,
)
from hyadex.core.lifecycle_admission import server_lifecycle_admission_blocker
from hyadex.core.sessions import (
    append_event,
    CLAUDE_STOP_FENCE_SESSIONS,
    ensure_session_not_deleting,
    public_session,
    register_session_task,
    revoke_provider_authority,
    session_lifecycle_lock,
    STORE,
)
from hyadex.core.state import (
    DELETED_SESSION_TOMBSTONES,
    DELETING_SESSIONS,
    HEALTH_QUEUE_RECONCILE_STATE,
    QUEUE_RETRY_TASKS,
    RUN_NOW_IDLE_OWNER_CLEANUP_TIMEOUT_SECONDS,
    RUN_NOW_IDLE_OWNER_TIMEOUT_SECONDS,
    RUN_NOW_PROVIDER_HANDOFF_TIMEOUT_SECONDS,
    SESSION_SNAPSHOT_RECONCILE_AT,
)
from hyadex.core.steering import force_send_task_owner
from hyadex.core.util import TransientAdmissionWait
from hyadex.core.wiring import WIRING
from hyadex.features.files import (
    discard_owned_git_baselines,
    run_event_metadata,
    validate_session_file_ids,
)
from hyadex.features.side_questions import CODEX_GOALS_RECONFIGURING
from hyadex.features.terminal import scrub_tmux_global_secret_environment
from hyadex.features.usage import join_task_despite_caller_cancellation
from hyadex.jobs.context import VALID_JOB_CONTEXT_MODES
from hyadex.providers.authority import issue_provider_authority, provider_authority_runtime_env
from hyadex.providers.claude.runtime import stop_cleanup_in_progress, TurnRequest
from hyadex.providers.codex.commands import (
    prepare_codex_login_turn,
    resolve_provider_command_selection,
)
from hyadex.providers.codex.runtime import migrate_retired_codex_selection, turn_start_blocker
from hyadex.providers.registry import (
    build_turn_provider_prompt,
    ensure_runtime_available,
    normalize_steering_lineage,
    provider_context_goal_is_exhausted,
    provider_turn_may_raise_emergency,
    ProviderCommandSelectionInvalid,
    ProviderCommandSelectionUnavailable,
    queued_turn_uses_retired_routing,
    standalone_provider_session,
)
from hyadex.providers.runtime import assert_provider_user_message_unchanged, ProviderTurnPayload

logger = logging.getLogger("hyadex-server")


async def enqueue_turn(
    session_id: str,
    req: TurnRequest,
    sess: dict[str, Any],
) -> dict[str, Any]:
    await wait_for_queue_recovery_admission(session_id)
    req.file_ids = validate_session_file_ids(session_id, req.file_ids)
    queued_id = f"queued_{uuid.uuid4().hex[:16]}"
    item: dict[str, Any]
    display_prompt = req.display_prompt if req.display_prompt is not None else req.prompt
    async with RUNS.queue_lock:
        # Recheck lifecycle admission under the durable queue write lock.
        lifecycle_blocker = server_lifecycle_admission_blocker()
        if lifecycle_blocker:
            raise HTTPException(status_code=503, detail=lifecycle_blocker)
        item = {
            "queued_id": queued_id,
            **(getattr(req, "shared_chat_metadata", None) or {}),
            "prompt": req.prompt,
            "file_ids": list(req.file_ids),
            "backend": req.backend,
            "model": req.model,
            "effort": req.effort,
            "skill_selection": (
                req.skill_selection.model_dump()
                if req.skill_selection is not None
                else None
            ),
            "display_prompt": req.display_prompt,
            "purpose": req.purpose,
            "client_capabilities": list(req.client_capabilities),
            "created_at": now_iso(),
            # Managed replacement may preserve queued turns only after their
            # durable event exists.
            "_durable": False,
        }
        queue = RUNS.queued.setdefault(session_id, deque())
        queue.append(item)
        position = len(queue)
        try:
            # Keep the queue lock through persistence. Stop, edit, remove,
            # reorder, Run Now, and managed-update admission must never
            # observe this item before its creation event exists.
            queued_event = await append_durable_event(session_id, "turn_queued", {
                "queued_id": queued_id,
                **(getattr(req, "shared_chat_metadata", None) or {}),
                "backend": req.backend or sess.get("backend") or DEFAULT_BACKEND,
                "model": req.model,
                "effort": req.effort,
                "skill_selection": (
                    req.skill_selection.model_dump()
                    if req.skill_selection is not None
                    else None
                ),
                "prompt": display_prompt,
                "request_prompt": req.prompt,
                "client_request_id": getattr(req, "client_request_id", None),
                "display_prompt": req.display_prompt,
                "file_ids": list(req.file_ids),
                "position": position,
                "purpose": req.purpose,
                "client_capabilities": list(req.client_capabilities),
            })
        except BaseException:
            # A provisional row is safe to remove. Roll back only this exact
            # object so concurrent additions keep their original order.
            queue = RUNS.queued.get(session_id)
            if queue is not None:
                kept = deque(candidate for candidate in queue if candidate is not item)
                if kept:
                    RUNS.queued[session_id] = kept
                else:
                    RUNS.queued.pop(session_id, None)
            raise
        # The fsynced turn_queued event is the authoritative ownership
        # boundary. Publish that fact without another await so cancellation
        # can never turn a recoverable row into a disk-only "orphan".
        item["_durable"] = True
    try:
        async with RUNS.active_lock:
            still_busy = session_id in RUNS.busy
    except asyncio.CancelledError:
        # The message already exists in the durable timeline. Keep the live
        # row and let normal promotion settle it instead of waiting for an
        # unrelated wake-up.
        schedule_next_queued_turn(session_id)
        raise
    if not still_busy:
        schedule_next_queued_turn(session_id)
    return {
        "queued": True,
        "queued_id": queued_id,
        "position": position,
        "session": public_session(STORE.sessions[session_id]),
        "event": queued_event,
    }


def provider_turn_may_manage_jobs(purpose: Any) -> bool:
    """Secure-peer text could never initiate durable automation."""

    return str(purpose or "").strip() != "secure_peer_handoff_delivery"


async def reconcile_idle_queue_session(
    session_id: str,
    *,
    schedule: bool,
    reason: str,
) -> bool:
    """Repair an idle queue whose volatile promotion owner disappeared.

    Durable queue rows are authoritative.  Volatile steering/request markers
    may only suppress them while a matching live owner exists.  Older builds
    could lose the waiter task but retain ``STEERING_SESSIONS`` forever; every
    later prompt then queued and every Run Now returned 409 despite an idle
    provider.  Reconcile that invariant without touching active work or a
    Stop-held queue head.
    """

    fence = queue_promotion_fence(session_id)
    if fence is not None:
        # Promotion cannot start anything while this fence holds, so do not
        # schedule a no-op task per poll. Name the fence instead.
        log_queue_promotion_fence(session_id, fence, reason)
        return False

    recovered_guards: list[str] = []
    should_schedule = False
    cancelled_waiter: asyncio.Task[Any] | None = None
    expired_request: tuple[str, asyncio.Task[dict[str, Any]]] | None = None
    async with RUNS.run_now_request_lock:
        request_entry = RUNS.run_now_requests.get(session_id)
        if request_entry is not None and request_entry[1].done():
            RUNS.run_now_requests.pop(session_id, None)
            recovered_guards.append("completed_run_now_request")
            request_entry = None
        if request_entry is not None:
            request_queued_id, request_task = request_entry
            owner = force_send_task_owner(request_task)
            age_seconds = owner.get("age_seconds")
            async with RUNS.active_lock:
                request_session_idle = not RUNS.holds_turn(session_id)
            if (
                request_task is asyncio.current_task()
                or not request_session_idle
                or not isinstance(age_seconds, (int, float))
                or age_seconds < RUN_NOW_IDLE_OWNER_TIMEOUT_SECONDS
            ):
                return False
            setattr(
                request_task,
                "_hyadex_force_send_phase",
                "expiring_idle_owner",
            )
            expired_request = (request_queued_id, request_task)

    if expired_request is not None:
        expired_queued_id, expired_task = expired_request
        logger.error(
            "expiring stale idle Force Send owner session=%s queued_id=%s "
            "owner=%s",
            session_id,
            expired_queued_id,
            force_send_task_owner(expired_task),
        )
        expired_task.cancel()
        _done, pending = await asyncio.wait(
            {expired_task},
            timeout=RUN_NOW_IDLE_OWNER_CLEANUP_TIMEOUT_SECONDS,
        )
        if pending:
            logger.error(
                "stale idle Force Send cleanup did not settle session=%s "
                "queued_id=%s",
                session_id,
                expired_queued_id,
            )
            return False
        await asyncio.gather(expired_task, return_exceptions=True)
        async with RUNS.run_now_request_lock:
            current = RUNS.run_now_requests.get(session_id)
            if current is not None and current[1] is expired_task:
                RUNS.run_now_requests.pop(session_id, None)
        recovered_guards.append("expired_idle_run_now_request")
        if await restore_missing_durable_force_send_row(
            session_id,
            expired_queued_id,
        ):
            recovered_guards.append("restored_durable_queue_row")

    async with RUNS.run_now_request_lock:
        request_entry = RUNS.run_now_requests.get(session_id)
        if request_entry is not None:
            return bool(recovered_guards)

        async with RUNS.active_lock:
            idle = not RUNS.holds_turn(session_id)
        if not idle:
            return False

        async with RUNS.queue_lock:
            promotion_owner = RUNS.queue_start_tasks.get(session_id)
            if promotion_owner is not None and promotion_owner.done():
                RUNS.queue_start_tasks.pop(session_id, None)
                recovered_guards.append("completed_queue_start_owner")
                promotion_owner = None
            if (
                promotion_owner is not None
                and promotion_owner is not asyncio.current_task()
            ):
                return False

            waiter_entry = RUNS.steering_waits.get(session_id)
            if waiter_entry is not None:
                _waiter_queued_id, waiter_started_at, waiter_task = waiter_entry
                if waiter_task.done():
                    RUNS.steering_waits.pop(session_id, None)
                    recovered_guards.append("completed_steering_waiter")
                elif (
                    time.monotonic() - waiter_started_at
                    >= RUN_NOW_PROVIDER_HANDOFF_TIMEOUT_SECONDS
                ):
                    # An idle provider has no slot left for this waiter to
                    # observe. Cancel only the exact aged owner; its fenced
                    # cleanup cannot target a replacement generation.
                    RUNS.steering_waits.pop(session_id, None)
                    cancelled_waiter = waiter_task
                    recovered_guards.append("expired_steering_waiter")
                else:
                    return False

            if session_id in RUNS.steering:
                RUNS.steering.discard(session_id)
                recovered_guards.append("ownerless_steering_fence")

            promoted = RUNS.run_now.get(session_id)
            if promoted is not None:
                if promoted.get("_update_transitioning") is True:
                    # No request owner remains, so no coroutine can commit or
                    # roll back this provisional non-native promotion. Its
                    # original queue event is still durable and provider
                    # delivery has not started; release the provisional flag
                    # so the ordinary scheduler can finish the exact row.
                    promoted.pop("_update_transitioning", None)
                    recovered_guards.append(
                        "ownerless_transitioning_promotion"
                    )
                # A committed or safely released promotion keeps its exact
                # queued_id and resumes through the ordinary scheduler.
                should_schedule = bool(
                    promoted.get("_durable") is not False
                    and promoted.get("_update_transitioning") is not True
                    and promoted.get("_paused_after_stop") is not True
                )
            else:
                head = next(iter(RUNS.queued.get(session_id) or ()), None)
                should_schedule = bool(
                    head
                    and head.get("_durable") is not False
                    and head.get("_paused_after_stop") is not True
                )

    if cancelled_waiter is not None:
        cancelled_waiter.cancel()
    if recovered_guards or should_schedule:
        # Repairing a stale guard is noteworthy; merely scheduling the head of
        # an idle queue is routine and was logging ~25 lines a minute.
        logger.log(
            logging.WARNING if recovered_guards else logging.DEBUG,
            "idle queue reconciled session=%s reason=%s recovered_guards=%s "
            "scheduled=%s promoted_queued_id=%s visible_queue_count=%s",
            session_id,
            reason,
            ",".join(recovered_guards) or "none",
            bool(schedule and should_schedule),
            str((RUNS.run_now.get(session_id) or {}).get("queued_id") or "")
            or None,
            len(RUNS.queued.get(session_id) or ()),
        )
    if schedule and should_schedule:
        schedule_next_queued_turn(session_id)
    return bool(recovered_guards or should_schedule)


async def reconcile_idle_queued_turns(*, reason: str) -> int:
    """Reconcile every chat with volatile or durable queued state."""

    session_ids = {
        *RUNS.queued.keys(),
        *RUNS.run_now.keys(),
        *RUNS.steering,
        *RUNS.steering_waits.keys(),
        *RUNS.run_now_requests.keys(),
    }
    repaired = 0
    for session_id in sorted(str(value) for value in session_ids):
        repaired += int(
            await reconcile_idle_queue_session(
                session_id,
                schedule=True,
                reason=reason,
            )
        )
    return repaired


async def retry_next_queued_turn_later(session_id: str, delay_seconds: int | None = None) -> None:
    await asyncio.sleep(max(int(delay_seconds or JOB_BUSY_RETRY_SECONDS), 5))
    await start_next_queued_turn(session_id)


def schedule_queued_turn_retry(
    session_id: str,
    delay_seconds: int | None = None,
) -> bool:
    """Arm at most one delayed promotion retry per chat.

    Every deferred admission used to spawn its own untracked retry task, and
    every health poll scheduled another promotion that deferred again, so the
    number of concurrent retry timers grew by one per poll (2026-09-04: ~20
    admission attempts per second per chat after 27 minutes). Returns whether a
    new retry was armed.
    """

    existing = QUEUE_RETRY_TASKS.get(session_id)
    if (
        existing is not None
        and not existing.done()
        and existing is not asyncio.current_task()
    ):
        # Another timer is already pending. The running timer itself may
        # re-arm (its promotion attempt deferred again), otherwise retries
        # would be single-shot.
        return False
    task = asyncio.create_task(
        retry_next_queued_turn_later(session_id, delay_seconds)
    )
    QUEUE_RETRY_TASKS[session_id] = task

    def release_retry_owner(completed: asyncio.Task[Any]) -> None:
        if QUEUE_RETRY_TASKS.get(session_id) is completed:
            QUEUE_RETRY_TASKS.pop(session_id, None)

    task.add_done_callback(release_retry_owner)
    return True


async def wait_for_steered_turn_slot(session_id: str) -> None:
    waiter_task = asyncio.current_task()

    async def release_waiter_fence() -> None:
        owns_fence = False
        async with RUNS.queue_lock:
            owner = RUNS.steering_waits.get(session_id)
            owns_fence = bool(
                waiter_task is not None
                and owner is not None
                and owner[2] is waiter_task
            )
            if owns_fence:
                RUNS.steering.discard(session_id)
        if not owns_fence:
            return
        async with RUNS.active_lock:
            idle = session_id not in RUNS.busy
        if idle:
            await start_next_queued_turn(session_id)

    try:
        while True:
            async with RUNS.active_lock:
                busy = session_id in RUNS.busy
            if not busy:
                break
            await asyncio.sleep(0.05)
    finally:
        # Cancellation of the anonymous waiter was the direct source of the
        # Sonic wedge. Cleanup must survive repeated cancellation and must
        # compare the exact task before touching a replacement generation.
        cleanup = asyncio.create_task(release_waiter_fence())
        try:
            await asyncio.shield(cleanup)
        except BaseException:
            with suppress(BaseException):
                await join_task_despite_caller_cancellation(cleanup)


def schedule_steered_turn_slot_waiter(
    session_id: str,
    queued_id: str,
) -> asyncio.Task[Any]:
    """Create and identify the one owner of a non-native steering fence."""

    task = asyncio.create_task(wait_for_steered_turn_slot(session_id))
    operation_id = str(
        getattr(
            asyncio.current_task(),
            "_hyadex_force_send_operation_id",
            "",
        )
        or f"steering_wait_{uuid.uuid4().hex[:16]}"
    )
    started_at = time.monotonic()
    setattr(task, "_hyadex_force_send_operation_id", operation_id)
    setattr(task, "_hyadex_force_send_queued_id", queued_id)
    setattr(task, "_hyadex_force_send_started_at", started_at)
    setattr(
        task,
        "_hyadex_force_send_phase",
        "waiting_for_provider_slot",
    )
    previous = RUNS.steering_waits.get(session_id)
    if previous is not None and not previous[2].done():
        # Admission should make this impossible. Preserve the newer exact
        # owner and retire the older waiter rather than leaving two callbacks
        # able to clear the same session-wide flag.
        previous[2].cancel()
    RUNS.steering_waits[session_id] = (queued_id, started_at, task)

    def release_owner(completed: asyncio.Task[Any]) -> None:
        current = RUNS.steering_waits.get(session_id)
        if current is not None and current[2] is completed:
            RUNS.steering_waits.pop(session_id, None)

    task.add_done_callback(release_owner)
    return task


async def start_next_queued_turn(session_id: str) -> None:
    await wait_for_queue_recovery_admission(session_id)
    await reconcile_idle_queue_session(
        session_id,
        schedule=False,
        reason="queue_start",
    )
    promotion_task = asyncio.current_task()
    if promotion_task is not None:
        # A caller may directly await more than one promotion on the same
        # asyncio task (tests do this, and maintenance/recovery callers can as
        # well). Do not let a prior terminal-discard continuation receipt
        # schedule a second promotion after a later requeue-only attempt.
        setattr(
            promotion_task,
            "_hyadex_continue_queue_after_discard",
            False,
        )
    async with RUNS.queue_lock:
        owner = RUNS.queue_start_tasks.get(session_id)
        if owner is not None and owner is not promotion_task and not owner.done():
            return
        if promotion_task is not None:
            RUNS.queue_start_tasks[session_id] = promotion_task

            def release_queue_start_owner(completed: asyncio.Task[Any]) -> None:
                if RUNS.queue_start_tasks.get(session_id) is completed:
                    RUNS.queue_start_tasks.pop(session_id, None)

            promotion_task.add_done_callback(release_queue_start_owner)

    admission_session = STORE.sessions.get(session_id)
    admission_backend = (
        str(admission_session.get("backend") or DEFAULT_BACKEND)
        .strip()
        .lower()
        if admission_session is not None
        else None
    )
    if promotion_task is not None:
        register_session_task(
            RUNS.turn_tasks,
            session_id,
            promotion_task,
        )
    try:
        # Stop and promotion use the same lifecycle -> queue lock order. The
        # queued item therefore remains visible until promotion owns admission;
        # a Stop that wins can pause it durably before this task removes it.
        async with session_lifecycle_lock(session_id):
            await _start_next_queued_turn_locked(
                session_id,
                admission_backend=admission_backend,
            )
    finally:
        if promotion_task is not None:
            tasks = RUNS.turn_tasks.get(session_id)
            if tasks is not None:
                tasks.discard(promotion_task)
                if not tasks:
                    RUNS.turn_tasks.pop(session_id, None)
            continue_after_discard = bool(getattr(
                promotion_task,
                "_hyadex_continue_queue_after_discard",
                False,
            ))
            setattr(
                promotion_task,
                "_hyadex_continue_queue_after_discard",
                False,
            )
            if continue_after_discard:
                # The terminal ledger work is complete and the lifecycle lock
                # has been released. Relinquish this exact promotion owner now
                # so a caller that directly awaits start_next_queued_turn does
                # not make its otherwise-valid successor look concurrent.
                async with RUNS.queue_lock:
                    if RUNS.queue_start_tasks.get(session_id) is promotion_task:
                        RUNS.queue_start_tasks.pop(session_id, None)
                schedule_next_queued_turn(session_id)


async def _start_next_queued_turn_locked(
    session_id: str,
    *,
    admission_backend: str | None,
) -> None:
    # Explicit Stop releases the broad lifecycle lock after its admission
    # fence is committed.  Keep queued promotion behind the narrower Stop
    # operation until provider/subagent cleanup is complete, without blocking
    # route reads or new queue writes in the meantime.
    if stop_cleanup_in_progress(session_id):
        log_queue_promotion_fence(session_id, "explicit_stop", "queue_start")
        return
    async with RUNS.queue_lock:
        if session_id in RUNS.steering:
            return
        item = RUNS.run_now.get(session_id)
        if item is not None and (
            item.get("_durable") is False
            or item.get("_paused_after_stop") is True
        ):
            if item.get("_paused_after_stop") is True:
                # An explicit Stop that lands after promotion wins. Return the
                # user message to the visible queue, preserving its hold until
                # a new Run Now request explicitly releases it.
                RUNS.run_now.pop(session_id, None)
                queue = RUNS.queued.setdefault(session_id, deque())
                queued_id = str(item.get("queued_id") or "")
                if not any(
                    str(candidate.get("queued_id") or "") == queued_id
                    for candidate in queue
                ):
                    queue.appendleft(item)
            return
        item = RUNS.run_now.pop(session_id, None)
        if item is None:
            queue = RUNS.queued.get(session_id)
            if queue and (
                queue[0].get("_durable") is False
                or queue[0].get("_paused_after_stop") is True
            ):
                return
            item = queue.popleft() if queue else None
            if queue is not None and not queue:
                RUNS.queued.pop(session_id, None)
    if not item:
        return
    if (
        session_id in DELETING_SESSIONS
    ):
        # Deletion can still abort while provider cleanup drains. Preserve the
        # durable queue until deletion reaches its commit point.
        await requeue_turn_front(session_id, item)
        return
    if (
        session_id in DELETED_SESSION_TOMBSTONES
        or session_id not in STORE.sessions
    ):
        await terminally_discard_queued_turn(
            session_id,
            item,
            "chat no longer exists",
        )
        return

    # Queue records are durable and may have been written by an older build or
    # restored after an interrupted update. Reject retired routing intent
    # before rebuilding TurnRequest. Such a row is terminal; record the pop so
    # restart recovery cannot resurrect it and retry indefinitely.
    if queued_turn_uses_retired_routing(item):
        await terminally_discard_queued_turn(
            session_id,
            item,
            "saved routing target configuration is invalid or no longer supported",
        )
        return

    try:
        req = TurnRequest(
            prompt=str(item.get("prompt") or ""),
            shared_chat_id=item.get("shared_chat_id"),
            shared_chat_request_id=item.get("shared_chat_request_id"),
            file_ids=list(item.get("file_ids") or []),
            backend=item.get("backend"),
            model=item.get("model"),
            effort=item.get("effort"),
            skill_selection=item.get("skill_selection"),
            display_prompt=item.get("display_prompt"),
            purpose=item.get("purpose"),
            steer_interrupted_run_id=item.get("steer_interrupted_run_id"),
            client_capabilities=list(item.get("client_capabilities") or []),
        )
    except Exception as exc:
        await terminally_discard_queued_turn(
            session_id,
            item,
            "saved queued request is invalid: " + concise_error_message(exc),
        )
        return
    try:
        ensure_session_not_deleting(session_id)
        display_file_ids = item.get("display_file_ids")
        await _start_turn_locked(
            session_id,
            req,
            queue_if_busy=False,
            queued_id=str(item["queued_id"]),
            display_file_ids=list(display_file_ids) if display_file_ids is not None else None,
            steering_lineage=normalize_steering_lineage(item.get("steering_lineage")) or None,
            admission_backend=admission_backend,
        )
    except asyncio.CancelledError as cancellation:
        # Promotion owns the only in-memory copy after popping it above. The
        # durable queue/ledger commit may already exist, so cancellation must
        # settle that exact owner before propagating instead of waiting for a
        # process restart to discover a zombie row.
        async def settle_cancelled_promotion() -> None:
            await requeue_turn_front(session_id, item)

        settlement = asyncio.create_task(settle_cancelled_promotion())
        try:
            await asyncio.shield(settlement)
        except asyncio.CancelledError:
            # A caller may keep cancelling while the queue lock or durable
            # event write is blocked.  Do not let a later cancellation abort
            # settlement after this task became the only volatile owner.
            await join_task_despite_caller_cancellation(settlement)
        raise cancellation
    except HTTPException as e:
        if session_id in DELETING_SESSIONS:
            # The delete endpoint has not committed yet and can still return
            # 409 while provider cleanup drains. Preserve this durable item so
            # an aborted delete can resume normally.
            await requeue_turn_front(session_id, item)
            return
        if (
            isinstance(
                e,
                (
                    ProviderCommandSelectionInvalid,
                    ProviderCommandSelectionUnavailable,
                ),
            )
            and item.get("skill_selection")
        ):
            # A stale selector cannot become valid without user re-selection,
            # while a transient native discovery failure has no safe retry
            # identity beyond this snapshot. Remove either exact durable row
            # visibly so it cannot pin every later queued user message.
            await terminally_discard_queued_turn(
                session_id,
                item,
                "saved provider command could not be revalidated; choose it "
                "again and resend the message",
            )
            return
        terminal_session_state = (
            session_id in DELETED_SESSION_TOMBSTONES
            or session_id not in STORE.sessions
        )
        if e.status_code == 404 or terminal_session_state:
            await terminally_discard_queued_turn(
                session_id,
                item,
                str(e.detail or "chat no longer exists"),
            )
            return
        if e.status_code in (409, 503):
            already_notified = bool(item.get("_turn_deferred_notified"))
            item["_turn_deferred_notified"] = True
            item["_last_deferred_detail"] = str(e.detail or "")
            await requeue_turn_front(session_id, item)
            if not already_notified:
                await append_event(session_id, "turn_deferred", {
                    "queued_id": item.get("queued_id"),
                    "message": f"Queued turn deferred: {e.detail}",
                })
            schedule_queued_turn_retry(session_id)
            return
        logger.warning("queued turn failed session=%s queued_id=%s: %s", session_id, item.get("queued_id"), e.detail)
        await terminally_discard_queued_turn(
            session_id,
            item,
            str(e.detail or "queued turn was permanently rejected"),
        )
    except Exception as e:
        logger.warning("queued turn failed session=%s queued_id=%s: %s", session_id, item.get("queued_id"), e)
        # An unexpected provider/storage exception is ambiguous, so preserve
        # the user's durable message and retry it visibly. The permanent HTTP
        # branch above records turn_unqueued instead; neither path may leave a
        # popped row ownerless and resurrectable only after restart.
        deferred_detail = concise_error_message(e)
        already_notified = bool(item.get("_turn_deferred_notified"))
        previous_detail = item.get("_last_deferred_detail")
        item["_turn_deferred_notified"] = True
        item["_last_deferred_detail"] = deferred_detail
        await requeue_turn_front(session_id, item)
        if not already_notified or previous_detail != deferred_detail:
            with suppress(Exception):
                await append_event(session_id, "turn_deferred", {
                    "queued_id": item.get("queued_id"),
                    "message": f"Queued turn retry deferred: {deferred_detail}",
                })
        schedule_queued_turn_retry(session_id)


def schedule_next_queued_turn(session_id: str) -> None:
    if SERVER.shutting_down:
        return
    asyncio.create_task(start_next_queued_turn(session_id))


async def _recover_queued_turns_for_sessions(session_items) -> tuple[int, int]:
    started = time.monotonic()
    recovered = await asyncio.to_thread(scan_queued_turns_from_events, session_items)
    scheduled_session_ids: list[str] = []
    rebuilt = 0
    async with RUNS.queue_lock:
        for session_id, recovered_items in recovered.items():
            existing_items = list(RUNS.queued.get(session_id) or ())
            existing_ids = {
                str(item.get("queued_id") or "")
                for item in existing_items
            }
            restored: list[dict[str, Any]] = []
            for item in recovered_items:
                if str(item.get("queued_id") or "") in existing_ids:
                    continue
                if queued_turn_uses_retired_routing(item):
                    # Rows from removed cross-chat, digest and peer delivery
                    # features can never run. Record their removal so the
                    # reducer cannot restore them on every restart.
                    try:
                        await append_durable_event(
                            session_id,
                            "turn_unqueued",
                            queued_turn_unqueued_payload(
                                session_id,
                                item,
                                "Discarded a queued message that used a removed chat routing feature.",
                            ),
                        )
                    except Exception as exc:
                        logger.warning(
                            "could not record retired queue row removal session=%s queued_id=%s error=%s",
                            session_id,
                            item.get("queued_id"),
                            concise_error_message(exc),
                        )
                    continue
                restored.append(item)
            if not restored:
                continue
            RUNS.queued[session_id] = deque([*restored, *existing_items])
            rebuilt += len(restored)
            if not RUNS.queued[session_id][0].get("_paused_after_stop"):
                scheduled_session_ids.append(session_id)
    for session_id in scheduled_session_ids:
        schedule_next_queued_turn(session_id)
    scheduled = len(scheduled_session_ids)
    logger.info(
        "queued turn recovery complete rebuilt=%d scheduled_sessions=%d elapsed=%.2fs",
        rebuilt,
        scheduled,
        time.monotonic() - started,
    )
    return rebuilt, scheduled


async def release_turn_slot(
    session_id: str,
    *,
    expected_run_id: str | None = None,
    expected_admission_id: str | None = None,
) -> bool:
    """Release a chat turn reservation without clearing a newer owner.

    A forcibly cancelled app-server consumer can finish its cleanup after the
    user has already started another turn.  Callers that own a concrete run
    must provide it so that delayed cleanup from the old task cannot erase the
    replacement turn's ACTIVE/BUSY bookkeeping. An expected run also must
    still own either ACTIVE or CURRENT; an already-released run returns False
    so fallback finalizers cannot emit or schedule twice.
    """

    async with RUNS.active_lock:
        if expected_run_id is not None or expected_admission_id is not None:
            active = RUNS.active.get(session_id)
            current_turn = RUNS.current_turns.get(session_id)
            active_run_id = (
                str(active.get("run_id") or "") if isinstance(active, dict) else ""
            )
            current_run_id = (
                str(current_turn.get("run_id") or "")
                if isinstance(current_turn, dict)
                else ""
            )
            current_admission_id = (
                str(current_turn.get("_server_restart_admission_id") or "")
                if isinstance(current_turn, dict)
                else ""
            )
            if expected_run_id is not None:
                owns_slot = expected_run_id in {active_run_id, current_run_id}
                conflicting_owner = any(
                    owner_id not in {"", expected_run_id}
                    for owner_id in (active_run_id, current_run_id)
                )
            else:
                owns_slot = bool(
                    expected_admission_id
                    and current_admission_id == expected_admission_id
                )
                # A provider-bound ACTIVE record cannot belong to a
                # reservation that still lacks a concrete run identity.
                conflicting_owner = bool(active_run_id)
            if not owns_slot or conflicting_owner:
                return False
        RUNS.active.pop(session_id, None)
        RUNS.busy.discard(session_id)
        RUNS.current_turns.pop(session_id, None)
        RUNS.stop_requests.discard(session_id)
        return True


async def reconcile_provider_task_exit(
    session_id: str,
    run_id: str,
    backend: str,
) -> bool:
    """Recover an owned slot when a provider task exits before its cleanup.

    Legacy subprocess runners can be cancelled at any await, including after
    their terminal event is durable but before BUSY/CURRENT are released. The
    task supervisor calls this from a shielded finalizer. Ownership matching
    prevents delayed cleanup from touching a replacement turn.
    """

    persisted_active = (STORE.sessions.get(session_id) or {}).get("active_run")
    terminal_missing = bool(
        isinstance(persisted_active, dict)
        and str(persisted_active.get("run_id") or "") == run_id
    )
    released = await release_turn_slot(
        session_id,
        expected_run_id=run_id,
    )
    if not released:
        RUNS.run_metadata.pop(run_id, None)
        RUNS.stopped_runs.discard(run_id)
        return False
    try:
        if terminal_missing:
            try:
                await append_event(session_id, "turn_stopped", {
                    "run_id": run_id,
                    "backend": backend,
                    "stopped": True,
                    "message": (
                        "The provider task ended before terminal cleanup "
                        "completed. Its session slot was recovered."
                    ),
                    **run_event_metadata(run_id),
                })
            except BaseException as exc:
                if isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
                    raise
                logger.exception(
                    "could not persist recovered provider terminal "
                    "session=%s run=%s error=%s",
                    session_id,
                    run_id,
                    concise_error_message(exc),
                )
    finally:
        RUNS.run_metadata.pop(run_id, None)
        RUNS.stopped_runs.discard(run_id)
        if should_schedule_queue_after_finish(session_id, stopped=True):
            schedule_next_queued_turn(session_id)
    return True


async def supervise_provider_turn_task(
    session_id: str,
    run_id: str,
    backend: str,
    runner: Any,
) -> None:
    """Run one provider coroutine with cancellation-safe slot reconciliation."""

    owner_task = asyncio.current_task()
    try:
        await runner
    finally:
        async def reconcile_and_release_snapshots() -> None:
            try:
                await reconcile_provider_task_exit(session_id, run_id, backend)
            finally:
                await discard_owned_git_baselines(owner_task)

        cleanup_task = asyncio.create_task(reconcile_and_release_snapshots())
        try:
            await asyncio.shield(cleanup_task)
        except asyncio.CancelledError:
            await join_task_despite_caller_cancellation(cleanup_task)
            raise


async def _start_turn_locked(
    session_id: str,
    req: TurnRequest,
    *,
    queue_if_busy: bool = True,
    queued_id: str | None = None,
    display_file_ids: list[str] | None = None,
    steering_lineage: list[dict[str, Any]] | None = None,
    provider_context_mode: Literal["chat", "standalone"] = "chat",
    admission_backend: str | None = None,
    scheduled_job_revision: str | None = None,
    scheduled_job_manual_run: bool = False,
) -> dict[str, Any]:
    # Queue promotion enters with the lifecycle lock already held and
    # intentionally bypasses ``start_turn``. It still must not start or queue
    # ahead of an undiscovered durable startup row.
    await wait_for_queue_recovery_admission(session_id)
    if req.purpose == "secure_peer_handoff_delivery":
        raise HTTPException(status_code=410, detail="cross-server agent delivery was removed")
    if req.purpose in RETIRED_TURN_PURPOSES:
        raise HTTPException(status_code=410, detail="cross-chat delivery was removed")
    sess = STORE.sessions.get(session_id)
    if not sess:
        raise HTTPException(status_code=404, detail="session not found")
    if retired := retired_session_runtime_message(sess):
        raise HTTPException(410, retired)
    if sess.get("archived"):
        raise HTTPException(status_code=409, detail="archived chats cannot start turns")
    if scheduled_job_manual_run and (
        req.purpose != "scheduled_job" or not scheduled_job_revision
    ):
        raise HTTPException(
            status_code=400,
            detail="manual scheduled-job admission requires a dispatch revision",
        )
    if req.purpose == "scheduled_job" and scheduled_job_revision:
        # JobStore validated this revision at dispatch. Check it again at the
        # actual provider admission boundary so a delete or edit racing
        # dispatch wins over the stale occurrence.
        if not req.job_id:
            raise HTTPException(
                status_code=400,
                detail="scheduled job id and dispatch revision must be paired",
            )
        await WIRING.jobs.assert_dispatch_revision(
            req.job_id,
            session_id,
            scheduled_job_revision,
        )
    if provider_context_mode not in VALID_JOB_CONTEXT_MODES:
        raise HTTPException(
            status_code=400,
            detail=(
                "provider_context_mode must be one of "
                f"{sorted(VALID_JOB_CONTEXT_MODES)}"
            ),
        )
    current_backend = str(
        sess.get("backend") or DEFAULT_BACKEND
    ).strip().lower()
    if (
        provider_context_mode == "chat"
        and admission_backend is not None
        and current_backend != admission_backend
    ):
        raise HTTPException(
            status_code=409,
            detail=(
                "the chat backend changed while the message was waiting to be "
                "accepted; retry the message"
            ),
        )
    runtime_fields_set = request_fields_set(req)
    runtime_validation_patch: dict[str, Any] = {}
    if req.backend:
        runtime_validation_patch["backend"] = req.backend
    if "model" in runtime_fields_set and req.model is not None:
        runtime_validation_patch["model"] = req.model
    if "effort" in runtime_fields_set and req.effort is not None:
        runtime_validation_patch["effort"] = req.effort
    # Validate user-selected runtime changes before a busy chat durably accepts
    # the message into its queue. Promotion must not be the first place an
    # invalid backend/model/effort or a locked backend switch is discovered.
    runtime_preview_session = preview_session_runtime_update(
        (
            standalone_provider_session(sess)
            if provider_context_mode == "standalone"
            else sess
        ),
        runtime_validation_patch,
    )
    if str(runtime_preview_session.get("backend") or DEFAULT_BACKEND).strip().lower() == BACKEND_CODEX:
        await prepare_codex_login_turn(runtime_preview_session)
    resolved_provider_command = await resolve_provider_command_selection(
        session_id,
        runtime_preview_session,
        req.skill_selection,
        prompt=req.prompt,
        purpose=req.purpose,
        provider_context_mode=provider_context_mode,
        client_capabilities=list(req.client_capabilities),
    )
    if provider_context_goal_is_exhausted(sess, provider_context_mode):
        raise HTTPException(
            status_code=409,
            detail=(
                "This Codex goal reached its Hyadex time limit. "
                "Increase or clear the goal time limit before starting another turn."
            ),
        )
    req.file_ids = validate_session_file_ids(session_id, req.file_ids)
    if display_file_ids is not None:
        display_file_ids = validate_session_file_ids(session_id, display_file_ids)
    normalized_lineage = normalize_steering_lineage(steering_lineage)
    interactive_app_server = (
        CODEX_INTERACTIVE_CLIENT_CAPABILITY in req.client_capabilities
    )
    interactive_agent_sdk = (
        CLAUDE_SDK_INTERACTIVE_CLIENT_CAPABILITY in req.client_capabilities
    )
    for item in normalized_lineage:
        item["file_ids"] = validate_session_file_ids(
            session_id,
            list(item.get("file_ids") or []),
        )
    reserved = False
    should_queue = False
    has_prior_queue = False
    reservation_admission_id = uuid.uuid4().hex
    if queue_if_busy:
        async with RUNS.queue_lock:
            promotion_owner = RUNS.queue_start_tasks.get(session_id)
            has_prior_queue = bool(
                RUNS.has_waiting_work(session_id)
                or (
                    promotion_owner is not None
                    and not promotion_owner.done()
                    and promotion_owner is not asyncio.current_task()
                )
            )
    async with RUNS.active_lock:
        lifecycle_blocker = server_lifecycle_admission_blocker()
        if lifecycle_blocker:
            raise HTTPException(status_code=503, detail=lifecycle_blocker)
        requested_backend = str(
            req.backend or sess.get("backend") or DEFAULT_BACKEND
        ).strip().lower()
        if CODEX_GOALS_RECONFIGURING and requested_backend == BACKEND_CODEX:
            raise TransientAdmissionWait(
                status_code=409,
                detail="wait for Codex goals configuration to finish",
            )
        if session_id in RUNS.maintenance:
            raise TransientAdmissionWait(
                status_code=409,
                detail=(
                    "wait for Claude Stop recovery to finish"
                    if session_id in CLAUDE_STOP_FENCE_SESSIONS
                    else "wait for provider session maintenance to finish"
                ),
            )
        if (
            session_id in RUNS.busy
            or has_prior_queue
            or stop_cleanup_in_progress(session_id)
        ):
            if queue_if_busy:
                should_queue = True
            else:
                raise TransientAdmissionWait(
                    status_code=409,
                    detail="session already has a running turn",
                )
        else:
            RUNS.busy.add(session_id)
            RUNS.current_turns[session_id] = {
                "run_id": None,
                # Private per-admission identity for restart confirmation. A
                # run id is assigned only after several awaited startup
                # checks, so the blocker revision needs its own token to
                # distinguish a replacement reservation in that interval.
                "_server_restart_admission_id": reservation_admission_id,
                "prompt": req.prompt,
                "display_prompt": req.display_prompt,
                "file_ids": list(req.file_ids),
                "backend": req.backend or sess.get("backend") or DEFAULT_BACKEND,
                "purpose": req.purpose,
                "provider_context_mode": provider_context_mode,
                "queued_id": queued_id,
                "steering_lineage": normalized_lineage,
                "client_capabilities": list(req.client_capabilities),
                "skill_selection": (
                    req.skill_selection.model_dump()
                    if req.skill_selection is not None
                    else None
                ),
                "interactive_app_server": interactive_app_server,
                "interactive_agent_sdk": interactive_agent_sdk,
                }
            reserved = True
    if should_queue:
        return await enqueue_turn(session_id, req, sess)

    blocker = await turn_start_blocker(ignore_session_id=session_id)
    if blocker:
        if reserved:
            await release_turn_slot(
                session_id,
                expected_admission_id=reservation_admission_id,
            )
            reserved = False
        raise TransientAdmissionWait(status_code=503, detail=f"agent launch deferred: {blocker}")

    started_event: dict[str, Any] | None = None
    started_payload: dict[str, Any] | None = None
    provider_task_committed = False
    try:
        fields_set = runtime_fields_set
        migration_previous_model = str(
            req.model if "model" in fields_set and req.model else sess.get("model") or ""
        ).strip() or None
        runtime_patch: dict[str, Any] = {}
        if req.backend:
            runtime_patch["backend"] = req.backend
        if "model" in fields_set and req.model is not None:
            runtime_patch["model"] = req.model
        if "effort" in fields_set and req.effort is not None:
            runtime_patch["effort"] = req.effort
        if provider_context_mode == "standalone":
            # Scheduled standalone runs inherit chat settings but never mutate
            # the parent runtime or provider binding. Clearing continuity first
            # also permits a locked parent chat to run a one-off alternate
            # backend. The normal preview semantics clear inherited model and
            # effort whenever that backend changes unless explicitly supplied.
            sess = preview_session_runtime_update(
                standalone_provider_session(sess),
                runtime_patch,
            )
        else:
            # Preserve ordinary chat behavior: per-turn runtime overrides are
            # durable chat selections and backend locking still applies.
            if req.backend:
                sess = await STORE.update(session_id, {"backend": req.backend})
                runtime_patch.pop("backend", None)
            if runtime_patch:
                sess = await STORE.update(session_id, runtime_patch)

        backend = sess.get("backend") or DEFAULT_BACKEND
        runtime_status = await ensure_runtime_available(backend, session=sess)
        if backend == BACKEND_CODEX:
            sess = await migrate_retired_codex_selection(
                session_id, sess, standalone=provider_context_mode == "standalone",
                previous_model=migration_previous_model,
            )
        if provider_context_mode == "chat":
            sess = await STORE.mark_backend_started(session_id, str(backend))

        run_id = f"run_{uuid.uuid4().hex[:16]}"
        async with RUNS.active_lock:
            current_turn = RUNS.current_turns.get(session_id)
            if current_turn is not None:
                current_turn["run_id"] = run_id
                current_turn["backend"] = backend
        manifest_path = codex_manifest_path(session_id)
        with suppress(OSError):
            manifest_path.unlink()
        prompt = build_turn_provider_prompt(
            session_id,
            req.prompt,
            req.file_ids,
            normalized_lineage,
        )
        prompt_file_ids = (
            list(normalized_lineage[-1].get("file_ids") or [])
            if len(normalized_lineage) >= 2
            else list(req.file_ids)
        )
        provider_jobs_access = effective_provider_jobs_access(sess)
        provider_actions = {"publish"}
        if provider_turn_may_raise_emergency(req.purpose):
            provider_actions.update({"emergency", "notify"})
        if (
            provider_turn_may_manage_jobs(req.purpose)
            and provider_jobs_access != "blocked"
        ):
            provider_actions.add("jobs")
        authority_path = await issue_provider_authority(
            session_id,
            run_id,
            actions=provider_actions,
        )
        provider_authority_context = ""
        provider_runtime_env = await provider_authority_runtime_env(
            run_id,
            authority_path,
            session_id,
        )
        provider_turn_payload = ProviderTurnPayload(
            prompt,
            provider_authority_context,
            provider_runtime_env,
        )
        assert_provider_user_message_unchanged(
            provider_turn_payload.user_prompt,
            prompt,
        )
        provider_prompt = provider_turn_payload.user_prompt

        if req.skill_selection is not None:
            # Close the gap between the early queue/admission check and the
            # durable provider-start boundary. Codex force-reloads its native
            # skill list here; Claude re-reads the exact connected SDK
            # generation without replacing that supervisor.
            resolved_provider_command = await resolve_provider_command_selection(
                session_id,
                sess,
                req.skill_selection,
                prompt=req.prompt,
                purpose=req.purpose,
                provider_context_mode=provider_context_mode,
                client_capabilities=list(req.client_capabilities),
                refresh_native=backend == BACKEND_CODEX,
            )

        display_prompt = req.display_prompt if req.display_prompt is not None else req.prompt
        started_payload = {
            "run_id": run_id,
            "backend": backend,
            "prompt": display_prompt,
            "file_ids": display_file_ids if display_file_ids is not None else req.file_ids,
        }
        if backend == BACKEND_CODEX:
            effective_model, effective_effort, _service_tier = codex_runtime_settings(sess)
            started_payload["model"] = effective_model
            started_payload["effort"] = effective_effort
        else:
            started_payload["model"] = sess.get("model")
            started_payload["effort"] = sess.get("effort")
        if getattr(req, "client_request_id", None):
            started_payload["client_request_id"] = req.client_request_id
        if queued_id:
            started_payload["queued_id"] = queued_id
        run_metadata = {
            "purpose": req.purpose,
            **(getattr(req, "shared_chat_metadata", None) or {}),
            "provider_context_mode": provider_context_mode,
            "job_id": req.job_id,
            "job_title": req.job_title,
            "job_scheduled_run_at": req.job_scheduled_run_at,
            "steer_interrupted_run_id": req.steer_interrupted_run_id,
        }
        if req.purpose == "scheduled_job":
            run_metadata["job_context_mode"] = provider_context_mode
            run_metadata["job_revision"] = scheduled_job_revision
            run_metadata["manual_run"] = scheduled_job_manual_run
        run_metadata = {key: value for key, value in run_metadata.items() if value is not None}
        if run_metadata:
            RUNS.run_metadata[run_id] = run_metadata
            started_payload.update(run_metadata)
        # Scheduled occurrences are authorized by this exact durable source
        # admission marker, and queued or client-identified requests need it
        # for exactly-once recovery.
        started_event = await (
            append_durable_event(
                session_id,
                "turn_started",
                started_payload,
            )
            if (
                req.purpose == "scheduled_job"
                or getattr(req, "shared_chat_id", None) is not None
                or queued_id is not None
                or getattr(req, "client_request_id", None) is not None
            )
            else append_event(session_id, "turn_started", started_payload)
        )
        # A shared tmux daemon can predate this process and retain credentials
        # from an older server build. Scrub it immediately before provider
        # launch even when the user never opened the terminal UI.
        await asyncio.to_thread(scrub_tmux_global_secret_environment)
        if backend == BACKEND_CODEX:
            assert_provider_user_message_unchanged(
                provider_turn_payload.user_prompt,
                prompt,
            )
            task = WIRING.run_codex(
                session_id,
                run_id,
                provider_turn_payload.user_prompt,
                dict(sess),
                manifest_path,
                interactive_app_server=interactive_app_server,
                provider_command=resolved_provider_command,
                provider_runtime_env=provider_turn_payload.runtime_env,
                **(
                    {"standalone_provider_context": True}
                    if provider_context_mode == "standalone"
                    else {}
                ),
            )
        else:
            assert_provider_user_message_unchanged(
                provider_turn_payload.user_prompt,
                prompt,
            )
            task = WIRING.run_claude(
                session_id,
                run_id,
                provider_turn_payload.user_prompt,
                dict(sess),
                manifest_path,
                interactive_agent_sdk=interactive_agent_sdk,
                provider_command=resolved_provider_command,
                provider_runtime_env=provider_turn_payload.runtime_env,
                **(
                    {"standalone_provider_context": True}
                    if provider_context_mode == "standalone"
                    else {}
                ),
            )
        turn_task = asyncio.create_task(supervise_provider_turn_task(
            session_id,
            run_id,
            str(backend),
            task,
        ))
        register_session_task(RUNS.turn_tasks, session_id, turn_task)
        provider_task_committed = True
        try:
            first_line = (req.prompt.strip().splitlines() or ["New chat"])[0]
            title_changed = await STORE.adopt_auto_title(
                session_id, first_line[:72] or "New chat", source="prompt",
                prompt_seed=req.prompt if req.purpose is None else None,
            )
            if not title_changed:
                await STORE.update(session_id, {})
        except Exception:
            # Provider ownership is already committed. Title persistence is a
            # presentation enhancement and cannot turn an accepted live run
            # into an apparent launch failure.
            logger.exception(
                "failed to persist post-admission title session=%s run=%s",
                session_id,
                run_id,
            )
        return {"run_id": run_id, "queued": False, "session": public_session(STORE.sessions[session_id]), "event": started_event}
    except BaseException as start_error:
        # Once the provider task is registered, it exclusively owns the live
        # turn and all of its authority/metadata cleanup.  A later response
        # decoration failure (for example, persisting the generated title)
        # must not revoke that running provider or release its slot out from
        # underneath it.
        if provider_task_committed:
            raise
        failed_run_id = str(locals().get("run_id") or "")

        async def settle_precommit_failure() -> BaseException | None:
            """Finish the failed admission's owned side effects exactly once."""

            cleanup_error: BaseException | None = None
            if started_event is not None and started_payload is not None:
                with suppress(BaseException):
                    await append_event(session_id, "turn_stopped", {
                        **started_payload,
                        "stopped": True,
                        "message": "Agent launch failed after durable turn admission.",
                    })
            if failed_run_id:
                try:
                    await revoke_provider_authority(failed_run_id)
                except BaseException as revoke_error:
                    if cleanup_error is None:
                        cleanup_error = revoke_error
                # Metadata is installed before provider-task commit. Remove
                # only this failed admission's record; a replacement run may
                # already own the session by the time cleanup reaches here.
                RUNS.run_metadata.pop(failed_run_id, None)
            if reserved:
                # Fenced cleanup cannot release a newer provider that took
                # over after this failed admission was stopped or cancelled.
                try:
                    await release_turn_slot(
                        session_id,
                        **(
                            {"expected_run_id": failed_run_id}
                            if failed_run_id
                            else {
                                "expected_admission_id": reservation_admission_id
                            }
                        ),
                    )
                except BaseException as release_error:
                    if cleanup_error is None:
                        cleanup_error = release_error
            return cleanup_error

        settlement_task = asyncio.create_task(settle_precommit_failure())
        cleanup_cancellation: asyncio.CancelledError | None = None
        try:
            cleanup_error = await asyncio.shield(settlement_task)
        except asyncio.CancelledError as cancellation:
            cleanup_cancellation = cancellation
            try:
                cleanup_error = await join_task_despite_caller_cancellation(
                    settlement_task
                )
            except BaseException as settlement_error:
                raise settlement_error from start_error
        except BaseException as settlement_error:
            raise settlement_error from start_error
        if cleanup_error is not None:
            raise cleanup_error from start_error
        if cleanup_cancellation is not None:
            raise cleanup_cancellation
        if (started_event is None and isinstance(start_error, OSError)
                and start_error.errno is not None
                and start_error.errno in {errno.ENOSPC, getattr(errno, "EDQUOT", errno.ENOSPC)}):
            # No provider task was registered and existing rollback completed.
            # Reuse the ordinary admission retry path; the next attempt must
            # succeed at its real acceptance write, not a disk-space guess.
            raise TransientAdmissionWait(
                status_code=503,
                detail="Server state storage is full. No agent turn was started. Free storage and retry.",
            ) from start_error
        raise


async def reconcile_idle_queue_session_from_snapshot(session_id: str) -> bool:
    """Run the per-chat queue repair from a session GET at most once per interval.

    Desktop clients fetch the session snapshot on every open and scroll; the
    repair pass is not a per-request duty.
    """

    now = time.monotonic()
    last = SESSION_SNAPSHOT_RECONCILE_AT.get(session_id, float("-inf"))
    if now - last < HEALTH_QUEUE_RECONCILE_INTERVAL_SECONDS:
        return False
    SESSION_SNAPSHOT_RECONCILE_AT[session_id] = now
    if len(SESSION_SNAPSHOT_RECONCILE_AT) > 4096:
        stale = [
            key
            for key, at in SESSION_SNAPSHOT_RECONCILE_AT.items()
            if now - at >= HEALTH_QUEUE_RECONCILE_INTERVAL_SECONDS
        ]
        for key in stale:
            SESSION_SNAPSHOT_RECONCILE_AT.pop(key, None)
    return await reconcile_idle_queue_session(
        session_id,
        schedule=True,
        reason="session_snapshot",
    )


async def reconcile_idle_queued_turns_from_health_poll() -> bool:
    """Schedule one queue repair pass without putting it on health's latency path."""

    if SERVER.shutting_down:
        return False
    state = HEALTH_QUEUE_RECONCILE_STATE
    running = state.get("task")
    if isinstance(running, asyncio.Task) and not running.done():
        return False
    now = time.monotonic()
    if (
        now - float(state.get("last_at", float("-inf")))
        < HEALTH_QUEUE_RECONCILE_INTERVAL_SECONDS
    ):
        return False
    state["last_at"] = now
    task = asyncio.create_task(
        reconcile_idle_queued_turns(reason="health_poll"),
        name="health-queue-reconcile",
    )
    state["task"] = task

    def reconcile_finished(completed: asyncio.Task[Any]) -> None:
        if state.get("task") is completed:
            state["task"] = None
        if completed.cancelled():
            return
        error = completed.exception()
        if error is not None:
            logger.error(
                "health queue reconcile failed",
                exc_info=(type(error), error, error.__traceback__),
            )

    task.add_done_callback(reconcile_finished)
    return True
