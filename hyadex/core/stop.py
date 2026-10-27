"""Explicit Stop and recovery of turns abandoned by a restart."""
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
from hyadex.core.paths import events_path
from hyadex.core.processes import terminate_process_tree
from hyadex.core.queue import (
    append_durable_event,
    await_native_steer_result,
    codex_goal_followup_requires_native,
    codex_goal_steer_selection_is_plain,
    deferred_force_send_outcome,
    fence_native_steer_delivery,
    force_send_conflict_detail,
    NonNativeForceSendRequiresLifecycleLock,
    pause_queued_turns_after_explicit_stop,
    prepare_steered_turn,
    requeue_native_steer_after_safe_rejection,
    stop_idle_claude_background_subagents_bounded,
    wait_for_queue_recovery_admission,
    withdraw_unaccepted_native_steer,
)
from hyadex.core.runs import RUNS
from hyadex.core.server_logging import concise_error_message
from hyadex.core.sessions import (
    append_event,
    CLAUDE_STOP_FENCE_SESSIONS,
    ensure_session_not_deleting,
    existing_codex_app_server_manager,
    register_session_task,
    revoke_provider_authority,
    server_lifecycle_admission_blocker,
    session_lifecycle_lock,
    STORE,
)
from hyadex.core.state import (
    RUN_NOW_COMPLETED_MAX,
    RUN_NOW_COMPLETED_RESULTS,
    RUN_NOW_COMPLETED_TTL_SECONDS,
)
from hyadex.core.turns import (
    reconcile_idle_queue_session,
    release_turn_slot,
    schedule_claude_stop_fence_retry,
    schedule_next_queued_turn,
    schedule_steered_turn_slot_waiter,
)
from hyadex.core.util import bounded_lock
from hyadex.features.usage import join_task_despite_caller_cancellation
from hyadex.providers.authority import NativeSteerHandoffError
from hyadex.providers.claude.history import session_provider_id
from hyadex.providers.claude.runtime import (
    cancel_claude_interactions,
    detached_stop_in_progress,
    interrupt_claude_sdk_run_bounded,
    queued_claude_runtime_matches_active,
    stop_cleanup_in_progress,
)
from hyadex.providers.codex.runtime import (
    cancel_codex_interactions,
    CODEX_NATIVE_ACTION_TASKS,
    pause_active_codex_goal_for_stop,
    quarantine_codex_goal_thread,
    queued_codex_runtime_matches_active,
    release_codex_control_slot,
    settle_idle_codex_goal_for_stop,
)
from hyadex.providers.registry import normalize_steering_lineage, queued_turn_uses_retired_routing
from hyadex.providers.subagents import (
    discard_unused_claude_stop_fence_attempt_lock,
    empty_subagent_stop_result,
    evict_claude_sdk_chat,
    stop_codex_descendant_subagents,
)

logger = logging.getLogger("hyadex-server")


async def run_queued_turn_now(session_id: str, queued_id: str) -> dict[str, Any]:
    """Coalesce duplicate Force Send requests without replaying a steer."""
    await wait_for_queue_recovery_admission(session_id)
    ensure_session_not_deleting(session_id)
    await reconcile_idle_queue_session(
        session_id,
        schedule=False,
        reason="force_send_admission",
    )
    cached: dict[str, Any] | None = None
    async with RUNS.run_now_request_lock:
        # Delete reserves the tombstone before acquiring this same lock. A
        # request that was waiting behind deletion must not register a new
        # control task after deletion captured the previous owner.
        ensure_session_not_deleting(session_id)
        now = time.monotonic()
        for key, outcome in list(RUN_NOW_COMPLETED_RESULTS.items()):
            if float(outcome.get("expires_at") or 0) <= now:
                RUN_NOW_COMPLETED_RESULTS.pop(key, None)
        cache_key = (session_id, queued_id)
        cached = RUN_NOW_COMPLETED_RESULTS.get(cache_key)
        if cached is not None:
            RUN_NOW_COMPLETED_RESULTS.move_to_end(cache_key)
        existing = RUNS.run_now_requests.get(session_id)
        if cached is not None:
            task = None
        elif existing is not None:
            existing_queued_id, existing_task = existing
            if existing_queued_id != queued_id:
                raise HTTPException(
                    status_code=409,
                    detail=force_send_conflict_detail(
                        session_id,
                        queued_id,
                        guard="run_now_request",
                        message=(
                            "Another Force Send is already being applied. "
                            "Wait for it to finish before steering a different message."
                        ),
                        action="Refresh the queue before trying a different message.",
                        retryable=True,
                        owner_queued_id=existing_queued_id,
                        owner_task=existing_task,
                    ),
                )
            task = existing_task
        else:
            operation_id = f"force_send_{uuid.uuid4().hex[:16]}"
            task = asyncio.create_task(
                _run_queued_turn_now_and_release(session_id, queued_id)
            )
            setattr(task, "_hyadex_force_send_operation_id", operation_id)
            setattr(task, "_hyadex_force_send_queued_id", queued_id)
            setattr(
                task,
                "_hyadex_force_send_started_at",
                time.monotonic(),
            )
            setattr(task, "_hyadex_force_send_phase", "admission")
            RUNS.run_now_requests[session_id] = (queued_id, task)

    if cached is not None:
        error = cached.get("error")
        if isinstance(error, dict):
            raise NativeSteerHandoffError(
                str(error.get("message") or "Force Send delivery is uncertain"),
                safe_to_requeue=False,
                delivery_uncertain=bool(
                    error.get("delivery_uncertain", True)
                ),
            )
        result = cached.get("result")
        if isinstance(result, dict):
            return dict(result)
        raise HTTPException(status_code=409, detail="Force Send result is unavailable")

    # A dropped HTTP connection must not cancel a steer that may already have
    # reached the provider. Later duplicate requests join this task.
    assert task is not None
    return await asyncio.shield(task)


async def _run_queued_turn_now_and_release(
    session_id: str,
    queued_id: str,
) -> dict[str, Any]:
    result: dict[str, Any] | None = None
    uncertain_error: dict[str, Any] | None = None
    try:
        try:
            # Native steering already has its own provider handoff fence and
            # must remain able to complete without waiting behind a Stop that
            # targets the same live provider turn.  A non-native Force Send,
            # however, is a Stop-then-start lifecycle transition and must be
            # serialized with the public Stop endpoint.
            result = await _run_queued_turn_now_once(
                session_id,
                queued_id,
                require_native=True,
            )
        except NonNativeForceSendRequiresLifecycleLock:
            async with session_lifecycle_lock(session_id):
                result = await _run_queued_turn_now_once(session_id, queued_id)
        return result
    except NativeSteerHandoffError as exc:
        if exc.safe_to_requeue:
            result = await deferred_force_send_outcome(session_id, queued_id, exc)
            return result
        if exc.delivery_uncertain or not exc.safe_to_requeue:
            uncertain_error = {
                "message": str(exc),
                "delivery_uncertain": bool(exc.delivery_uncertain),
            }
        raise
    finally:
        owner_task = asyncio.current_task()

        async def settle_force_send_request() -> None:
            async with RUNS.run_now_request_lock:
                paused_after_stop = False
                if (
                    result is not None
                    and result.get("ok") is True
                    and not result.get("deferred")
                ):
                    # Stop may durably pause a non-native promotion after its
                    # handoff returned but before this completion cache is written.
                    # Validate under the same lock order used by Stop so a later
                    # Run Now can never receive stale success for a held message.
                    async with RUNS.queue_lock:
                        candidates = list(RUNS.queued.get(session_id) or ())
                        promoted = RUNS.run_now.get(session_id)
                        if promoted is not None:
                            candidates.append(promoted)
                        paused_after_stop = any(
                            str(item.get("queued_id") or "") == queued_id
                            and item.get("_paused_after_stop") is True
                            for item in candidates
                        )
                cacheable_result = (
                    result
                    if result is not None
                    and result.get("ok") is True
                    and not result.get("deferred")
                    and not paused_after_stop
                    else None
                )
                if cacheable_result is not None or uncertain_error is not None:
                    cache_key = (session_id, queued_id)
                    RUN_NOW_COMPLETED_RESULTS[cache_key] = {
                        "expires_at": (
                            time.monotonic() + RUN_NOW_COMPLETED_TTL_SECONDS
                        ),
                        **(
                            {"result": dict(cacheable_result)}
                            if cacheable_result is not None
                            else {"error": uncertain_error}
                        ),
                    }
                    RUN_NOW_COMPLETED_RESULTS.move_to_end(cache_key)
                    while len(RUN_NOW_COMPLETED_RESULTS) > RUN_NOW_COMPLETED_MAX:
                        RUN_NOW_COMPLETED_RESULTS.popitem(last=False)
                current = RUNS.run_now_requests.get(session_id)
                if current is not None and current[1] is owner_task:
                    RUNS.run_now_requests.pop(session_id, None)

        settlement = asyncio.create_task(settle_force_send_request())
        try:
            await asyncio.shield(settlement)
        except BaseException:
            with suppress(BaseException):
                await join_task_despite_caller_cancellation(settlement)


async def _run_queued_turn_now_once(
    session_id: str,
    queued_id: str,
    *,
    require_native: bool = False,
) -> dict[str, Any]:
    if session_id not in STORE.sessions:
        raise HTTPException(status_code=404, detail="session not found")
    if stop_cleanup_in_progress(session_id):
        raise HTTPException(
            status_code=409,
            detail="Stop is still finishing for this chat; retry Force Send shortly",
        )

    async with RUNS.active_lock:
        active_turn = dict(RUNS.active.get(session_id) or {})
        interrupted_turn = dict(RUNS.current_turns.get(session_id) or {})

    selected: dict[str, Any] | None = None
    selected_index: int | None = None
    selected_predecessor_id: str | None = None
    selected_successor_id: str | None = None
    selected_was_paused = False
    native_steer = False
    goal_followup = codex_goal_followup_requires_native(
        STORE.sessions[session_id], active_turn, interrupted_turn,
    )
    # A goal may be created during an ordinary turn, including a turn with
    # run-bound provider authority. Its goal-only lane preserves that owner;
    # it must never reopen the generic logical-run/authority replacement lane.
    native_steer_queue_key = (
        "codex_goal_steer_queue"
        if goal_followup and active_turn.get("codex_native_operation_kind") != "goal_resume"
        else "native_steer_queue"
    )
    native_steer_queue = active_turn.get(native_steer_queue_key)
    selected_backend = str(
        STORE.sessions[session_id].get("backend") or DEFAULT_BACKEND
    )
    remaining: int
    async with RUNS.queue_lock:
        lifecycle_blocker = server_lifecycle_admission_blocker()
        if lifecycle_blocker:
            raise HTTPException(status_code=503, detail=lifecycle_blocker)
        promoted_owner = RUNS.run_now.get(session_id)
        if session_id in RUNS.steering or promoted_owner is not None:
            waiter_entry = RUNS.steering_waits.get(session_id)
            guard = (
                "run_now_promotion"
                if promoted_owner is not None
                else "steering_reservation"
            )
            raise HTTPException(
                status_code=409,
                detail=force_send_conflict_detail(
                    session_id,
                    queued_id,
                    guard=guard,
                    message=(
                        "Force Send is already being applied. "
                        "Wait for it to finish before steering another message."
                    ),
                    action="Refresh the queue and retry after the current promotion settles.",
                    retryable=True,
                    owner_queued_id=(
                        str(promoted_owner.get("queued_id") or "")
                        if promoted_owner is not None
                        else None
                    ),
                    owner_task=(
                        waiter_entry[2]
                        if waiter_entry is not None
                        else None
                    ),
                ),
            )
        queue = RUNS.queued.get(session_id)
        if queue:
            items = list(queue)
            selected_index = next(
                (idx for idx, item in enumerate(items) if item.get("queued_id") == queued_id),
                None,
            )
            if selected_index is not None:
                selected = items[selected_index]
                if selected.get("_native_delivery_fenced") is True:
                    raise HTTPException(
                        status_code=409,
                        detail=force_send_conflict_detail(
                            session_id,
                            queued_id,
                            guard="delivery_uncertain",
                            message=(
                                "This message may already have reached the "
                                "provider and cannot be Force Sent again."
                            ),
                            action=(
                                "Inspect the provider history, then remove "
                                "this held queue item when it is safe."
                            ),
                            retryable=False,
                            owner_queued_id=queued_id,
                        ),
                    )
                if selected.get("purpose") in RETIRED_TURN_PURPOSES | {"scheduled_job"}:
                    raise HTTPException(
                        status_code=409,
                        detail=force_send_conflict_detail(
                            session_id,
                            queued_id,
                            guard="selected_internal_turn",
                            message=(
                                "A scheduled run cannot Force Send or "
                                "steer another turn."
                            ),
                            action=(
                                "Let the scheduled run start in queue order "
                                "or remove it from the queue."
                            ),
                            retryable=False,
                            owner_queued_id=queued_id,
                        ),
                    )
                # Explicit Send now promotes only the selected pending row;
                # every earlier row retains its relative order and content.
                selected_was_paused = (
                    selected.get("_paused_after_stop") is True
                )
                if selected_index > 0:
                    selected_predecessor_id = str(
                        items[selected_index - 1].get("queued_id") or ""
                    ) or None
                if selected_index + 1 < len(items):
                    selected_successor_id = str(
                        items[selected_index + 1].get("queued_id") or ""
                    ) or None
                selected_backend = str(
                    selected.get("backend")
                    or STORE.sessions[session_id].get("backend")
                    or DEFAULT_BACKEND
                )
                # QUEUE_LOCK admission and async message lookup can yield while
                # the live provider creates a goal or replaces its turn. Read
                # the latest owner here without an inverse ACTIVE/QUEUE lock
                # acquisition. There is no await before the decision below.
                # Once observed, goal protection must not turn into Stop
                # fallback merely because the goal cache changes meanwhile.
                active_turn = dict(RUNS.active.get(session_id) or {})
                interrupted_turn = dict(RUNS.current_turns.get(session_id) or {})
                goal_followup = goal_followup or codex_goal_followup_requires_native(
                    STORE.sessions[session_id], active_turn, interrupted_turn,
                )
                native_steer_queue_key = (
                    "codex_goal_steer_queue"
                    if goal_followup and active_turn.get("codex_native_operation_kind") != "goal_resume"
                    else "native_steer_queue"
                )
                native_steer_queue = active_turn.get(native_steer_queue_key)
                native_steer = bool(
                    (
                        active_turn.get("provider_turn_ready")
                        or (
                            goal_followup
                            and active_turn.get("codex_native_operation_kind") == "goal_resume"
                        )
                    )
                    and native_steer_queue is not None
                    and (
                        not goal_followup or (
                            isinstance(STORE.sessions[session_id].get("codex_goal"), dict)
                            and STORE.sessions[session_id]["codex_goal"].get("status") == "active"
                            and not STORE.sessions[session_id].get("codex_goal_time_budget_exhausted")
                            and codex_goal_steer_selection_is_plain(selected)
                        )
                    )
                    and not queued_turn_uses_retired_routing(selected)
                    and selected.get("skill_selection") is None
                    # Only ordinary logical-run replacement issues fresh
                    # authority. A goal steer keeps its exact owner and does
                    # not replace the original turn's references or command.
                    and (
                        goal_followup or (
                            interrupted_turn.get("skill_selection") is None
                            and not queued_turn_uses_retired_routing(interrupted_turn)
                        )
                    )
                    and (
                        (
                            active_turn.get("transport")
                            == CODEX_TRANSPORT_APP_SERVER
                            and selected_backend == BACKEND_CODEX
                            # Native goal steering sends input to the running
                            # turn. Picker changes apply to future turns; they
                            # do not need to match its already pinned runtime.
                            and (
                                goal_followup or queued_codex_runtime_matches_active(
                                    session_id, selected, active_turn,
                                )
                            )
                        )
                        or (
                            active_turn.get("transport")
                            == CLAUDE_TRANSPORT_AGENT_SDK
                            and selected_backend == BACKEND_CLAUDE
                            and queued_claude_runtime_matches_active(
                                session_id,
                                selected,
                                active_turn,
                            )
                        )
                    )
                )
                if goal_followup and not native_steer:
                    unsupported_input = not codex_goal_steer_selection_is_plain(selected)
                    raise HTTPException(
                        status_code=409,
                        detail=force_send_conflict_detail(
                            session_id, queued_id,
                            guard="active_goal_requires_native_steer",
                            message=(
                                "This queued action adds chat access or invokes a provider command, which cannot be applied inside the running Codex turn."
                                if unsupported_input else
                                "Codex does not have a running goal turn ready to receive this message."
                            ),
                            action=(
                                "It remains queued for a new turn. Text and file follow-ups can be sent now."
                                if unsupported_input else
                                "Your message remains queued. Retry Send now when Codex is running."
                            ),
                            retryable=not unsupported_input,
                            owner_queued_id=queued_id,
                        ),
                    )
                if require_native and not native_steer:
                    # Probe before removing or marking the queue item.  The
                    # caller will retry under the per-chat lifecycle lock so
                    # explicit Stop cannot interleave between interruption and
                    # promotion of this queued message.
                    raise NonNativeForceSendRequiresLifecycleLock
                if native_steer:
                    # Provider-side fencing happens after this item leaves the
                    # live deque. Preserve its exact durable position so crash
                    # recovery and a safe rejection can restore the same order.
                    selected["_native_delivery_queue_position"] = (
                        selected_index + 1
                    )
                kept = deque(
                    item
                    for idx, item in enumerate(items)
                    if idx != selected_index
                )
                if kept:
                    RUNS.queued[session_id] = kept
                    remaining = len(kept)
                else:
                    RUNS.queued.pop(session_id, None)
                    remaining = 0
            else:
                remaining = len(items)
        else:
            remaining = 0
        if selected is not None:
            RUNS.steering.add(session_id)
            if not native_steer:
                # The original queue event is durable, but its promotion is
                # not restart-safe until turn_queue_run_now is persisted.
                selected["_update_transitioning"] = True
                RUNS.run_now[session_id] = selected

    if selected is None:
        raise HTTPException(status_code=404, detail="queued turn not found")
    # Selecting Run Now is an explicit request to release a queue hold created
    # by Stop. Native steering does not pass through start_next_queued_turn(),
    # so clear it here for both transport paths.
    selected["_paused_after_stop"] = False

    if native_steer:
        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        owner_task = asyncio.current_task()
        if owner_task is not None:
            setattr(
                owner_task,
                "_hyadex_force_send_phase",
                "provider_handoff_queued",
            )
        try:
            request = {
                "selected": selected,
                "remaining": remaining,
                "future": future,
                "phase": "queued",
                "accepted_event": asyncio.Event(),
                "owner_task": owner_task,
                "expected_provider_turn_id": (
                    str(active_turn.get("provider_turn_id") or "")
                    if active_turn.get("provider_turn_ready") else ""
                ),
                "goal_identity": (
                    str((STORE.sessions[session_id].get("codex_goal") or {}).get("id") or ""),
                    str((STORE.sessions[session_id].get("codex_goal") or {}).get("objective") or ""),
                ) if goal_followup else None,
            }
            # Commit delivery against the live ACTIVE record. The provider can
            # become terminal after the initial eligibility snapshot; putting
            # into that now-detached queue would strand this Future forever.
            async with RUNS.active_lock:
                current = RUNS.active.get(session_id)
                if (
                    not current
                    or not (
                        current.get("provider_turn_ready")
                        or (
                            goal_followup
                            and current.get("codex_native_operation_kind") == "goal_resume"
                        )
                    )
                    or current.get(native_steer_queue_key) is not native_steer_queue
                    or str(current.get("run_id") or "")
                    != str(active_turn.get("run_id") or "")
                ):
                    raise NativeSteerHandoffError(
                        "The active provider turn finished before steering delivery",
                        safe_to_requeue=True,
                    )
                if not current.get("provider_turn_ready"):
                    # The native goal owner will bind the pending message to
                    # its next turn/started event, never a stale completed ID.
                    request["expected_provider_turn_id"] = ""
                try:
                    native_steer_queue.put_nowait(request)
                except asyncio.QueueFull as exc:
                    raise NativeSteerHandoffError(
                        "The active provider is already applying another steering message",
                        safe_to_requeue=True,
                    ) from exc
            result = await await_native_steer_result(
                session_id,
                selected,
                backend=selected_backend,
                native_steer_queue=native_steer_queue,
                request=request,
                future=future,
            )
            result["remaining"] = remaining
            return result
        except BaseException as exc:
            safe_to_requeue = bool(getattr(exc, "safe_to_requeue", False))
            if isinstance(exc, asyncio.CancelledError):
                safe_to_requeue = withdraw_unaccepted_native_steer(
                    native_steer_queue,
                    request,
                )
                if not safe_to_requeue:
                    fence_task = asyncio.create_task(
                        fence_native_steer_delivery(
                            session_id,
                            selected,
                            backend=selected_backend,
                        )
                    )
                    try:
                        await asyncio.shield(fence_task)
                    except BaseException:
                        with suppress(BaseException):
                            await join_task_despite_caller_cancellation(
                                fence_task
                            )
            if selected_index is not None and safe_to_requeue:
                requeue_task = asyncio.create_task(
                    requeue_native_steer_after_safe_rejection(
                        session_id,
                        selected,
                        selected_index=selected_index,
                        selected_predecessor_id=selected_predecessor_id,
                        selected_successor_id=selected_successor_id,
                        preserve_pause=goal_followup,
                    )
                )
                try:
                    await asyncio.shield(requeue_task)
                except BaseException:
                    await join_task_despite_caller_cancellation(requeue_task)
            raise
        finally:
            async def release_native_steering_fence() -> None:
                async with RUNS.queue_lock:
                    RUNS.steering.discard(session_id)
                async with RUNS.active_lock:
                    still_busy = session_id in RUNS.busy
                if not still_busy:
                    schedule_next_queued_turn(session_id)

            cleanup = asyncio.create_task(release_native_steering_fence())
            try:
                await asyncio.shield(cleanup)
            except BaseException:
                with suppress(BaseException):
                    await join_task_despite_caller_cancellation(cleanup)

    promotion_committed = False
    try:
        owner_task = asyncio.current_task()
        if owner_task is not None:
            setattr(
                owner_task,
                "_hyadex_force_send_phase",
                "stopping_active_turn",
            )
        claude_sdk_stop_run_id = (
            str(active_turn.get("run_id") or interrupted_turn.get("run_id") or "").strip()
            if active_turn.get("transport") == CLAUDE_TRANSPORT_AGENT_SDK
            else ""
        )
        stop_result = await stop_turn(
            session_id,
            expected_run_id=claude_sdk_stop_run_id or None,
            emit_event=False,
            schedule_queue=False,
            require_provider_turn_ready=True,
            cascade_codex_subagents=False,
            cascade_claude_subagents=False,
            hard_terminalize_on_timeout=bool(claude_sdk_stop_run_id),
            pause_queued_turns_on_stop=False,
            preserve_active_goal=True,
        )
        # A pending interrupt has not released the provider slot. Promoting it
        # would publish "Starting" and leave an unbounded BUSY waiter behind.
        deferred = bool(
            stop_result.get("deferred")
            or stop_result.get("pending")
            or stop_result.get("superseded")
        )
        interrupted = bool(stop_result.get("stopped")) and not deferred
        if deferred:
            already_notified = bool(selected.get("_turn_deferred_notified"))
            selected["_turn_deferred_notified"] = True
            selected.pop("_update_transitioning", None)
            async with RUNS.queue_lock:
                RUNS.run_now.pop(session_id, None)
                RUNS.steering.discard(session_id)
                queue_items = list(RUNS.queued.get(session_id) or [])
                insert_at = max(0, min(int(selected_index or 0), len(queue_items)))
                queue_items.insert(insert_at, selected)
                RUNS.queued[session_id] = deque(queue_items)
                remaining = len(queue_items)
            if stop_result.get("pending"):
                message = (
                    "Stop is still finishing. This message remains queued; "
                    "try Force Send again shortly."
                )
            elif stop_result.get("superseded"):
                message = (
                    "The active turn changed before it could be stopped. "
                    "This message remains queued; try Force Send again shortly."
                )
            else:
                message = (
                    "The provider is still starting, so the current turn was not interrupted. "
                    "This message remains queued; try Force Send again shortly."
                )
            if not already_notified:
                await append_event(session_id, "turn_deferred", {
                    "queued_id": queued_id,
                    "message": message,
                    "remaining": remaining,
                })
            async with RUNS.active_lock:
                still_busy = session_id in RUNS.busy
            if not still_busy:
                schedule_next_queued_turn(session_id)
            return {
                "ok": False,
                "queued_id": queued_id,
                "interrupted": False,
                "deferred": True,
                "replays_interrupted_message": False,
                "message": message,
                "remaining": remaining,
                "superseded_queued_ids": [],
            }
        prepared = prepare_steered_turn(selected, interrupted_turn if interrupted else None)
        async with RUNS.queue_lock:
            current = RUNS.run_now.get(session_id)
            if current and current.get("queued_id") == queued_id:
                RUNS.run_now[session_id] = prepared

        display_prompt = str(prepared.get("display_prompt") or prepared.get("steering_prompt") or selected.get("prompt") or "")
        if owner_task is not None:
            setattr(
                owner_task,
                "_hyadex_force_send_phase",
                "committing_promotion",
            )
        await append_durable_event(session_id, "turn_queue_run_now", {
            "queued_id": queued_id,
            "backend": prepared.get("backend") or STORE.sessions[session_id].get("backend") or DEFAULT_BACKEND,
            "prompt": display_prompt,
            "request_prompt": prepared.get("prompt") or "",
            "display_prompt": display_prompt,
            "file_ids": list(prepared.get("file_ids") or []),
            "display_file_ids": list(
                prepared.get("display_file_ids")
                if prepared.get("display_file_ids") is not None
                else selected.get("file_ids") or []
            ),
            "interrupted_run_id": prepared.get("steer_interrupted_run_id"),
            "replays_interrupted_message": bool(prepared.get("replays_interrupted_message")),
            "steering_lineage": normalize_steering_lineage(prepared.get("steering_lineage")),
            "purpose": prepared.get("purpose"),
            "skill_selection": prepared.get("skill_selection"),
            "message": (
                "Steering message promoted; it will continue the confirmed native provider thread."
                if interrupted
                else "Queued message promoted to run next."
            ),
            "remaining": remaining,
            "superseded_queued_ids": [],
        })
        promotion_committed = True
        prepared.pop("_update_transitioning", None)
        schedule_steered_turn_slot_waiter(session_id, queued_id)
        if owner_task is not None:
            setattr(
                owner_task,
                "_hyadex_force_send_phase",
                "promotion_committed",
            )
        return {
            "ok": True,
            "queued_id": queued_id,
            "interrupted": interrupted,
            "deferred": deferred,
            "replays_interrupted_message": bool(prepared.get("replays_interrupted_message")),
            "superseded_queued_ids": [],
        }
    except BaseException:
        async def restore_uncommitted_promotion() -> None:
            async with RUNS.queue_lock:
                if not promotion_committed:
                    current = RUNS.run_now.get(session_id)
                    if current is not None and current.get("queued_id") == queued_id:
                        RUNS.run_now.pop(session_id, None)
                    selected.pop("_update_transitioning", None)
                    selected["_paused_after_stop"] = selected_was_paused
                    items = list(RUNS.queued.get(session_id) or ())
                    if not any(
                        item.get("queued_id") == queued_id
                        for item in items
                    ):
                        successor_index = (
                            next(
                                (
                                    idx
                                    for idx, item in enumerate(items)
                                    if item.get("queued_id") == selected_successor_id
                                ),
                                None,
                            )
                            if selected_successor_id is not None
                            else None
                        )
                        predecessor_index = (
                            next(
                                (
                                    idx
                                    for idx, item in enumerate(items)
                                    if item.get("queued_id") == selected_predecessor_id
                                ),
                                None,
                            )
                            if selected_predecessor_id is not None
                            else None
                        )
                        if successor_index is not None:
                            insert_at = successor_index
                        elif predecessor_index is not None:
                            insert_at = predecessor_index + 1
                        else:
                            insert_at = min(int(selected_index or 0), len(items))
                        items.insert(insert_at, selected)
                        RUNS.queued[session_id] = deque(items)
                RUNS.steering.discard(session_id)
            schedule_next_queued_turn(session_id)

        # Force Send temporarily turns a durable queue row into a restart-
        # blocking transition. A second cancellation can arrive while rollback
        # waits for QUEUE_LOCK; settle the exact rollback child before exposing
        # cancellation so `_update_transitioning` cannot become a stale managed-
        # update blocker and its steering fence cannot become ownerless.
        restoration = asyncio.create_task(restore_uncommitted_promotion())
        try:
            await asyncio.shield(restoration)
        except BaseException:
            with suppress(BaseException):
                await join_task_despite_caller_cancellation(restoration)
        raise


RESTART_ORPHAN_ACTIVITY_TYPES = {
    "turn_started",
    "process_started",
    "provider_session",
    "assistant_text",
    "reasoning_summary",
    "reasoning_text",
    "tool_started",
    "tool_finished",
    "artifact_created",
    "idle_warning",
    # Provider errors are emitted before the normal turn_finished marker. A
    # crash in that narrow window still leaves an open turn to retire.
    "error",
}
RESTART_ORPHAN_TERMINAL_TYPES = {
    "turn_finished",
    "turn_stopped",
}


def abandoned_turn_after_restart(
    session_id: str,
    session: dict[str, Any],
) -> dict[str, Any] | None:
    """Return the newest run left open when the server process disappeared.

    Active turns live in memory, so a SIGKILL cannot persist their normal
    terminal event.  Restrict the recovery scan to chats whose last durable
    event is turn activity; this keeps startup proportional to the number of
    interrupted chats instead of rescanning every large history.
    """

    persisted_active = (
        dict(session.get("active_run"))
        if isinstance(session.get("active_run"), dict)
        else {}
    )
    latest_type = str(session.get("latest_event_type") or "")
    if (
        not str(persisted_active.get("run_id") or "").strip()
        and latest_type not in RESTART_ORPHAN_ACTIVITY_TYPES
        and not latest_type.startswith("codex_")
    ):
        return None
    records = tail_jsonl_file(
        events_path(session_id),
        limit=200,
        max_bytes=8 * 1024 * 1024,
    )
    active_run_id = str(persisted_active.get("run_id") or "").strip()
    open_runs: dict[str, dict[str, Any]] = (
        {active_run_id: persisted_active}
        if active_run_id
        else {}
    )
    closed_runs: set[str] = set()
    for event in records:
        event_type = str(event.get("type") or "")
        run_id = str(event.get("run_id") or "").strip()
        if not run_id:
            continue
        if event_type in RESTART_ORPHAN_TERMINAL_TYPES:
            open_runs.pop(run_id, None)
            closed_runs.add(run_id)
            continue
        if event_type == "turn_started":
            closed_runs.discard(run_id)
            open_runs[run_id] = dict(event)
            open_runs[run_id]["last_activity_seq"] = int(event.get("seq") or 0)
            continue
        if event_type in RESTART_ORPHAN_ACTIVITY_TYPES and run_id not in closed_runs:
            candidate = open_runs.setdefault(run_id, {"run_id": run_id})
            candidate["last_activity_seq"] = int(event.get("seq") or 0)
            for key in (
                "backend",
                "purpose",
                "provider_context_mode",
                "job_context_mode",
                "job_id",
                "job_title",
                "digest_job_id",
                "source_session_id",
                "target_session_id",
                "cross_chat_envelope_id",
            ):
                if candidate.get(key) is None and event.get(key) is not None:
                    candidate[key] = event.get(key)
    if not open_runs:
        return None
    return max(
        open_runs.values(),
        key=lambda event: int(event.get("last_activity_seq") or event.get("seq") or 0),
    )


async def recover_abandoned_turns_after_start(
    *,
    forced_restart_request_id: str | None = None,
) -> int:
    """Close runs orphaned by a crash without replaying accepted user input."""

    session_items = [
        (session_id, dict(session))
        for session_id, session in STORE.sessions.items()
    ]

    def scan_candidates() -> list[tuple[str, dict[str, Any]]]:
        found: list[tuple[str, dict[str, Any]]] = []
        for session_id, session in session_items:
            try:
                abandoned = abandoned_turn_after_restart(session_id, session)
            except Exception as exc:
                logger.warning(
                    "could not inspect abandoned turn session=%s error=%s",
                    session_id,
                    concise_error_message(exc),
                )
                continue
            if abandoned:
                found.append((session_id, abandoned))
        return found

    candidates = await asyncio.to_thread(scan_candidates)
    recovered = 0
    for session_id, abandoned in candidates:
        run_id = str(abandoned.get("run_id") or "").strip()
        if not run_id:
            continue
        payload: dict[str, Any] = {
            "run_id": run_id,
            "backend": abandoned.get("backend")
            or (STORE.sessions.get(session_id) or {}).get("backend"),
            "stopped": True,
            "recovered_after_restart": True,
            "message": (
                "This agent turn was interrupted when HyadexServer restarted. "
                "Its accepted user message was not replayed."
            ),
        }
        if forced_restart_request_id:
            payload.update({
                "forced_restart": True,
                "restart_request_id": forced_restart_request_id,
                "message": (
                    "This agent turn was forcibly interrupted when HyadexServer "
                    "restarted. Its accepted user message was not replayed."
                ),
            })
        for key in (
            "purpose",
            "provider_context_mode",
            "job_context_mode",
            "job_id",
            "job_title",
            "digest_job_id",
            "source_session_id",
            "target_session_id",
            "cross_chat_envelope_id",
            "secure_peer_envelope_id",
        ):
            if abandoned.get(key) is not None:
                payload[key] = abandoned[key]
        try:
            await append_event(session_id, "turn_stopped", payload)
        except Exception as exc:
            logger.warning(
                "could not close abandoned turn session=%s run=%s error=%s",
                session_id,
                run_id,
                concise_error_message(exc),
            )
        else:
            recovered += 1
    if recovered:
        logger.warning(
            "closed abandoned agent turns after restart count=%d",
            recovered,
        )
    return recovered


def tail_jsonl_file(path: Path, limit: int = 40, max_bytes: int = 2 * 1024 * 1024) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    line_count = max(1, min(int(limit or 40), 200))
    size = path.stat().st_size
    byte_count = max(4096, min(int(max_bytes or 2 * 1024 * 1024), 8 * 1024 * 1024))
    with path.open("rb") as f:
        if size > byte_count:
            f.seek(size - byte_count)
        data = f.read(byte_count)
    records: list[dict[str, Any]] = []
    for line in data.decode("utf-8", "replace").splitlines()[-line_count:]:
        with suppress(Exception):
            parsed = json.loads(line)
            if isinstance(parsed, dict):
                records.append(parsed)
    return records


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


async def stop_turn(
    session_id: str,
    *,
    expected_run_id: str | None = None,
    emit_event: bool = True,
    schedule_queue: bool = True,
    require_provider_turn_ready: bool = False,
    cascade_codex_subagents: bool = True,
    cascade_claude_subagents: bool = True,
    hard_terminalize_on_timeout: bool = True,
    pause_queued_turns_on_stop: bool = True,
    preserve_active_goal: bool = False,
    _admission_ready: asyncio.Event | None = None,
) -> dict[str, Any]:
    deferred = False
    native_interrupt_reserved = False
    native_control_interrupt_reserved = False
    native_claude_interrupt_reserved = False
    transition_ready: asyncio.Event | None = None
    owned_tasks: list[asyncio.Task[Any]] = []
    stopping_run_id: str | None = None
    stopping_purpose = ""
    stopping_control_reservation_id = ""
    stopping_control_thread_id = ""
    stopping_admission_id = ""
    async with RUNS.active_lock:
        active = RUNS.active.get(session_id)
        busy = session_id in RUNS.busy
        current_turn = RUNS.current_turns.get(session_id) or {}
        expected_run_id = str(expected_run_id or "").strip() or None
        if expected_run_id is not None:
            active_run_id = str((active or {}).get("run_id") or "")
            current_run_id = str(current_turn.get("run_id") or "")
            if (
                not busy
                or expected_run_id not in {active_run_id, current_run_id}
                or any(
                    owner_id not in {"", expected_run_id}
                    for owner_id in (active_run_id, current_run_id)
                )
            ):
                if _admission_ready is not None:
                    _admission_ready.set()
                return {
                    "ok": True,
                    "stopped": False,
                    "pending": False,
                    "superseded": True,
                    "message": "The requested run no longer owns this chat.",
                }
        if (STORE.sessions.get(session_id) or {}).get("gemini_cleanup_pending_run_id"):
            if _admission_ready is not None:
                _admission_ready.set()
            return {"ok": False, "stopped": False, "pending": True,
                    "message": "Retired provider cleanup was not confirmed; verify the old runtime on the server."}
        if preserve_active_goal and codex_goal_followup_requires_native(
            STORE.sessions.get(session_id) or {}, active or {}, current_turn,
        ):
            # Force Send may have passed its non-goal probe before the
            # provider created a goal. Reject at the final, synchronous Stop
            # admission boundary, before closing authority or marking a run
            # stopped. Its caller restores the selected row in place.
            if _admission_ready is not None:
                _admission_ready.set()
            raise HTTPException(
                status_code=409,
                detail=force_send_conflict_detail(
                    session_id, "",
                    guard="active_goal_requires_native_steer",
                    message="A Codex goal became active before Force Send could stop the turn. The message remains queued; the goal was not paused.",
                    action="Retry the follow-up through the active goal's native steering lane.",
                    retryable=True,
                ),
            )
        if active:
            if require_provider_turn_ready and not (
                active.get("transport") == CODEX_TRANSPORT_APP_SERVER
                and active.get("codex_child_continuation_waiting") is True
                and isinstance(active.get("codex_child_continuation_stop"), asyncio.Event)
            ) and (
                not active.get("provider_turn_ready")
                or (
                    active.get("transport") == CODEX_TRANSPORT_APP_SERVER
                    and not active.get("provider_session_id")
                )
            ):
                deferred = True
            else:
                active["stop_requested"] = True
                child_stop = active.get("codex_child_continuation_stop")
                if isinstance(child_stop, asyncio.Event):
                    child_stop.set()
                if active.get("transport") == CLAUDE_TRANSPORT_AGENT_SDK:
                    active["claude_permissions_open"] = False
                if active.get("run_id"):
                    stopping_run_id = str(active["run_id"])
                    RUNS.stopped_runs.add(stopping_run_id)
                transition_value = active.get("logical_transition_ready")
                if isinstance(transition_value, asyncio.Event):
                    transition_ready = transition_value
                native_turn = active.get("codex_app_server_turn")
                if (
                    active.get("transport") == CODEX_TRANSPORT_APP_SERVER
                    and native_turn is not None
                    and getattr(native_turn, "turn_id", "")
                    and getattr(native_turn, "_completed", False) is not True
                    and not active.get("native_interrupt_sent")
                ):
                    active["native_interrupt_sent"] = True
                    native_interrupt_reserved = True
                elif (
                    active.get("transport") == CODEX_TRANSPORT_APP_SERVER
                    and active.get("codex_native_operation")
                    and active.get("provider_thread_id")
                    and active.get("provider_turn_id")
                    and not active.get("native_interrupt_sent")
                ):
                    active["native_interrupt_sent"] = True
                    native_control_interrupt_reserved = True
                elif (
                    active.get("transport") == CLAUDE_TRANSPORT_AGENT_SDK
                    and active.get("claude_sdk_run") is not None
                    and not active.get("native_interrupt_sent")
                ):
                    active["native_interrupt_sent"] = True
                    native_claude_interrupt_reserved = True
        elif busy:
            if require_provider_turn_ready:
                deferred = True
            else:
                stopping_run_id = str(
                    current_turn.get("run_id") or ""
                ) or None
                if expected_run_id is not None and stopping_run_id:
                    # A targeted cancellation is run-scoped. A session-wide
                    # startup marker could otherwise stop a successor if the
                    # expected launch loses its slot before binding ACTIVE.
                    RUNS.stopped_runs.add(stopping_run_id)
                else:
                    RUNS.stop_requests.add(session_id)
        # Identity of the reservation this Stop observed. A Stop that outlives
        # its deadline runs detached; it must never act on a successor turn
        # admitted after the fence was released.
        stopping_admission_id = str(
            current_turn.get("_server_restart_admission_id") or ""
        )
        stopping_control_reservation_id = str(
            (active or {}).get("codex_control_reservation_id")
            or current_turn.get("codex_control_reservation_id")
            or ""
        ).strip()
        stopping_control_thread_id = str(
            (active or {}).get("provider_thread_id")
            or (active or {}).get("provider_session_id")
            or ""
        ).strip()
        stopping_purpose = str(
            current_turn.get("purpose")
            or (RUNS.run_metadata.get(stopping_run_id or "") or {}).get("purpose")
            or ""
        )
        current_task = asyncio.current_task()
        owned_tasks = [
            task
            for task in (
                *tuple(RUNS.turn_tasks.get(session_id) or ()),
                *tuple(
                    task
                    for (task_session_id, _operation_id), task in
                    CODEX_NATIVE_ACTION_TASKS.items()
                    if task_session_id == session_id
                ),
                active.get("owner_task") if active else None,
            )
            if (
                isinstance(task, asyncio.Task)
                and task is not current_task
                and not task.done()
            )
        ]
    # The observed owner is now fenced synchronously under ACTIVE_LOCK.  The
    # explicit-operation entry blocks replacement admission, so release the
    # broad lifecycle lock before any durable queue IO or provider cleanup.
    if _admission_ready is not None:
        _admission_ready.set()
    subagent_stop = empty_subagent_stop_result()
    session = STORE.sessions.get(session_id) or {}
    codex_manager = existing_codex_app_server_manager(session)
    pause_queued_successors = bool(
        pause_queued_turns_on_stop
        and stopping_purpose != "scheduled_job"
    )

    def schedule_unpaused_queue_after_release(released: bool = True) -> None:
        if released and schedule_queue and not pause_queued_successors:
            schedule_next_queued_turn(session_id)

    async def release_stopping_slot(
        expected_run_id: str | None,
        *,
        allow_prebind: bool = False,
    ) -> bool:
        """Release only the owner Stop observed at admission time."""

        if stopping_control_reservation_id:
            return await release_codex_control_slot(
                session_id,
                expected_thread_id=stopping_control_thread_id,
                expected_reservation_id=stopping_control_reservation_id,
                allow_prebind=allow_prebind,
            )
        return await release_turn_slot(
            session_id,
            expected_run_id=expected_run_id,
        )

    async def settle_stopped_claude_subagents() -> None:
        """Fence surviving Claude children only after this parent is idle."""

        nonlocal subagent_stop
        if (
            not cascade_claude_subagents
            or str(session.get("backend") or DEFAULT_BACKEND) != BACKEND_CLAUDE
        ):
            return
        owns_maintenance = False
        async with RUNS.active_lock:
            session_idle = (
                session_id not in RUNS.active
                and session_id not in RUNS.busy
            )
            if session_idle:
                already_reserved = session_id in RUNS.maintenance
                owns_maintenance = (
                    not already_reserved
                    or session_id in CLAUDE_STOP_FENCE_SESSIONS
                )
                if owns_maintenance:
                    RUNS.maintenance.add(session_id)
        if not session_idle or not owns_maintenance:
            return
        # Cancellation anywhere after this point must leave update admission
        # excluded unless the durable child-stop marker confirms completion.
        subagent_stop["fence_committed"] = False
        retry_required = False
        try:
            subagent_stop = await stop_idle_claude_background_subagents_bounded(
                session_id,
                emit_event=emit_event,
            )
        finally:
            fence_committed = bool(subagent_stop.get("fence_committed", False))
            async with RUNS.active_lock:
                if fence_committed:
                    CLAUDE_STOP_FENCE_SESSIONS.discard(session_id)
                    RUNS.maintenance.discard(session_id)
                    discard_unused_claude_stop_fence_attempt_lock(session_id)
                else:
                    CLAUDE_STOP_FENCE_SESSIONS.add(session_id)
                    RUNS.maintenance.add(session_id)
                    retry_required = True
            if retry_required:
                schedule_claude_stop_fence_retry(session_id)

    if deferred:
        return {
            "ok": True,
            "stopped": False,
            "deferred": True,
            "message": "The provider had not accepted the current turn yet, so it was left running.",
        }
    if pause_queued_successors:
        await pause_queued_turns_after_explicit_stop(session_id)
    if not deferred and stopping_run_id:
        # ACTIVE.stop_requested / STOPPED_RUNS already closes provider helper
        # authority synchronously. Remove the capability record outside the
        # broad lifecycle-lock admission edge.
        await revoke_provider_authority(stopping_run_id)
    root_thread_id = str(
        (active or {}).get("provider_thread_id")
        or (active or {}).get("provider_session_id")
        or session_provider_id(session)
        or ""
    ).strip()
    if (
        cascade_codex_subagents
        and not active
        and str(session.get("backend") or "") == BACKEND_CODEX
        and root_thread_id
        and codex_manager is not None
    ):
        subagent_stop = await stop_codex_descendant_subagents(
            session_id,
            root_thread_id,
            manager=codex_manager,
        )
    if not active and not busy:
        await settle_stopped_claude_subagents()
    # ACTIVE is now fenced with stop_requested under the same lock consulted
    # by provider permission callbacks. Cancel existing requests only after
    # that fence, so no callback can register in the old cancel-before-stop
    # gap and survive this Stop operation.
    await cancel_codex_interactions(session_id, resolution="turn_stopped")
    await cancel_claude_interactions(session_id, resolution="turn_stopped")
    if not active:
        if busy:
            deadline = (
                asyncio.get_running_loop().time()
                + STOP_CONFIRM_TIMEOUT_SECONDS
            )
            current_turn: dict[str, Any] = {}
            pending = set(owned_tasks)
            while True:
                current_task = asyncio.current_task()
                async with RUNS.active_lock:
                    bound_active = RUNS.active.get(session_id)
                    still_busy = session_id in RUNS.busy
                    current_turn = dict(RUNS.current_turns.get(session_id) or {})
                    reservation_replaced = bool(
                        still_busy
                        and stopping_admission_id
                        and str(current_turn.get("_server_restart_admission_id") or "")
                        != stopping_admission_id
                    )
                    pending = {
                        task
                        for task in RUNS.turn_tasks.get(session_id) or ()
                        if task is not current_task and not task.done()
                    } | {
                        task
                        for (task_session_id, _operation_id), task in
                        CODEX_NATIVE_ACTION_TASKS.items()
                        if (
                            task_session_id == session_id
                            and task is not current_task
                            and not task.done()
                        )
                    }
                if reservation_replaced:
                    # A successor turn owns the chat: the reservation this Stop
                    # observed is gone. Leave the new turn untouched.
                    logger.info(
                        "explicit Stop superseded by a new turn session=%s",
                        session_id,
                    )
                    return {
                        "ok": True,
                        "stopped": True,
                        "pending": False,
                        "superseded": True,
                        "subagents": subagent_stop,
                    }
                if bound_active:
                    # The runner bound while Stop was waiting. Re-enter through
                    # the normal provider-interrupt path instead of recording a
                    # false terminal event.
                    return await stop_turn(
                        session_id,
                        expected_run_id=expected_run_id,
                        emit_event=emit_event,
                        schedule_queue=schedule_queue,
                        require_provider_turn_ready=require_provider_turn_ready,
                        cascade_codex_subagents=cascade_codex_subagents,
                        cascade_claude_subagents=cascade_claude_subagents,
                        hard_terminalize_on_timeout=hard_terminalize_on_timeout,
                        pause_queued_turns_on_stop=False,
                    )
                if not still_busy:
                    await settle_stopped_claude_subagents()
                    goal_result = await settle_idle_codex_goal_for_stop(
                        session_id
                    )
                    return {
                        "ok": True,
                        "stopped": True,
                        "pending": False,
                        "subagents": subagent_stop,
                        **goal_result,
                    }
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    break
                if pending:
                    await asyncio.wait(
                        pending,
                        timeout=remaining,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                else:
                    await asyncio.sleep(min(0.01, remaining))

            if pending:
                if (
                    hard_terminalize_on_timeout
                    and str(session.get("backend") or "") in {BACKEND_CODEX, BACKEND_CLAUDE}
                ):
                    backend = str(session.get("backend") or "")
                    run_id = str(current_turn.get("run_id") or "") or None
                    if run_id:
                        RUNS.stopped_runs.add(run_id)
                    for task in pending:
                        task.cancel()
                    if pending:
                        await asyncio.wait(
                            pending,
                            timeout=min(1.0, STOP_CONFIRM_TIMEOUT_SECONDS),
                        )
                    if backend == BACKEND_CLAUDE:
                        await evict_claude_sdk_chat(
                            session_id,
                            force=True,
                        )
                    released = await release_stopping_slot(
                        run_id,
                        allow_prebind=True,
                    )
                    if not released and stopping_control_reservation_id:
                        # The cancelled acquisition may have released its own
                        # exact reservation while Stop was joining the task.
                        # Count that as this Stop's release only if the chat is
                        # still idle; a promoted replacement must remain owned.
                        async with RUNS.active_lock:
                            released = not RUNS.holds_turn(session_id)
                    await settle_stopped_claude_subagents()
                    if emit_event and released:
                        await append_event(session_id, "turn_stopped", {
                            "run_id": run_id,
                            "backend": backend,
                            "message": (
                                "Stopped while the agent process was "
                                "still starting."
                            ),
                            "hard_stop": True,
                        })
                    RUNS.run_metadata.pop(run_id, None) if run_id else None
                    if run_id and all(task.done() for task in pending):
                        RUNS.stopped_runs.discard(run_id)
                    schedule_unpaused_queue_after_release(released)
                    goal_result = (
                        await settle_idle_codex_goal_for_stop(session_id)
                        if backend == BACKEND_CODEX
                        else {}
                    )
                    return {
                        "ok": True,
                        "stopped": True,
                        "pending": False,
                        "hard_stop": True,
                        "subagents": subagent_stop,
                        **goal_result,
                    }
                # Keep the deferred STOP_REQUESTS marker. A legitimate slow
                # launch owns this reservation and will reconcile the stop as
                # soon as it binds; do not cancel it mid-pin or mid-handshake.
                return {
                    "ok": True,
                    "stopped": False,
                    "pending": True,
                    "message": (
                        "Stop is waiting for the agent process to finish "
                        "starting."
                    ),
                }
            # No request or runner owns this BUSY reservation after the grace
            # period, so it is an orphan and can be safely released.
            run_id = str(current_turn.get("run_id") or "") or None
            released = await release_stopping_slot(
                run_id,
                allow_prebind=True,
            )
            await settle_stopped_claude_subagents()
            if run_id:
                RUNS.run_metadata.pop(run_id, None)
            if emit_event and released:
                await append_event(session_id, "turn_stopped", {
                    "run_id": run_id,
                    "message": "Stopped before the agent process was ready.",
                })
            schedule_unpaused_queue_after_release(released)
            goal_result = await settle_idle_codex_goal_for_stop(session_id)
            return {
                "ok": True,
                "stopped": True,
                "pending": False,
                "subagents": subagent_stop,
                **goal_result,
            }
        schedule_unpaused_queue_after_release()
        # The requested terminal state is already satisfied. Returning
        # stopped=True also lets clients clear a stale local running marker.
        goal_result = await settle_idle_codex_goal_for_stop(session_id)
        return {
            "ok": True,
            "stopped": True,
            "pending": False,
            "subagents": subagent_stop,
            **goal_result,
        }
    proc = active.get("proc") if active else None
    native_turn = active.get("codex_app_server_turn") if active else None
    active_run_id = str(active.get("run_id") or "") or None
    goal_pause_ok, goal_paused, goal_pause_error = (
        await pause_active_codex_goal_for_stop(session_id, active)
    )

    async def terminal_goal_pause_failure_result(
        native_interrupt: bool,
    ) -> dict[str, Any]:
        thread_id = str(
            active.get("provider_thread_id")
            or active.get("provider_session_id")
            or session_provider_id(STORE.sessions.get(session_id) or {})
            or ""
        ).strip()
        fenced = await quarantine_codex_goal_thread(
            session_id,
            thread_id,
            run_id=str(active.get("run_id") or "") or None,
            reason=goal_pause_error
            or "Codex goal reconciliation failed while stopping the turn.",
        )
        return {
            "ok": True,
            "stopped": True,
            "pending": False,
            "native_interrupt": native_interrupt,
            "goal_paused": False,
            "goal_fenced": fenced,
            "subagents": subagent_stop,
            "message": (
                "Turn stopped. The stale Codex goal thread was fenced and "
                "will not be reused."
            ),
        }

    async def hard_terminalize_active_stop(
        *,
        native_interrupt: bool,
        reason: str,
        tasks: set[asyncio.Task[Any]],
    ) -> dict[str, Any]:
        """Bound explicit Stop even if a managed provider consumer is stale."""

        run_id = str(active.get("run_id") or "") or None
        if run_id:
            RUNS.stopped_runs.add(run_id)
        current_task = asyncio.current_task()
        owned = {
            task
            for task in tasks
            if task is not current_task and not task.done()
        }
        for task in owned:
            task.cancel()
        if owned:
            await asyncio.wait(
                owned,
                timeout=min(1.0, STOP_CONFIRM_TIMEOUT_SECONDS),
            )
        is_claude_sdk = (
            active.get("transport") == CLAUDE_TRANSPORT_AGENT_SDK
        )
        is_codex_app_server = bool(
            active.get("backend") == BACKEND_CODEX
            and active.get("transport") == CODEX_TRANSPORT_APP_SERVER
        )
        # A natural terminal notification may have won the race with local
        # cancellation. In that case its runner already emitted the terminal
        # row and released the reservation, so do not manufacture a duplicate.
        async with RUNS.active_lock:
            still_owned = (
                RUNS.active.get(session_id) is active
                and session_id in RUNS.busy
            )
        if not still_owned:
            if run_id and all(task.done() for task in owned):
                RUNS.stopped_runs.discard(run_id)
            return {
                "ok": True,
                "stopped": True,
                "pending": False,
                "native_interrupt": native_interrupt,
                "goal_paused": goal_paused,
                "subagents": subagent_stop,
            }

        if is_claude_sdk:
            # Scope force teardown to the exact supervisor generation. A
            # delayed Stop must not evict a replacement that bound while its
            # old runner was unwinding.
            ownership_token = str(
                active.get("claude_sdk_owner_token") or ""
            )
            if ownership_token:
                await evict_claude_sdk_chat(
                    session_id,
                    force=True,
                    ownership_token=ownership_token,
                )

        fenced = False
        if is_codex_app_server:
            thread_id = str(
                active.get("provider_thread_id")
                or active.get("provider_session_id")
                or session_provider_id(STORE.sessions.get(session_id) or {})
                or ""
            ).strip()
            fenced = await quarantine_codex_goal_thread(
                session_id,
                thread_id,
                run_id=run_id,
                reason=reason,
            )
        released = await release_stopping_slot(
            run_id,
        )
        await settle_stopped_claude_subagents()
        active_run = (STORE.sessions.get(session_id) or {}).get("active_run")
        should_emit_terminal = bool(
            emit_event
            and released
            and (
                not run_id
                or (
                    isinstance(active_run, dict)
                    and str(active_run.get("run_id") or "") == run_id
                )
            )
        )
        if should_emit_terminal:
            await append_event(session_id, "turn_stopped", {
                "run_id": run_id,
                "backend": active.get("backend"),
                "native_interrupt": native_interrupt,
                "goal_fenced": fenced,
                "hard_stop": True,
                "message": (
                    "Stopped after the provider did not acknowledge interruption in time."
                ),
            })
        if run_id:
            RUNS.run_metadata.pop(run_id, None)
            if all(task.done() for task in owned):
                RUNS.stopped_runs.discard(run_id)
        schedule_unpaused_queue_after_release(released)
        return {
            "ok": True,
            "stopped": True,
            "pending": False,
            "hard_stop": True,
            "native_interrupt": native_interrupt,
            "goal_paused": goal_paused,
            "goal_fenced": fenced,
            "subagents": subagent_stop,
            "message": (
                "Turn stopped. The unresponsive Claude runner was retired."
                if active.get("backend") == BACKEND_CLAUDE
                else (
                    "Turn stopped. The stale Codex thread was fenced and will "
                    "not be reused."
                    if is_codex_app_server
                    else "Turn stopped. The unresponsive provider runner was retired."
                )
            ),
        }

    native_interrupt = False
    if native_interrupt_reserved and native_turn is not None:
        try:
            await native_turn.interrupt()
            native_interrupt = True
        except Exception as exc:
            async with RUNS.active_lock:
                current = RUNS.active.get(session_id)
                if current is active:
                    current["native_interrupt_sent"] = False
            logger.warning(
                "Codex app-server interrupt failed session=%s error=%s",
                session_id,
                exc,
            )
    elif native_control_interrupt_reserved and codex_manager is not None:
        try:
            await codex_manager.request(
                "turn/interrupt",
                {
                    "threadId": str(active.get("provider_thread_id") or ""),
                    "turnId": str(active.get("provider_turn_id") or ""),
                },
            )
            native_interrupt = True
        except Exception as exc:
            async with RUNS.active_lock:
                current = RUNS.active.get(session_id)
                if current is active:
                    current["native_interrupt_sent"] = False
            logger.warning(
                "Codex native control interrupt failed session=%s error=%s",
                session_id,
                exc,
            )
    elif native_claude_interrupt_reserved:
        claude_sdk_run = active.get("claude_sdk_run")
        try:
            await interrupt_claude_sdk_run_bounded(
                claude_sdk_run,
            )
            native_interrupt = True
        except Exception as exc:
            async with RUNS.active_lock:
                current = RUNS.active.get(session_id)
                if current is active:
                    current["native_interrupt_sent"] = False
            logger.warning(
                "Claude Agent SDK interrupt failed session=%s error=%s",
                session_id,
                exc,
            )
    if (
        cascade_codex_subagents
        and str(session.get("backend") or "") == BACKEND_CODEX
        and active.get("transport") == CODEX_TRANSPORT_APP_SERVER
        and root_thread_id
        and codex_manager is not None
    ):
        subagent_stop = await stop_codex_descendant_subagents(
            session_id,
            root_thread_id,
            manager=codex_manager,
        )
    # The app-server process may serve multiple Codex chats and must never be
    # terminated as a per-chat Stop action.
    if proc and active.get("transport") != CODEX_TRANSPORT_APP_SERVER:
        await terminate_process_tree(proc)
    if transition_ready is not None:
        try:
            await asyncio.wait_for(
                asyncio.shield(transition_ready.wait()),
                timeout=STOP_CONFIRM_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            async with RUNS.active_lock:
                current = RUNS.active.get(session_id)
                if current is active:
                    current["native_interrupt_sent"] = False
            if (
                hard_terminalize_on_timeout
                and active.get("transport")
                in HARD_TERMINALIZABLE_TURN_TRANSPORTS
            ):
                return await hard_terminalize_active_stop(
                    native_interrupt=native_interrupt,
                    reason=(
                        goal_pause_error
                        or "Provider steering transition did not settle after Stop."
                    ),
                    tasks=set(owned_tasks),
                )
            return {
                "ok": True,
                "stopped": False,
                "pending": True,
                "native_interrupt": native_interrupt,
                "goal_paused": goal_paused,
                "subagents": subagent_stop,
                "message": (
                    goal_pause_error
                    or "Stop was sent; waiting for the steering transition to settle."
                ),
            }
    deadline = (
        asyncio.get_running_loop().time()
        + STOP_CONFIRM_TIMEOUT_SECONDS
    )
    dynamic_tasks = set(owned_tasks)
    while True:
        current_task = asyncio.current_task()
        async with RUNS.active_lock:
            current = RUNS.active.get(session_id)
            still_busy = session_id in RUNS.busy
            dynamic_tasks = {
                task
                for task in RUNS.turn_tasks.get(session_id) or ()
                if task is not current_task and not task.done()
            } | {
                task
                for (task_session_id, _operation_id), task in
                CODEX_NATIVE_ACTION_TASKS.items()
                if (
                    task_session_id == session_id
                    and task is not current_task
                    and not task.done()
                )
            }
            owner_task = active.get("owner_task")
            if (
                isinstance(owner_task, asyncio.Task)
                and owner_task is not current_task
                and not owner_task.done()
            ):
                dynamic_tasks.add(owner_task)
        if current is not active:
            if not goal_pause_ok:
                return await terminal_goal_pause_failure_result(native_interrupt)
            await settle_stopped_claude_subagents()
            return {
                "ok": True,
                "stopped": True,
                "pending": False,
                "native_interrupt": native_interrupt,
                "goal_paused": goal_paused,
                "subagents": subagent_stop,
            }
        if not still_busy:
            if not goal_pause_ok:
                released = await release_stopping_slot(
                    active_run_id,
                )
                schedule_unpaused_queue_after_release(released)
                return await terminal_goal_pause_failure_result(native_interrupt)
            released = await release_stopping_slot(
                active_run_id,
            )
            await settle_stopped_claude_subagents()
            schedule_unpaused_queue_after_release(released)
            return {
                "ok": True,
                "stopped": True,
                "pending": False,
                "native_interrupt": native_interrupt,
                "goal_paused": goal_paused,
                "subagents": subagent_stop,
            }
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            break
        if dynamic_tasks:
            await asyncio.wait(
                dynamic_tasks,
                timeout=remaining,
                return_when=asyncio.FIRST_COMPLETED,
            )
        else:
            await asyncio.sleep(min(0.01, remaining))

    if not dynamic_tasks:
        if not goal_pause_ok:
            released = await release_stopping_slot(
                active_run_id,
            )
            if emit_event and released:
                await append_event(session_id, "turn_stopped", {
                    "run_id": active.get("run_id"),
                    "backend": active.get("backend"),
                    "native_interrupt": native_interrupt,
                    "goal_fenced": True,
                })
            schedule_unpaused_queue_after_release(released)
            return await terminal_goal_pause_failure_result(native_interrupt)
        # No request or runner still owns this ACTIVE record. It is stale and
        # safe to clear after the bounded confirmation window.
        released = await release_stopping_slot(
            active_run_id,
        )
        await settle_stopped_claude_subagents()
        if emit_event and released:
            await append_event(session_id, "turn_stopped", {
                "run_id": active.get("run_id"),
                "backend": active.get("backend"),
                "native_interrupt": native_interrupt,
            })
        schedule_unpaused_queue_after_release(released)
        return {
            "ok": True,
            "stopped": True,
            "pending": False,
            "native_interrupt": native_interrupt,
            "goal_paused": goal_paused,
            "subagents": subagent_stop,
        }

    # A provider interrupt is an acknowledgement, not terminal state. Leave
    # the chat active and permit a later Stop click to retry the interrupt
    # instead of rendering a false "Turn Stopped" row.
    async with RUNS.active_lock:
        current = RUNS.active.get(session_id)
        if current is active:
            current["native_interrupt_sent"] = False
    if (
        hard_terminalize_on_timeout
        and active.get("transport") in HARD_TERMINALIZABLE_TURN_TRANSPORTS
    ):
        return await hard_terminalize_active_stop(
            native_interrupt=native_interrupt,
            reason=(
                goal_pause_error
                or "The provider did not confirm the requested Stop before timeout."
            ),
            tasks=dynamic_tasks,
        )
    return {
        "ok": True,
        "stopped": False,
        "pending": True,
        "native_interrupt": native_interrupt,
        "goal_paused": goal_paused,
        "message": (
            goal_pause_error
            or "Stop was sent; waiting for the provider to finish stopping."
        ),
    }
