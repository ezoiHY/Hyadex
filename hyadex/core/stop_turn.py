"""Stopping a chat's running turn: interrupting the provider, fencing the run and settling its queue."""
from __future__ import annotations

import asyncio
import logging.handlers
from typing import Any

from fastapi import HTTPException

from hyadex.core.config import (
    BACKEND_CLAUDE,
    BACKEND_CODEX,
    CLAUDE_TRANSPORT_AGENT_SDK,
    CODEX_TRANSPORT_APP_SERVER,
    DEFAULT_BACKEND,
    HARD_TERMINALIZABLE_TURN_TRANSPORTS,
    STOP_CONFIRM_TIMEOUT_SECONDS,
)
from hyadex.core.processes import terminate_process_tree
from hyadex.core.queue import (
    codex_goal_followup_requires_native,
    pause_queued_turns_after_explicit_stop,
)
from hyadex.core.turn_start import release_turn_slot, schedule_next_queued_turn
from hyadex.core.runs import RUNS
from hyadex.core.session_codex import existing_codex_app_server_manager
from hyadex.core.sessions import (
    append_event,
    CLAUDE_STOP_FENCE_SESSIONS,
    revoke_provider_authority,
    STORE,
)
from hyadex.core.steering import force_send_conflict_detail
from hyadex.core.stop_fence import schedule_claude_stop_fence_retry
from hyadex.providers.claude.background import stop_idle_claude_background_subagents_bounded
from hyadex.providers.claude.history import session_provider_id
from hyadex.providers.claude.interactions import cancel_claude_interactions
from hyadex.providers.claude.runtime import interrupt_claude_sdk_run_bounded
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
from hyadex.providers.codex.subagents import stop_codex_descendant_subagents
from hyadex.providers.subagents import empty_subagent_stop_result

logger = logging.getLogger("hyadex-server")


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
