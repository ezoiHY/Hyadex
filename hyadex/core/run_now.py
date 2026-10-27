"""Run now: promoting a queued turn ahead of the running one."""
from __future__ import annotations

import asyncio
from collections import deque
from contextlib import suppress
import logging.handlers
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
    HARD_TERMINALIZABLE_TURN_TRANSPORTS,
    RETIRED_TURN_PURPOSES,
    STOP_CONFIRM_TIMEOUT_SECONDS,
)
from hyadex.core.history_sync import (
    append_durable_event,
    fence_native_steer_delivery,
    requeue_native_steer_after_safe_rejection,
)
from hyadex.core.processes import terminate_process_tree
from hyadex.core.queue import (
    codex_goal_followup_requires_native,
    NonNativeForceSendRequiresLifecycleLock,
    pause_queued_turns_after_explicit_stop,
)
from hyadex.core.queue_admission import wait_for_queue_recovery_admission
from hyadex.core.turn_start import (
    reconcile_idle_queue_session,
    release_turn_slot,
    schedule_next_queued_turn,
    schedule_steered_turn_slot_waiter,
)
from hyadex.core.runs import RUNS
from hyadex.core.session_codex import existing_codex_app_server_manager
from hyadex.core.lifecycle_admission import server_lifecycle_admission_blocker
from hyadex.core.sessions import (
    append_event,
    CLAUDE_STOP_FENCE_SESSIONS,
    ensure_session_not_deleting,
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
from hyadex.core.stop_turn import stop_turn
from hyadex.features.usage import join_task_despite_caller_cancellation
from hyadex.providers.authority import NativeSteerHandoffError
from hyadex.providers.claude.background import stop_idle_claude_background_subagents_bounded
from hyadex.providers.claude.history import session_provider_id
from hyadex.providers.claude.interactions import cancel_claude_interactions
from hyadex.providers.claude.runtime import (
    interrupt_claude_sdk_run_bounded,
    queued_claude_runtime_matches_active,
    stop_cleanup_in_progress,
)
from hyadex.providers.claude.subagents import (
    discard_unused_claude_stop_fence_attempt_lock,
    evict_claude_sdk_chat,
)
from hyadex.providers.codex.goals import (
    pause_active_codex_goal_for_stop,
    quarantine_codex_goal_thread,
    settle_idle_codex_goal_for_stop,
)
from hyadex.providers.codex.interactions import cancel_codex_interactions
from hyadex.providers.codex.managers import CODEX_NATIVE_ACTION_TASKS, release_codex_control_slot
from hyadex.providers.codex.runtime import queued_codex_runtime_matches_active
from hyadex.providers.codex.subagents import stop_codex_descendant_subagents
from hyadex.providers.registry import normalize_steering_lineage, queued_turn_uses_retired_routing
from hyadex.providers.subagents import empty_subagent_stop_result

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


