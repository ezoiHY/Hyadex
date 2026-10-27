"""Codex goal and compaction controls started from the API: set, clear, resume and compact."""
from __future__ import annotations

import asyncio
import logging.handlers
from typing import Any
import uuid

from fastapi import HTTPException

from hyadex.core.config import (
    BACKEND_CODEX,
    CODEX_GOAL_CONTROL_TIMEOUT_SECONDS,
    CODEX_GOALS_ENABLED,
)
from hyadex.core.paths import now_iso
from hyadex.core.queue import request_fields_set
from hyadex.core.queue_admission import wait_for_queue_recovery_admission
from hyadex.core.runs import RUNS
from hyadex.core.server_logging import concise_error_message
from hyadex.core.sessions import append_event, STORE
from hyadex.core.session_settings import codex_runtime_settings
from hyadex.providers.claude.history import session_provider_id
from hyadex.providers.codex.app_server_manager import CodexAppServerManager
from hyadex.providers.codex.commands import acquire_codex_control_thread
from hyadex.providers.codex.goals import (
    CodexGoalRequest,
    quarantine_codex_goal_thread,
    stop_codex_goal_resume,
    validate_codex_goal_resume_budget,
)
from hyadex.providers.codex.managers import codex_control_http_error, register_codex_native_action
from hyadex.providers.codex.notifications import current_codex_token_usage_snapshot
from hyadex.providers.codex.runtime import turn_start_blocker
from hyadex.providers.codex.threads import is_codex_resume_failure
from hyadex.providers.codex.native_turns import (
    consume_codex_native_turn,
    release_codex_control_thread,
)

logger = logging.getLogger("hyadex-server")


def start_codex_goal_resume_consumer(
    session_id: str, operation_id: str, manager: CodexAppServerManager,
    thread_id: str, reservation_id: str, subscription: Any,
) -> asyncio.Task[None]:
    entered = False

    async def consume() -> None:
        nonlocal entered
        entered = True
        await consume_codex_native_turn(
            session_id, operation_id, "goal_resume", manager,
            thread_id, reservation_id, subscription,
        )

    task = asyncio.create_task(consume())
    register_codex_native_action(session_id, operation_id, task)

    def reconcile_unstarted_cancel(completed: asyncio.Task[None]) -> None:
        if not entered and completed.cancelled():
            # Cancelling a task before its first step does not execute its
            # finally block. Keep cleanup tracked under the same operation.
            cleanup = asyncio.create_task(consume_codex_native_turn(
                session_id, operation_id, "goal_resume", manager,
                thread_id, reservation_id, subscription,
                interrupted_before_start=True,
            ))
            register_codex_native_action(session_id, operation_id, cleanup)

    task.add_done_callback(reconcile_unstarted_cancel)
    return task


async def _put_codex_goal_locked(
    session_id: str,
    req: CodexGoalRequest,
) -> dict[str, Any]:
    if not CODEX_GOALS_ENABLED:
        raise HTTPException(
            status_code=409,
            detail=(
                "Persistent Codex goals are disabled for this server. "
                "Enable them in server settings before creating or updating a goal."
            ),
        )
    fields_set = request_fields_set(req)
    if not fields_set:
        raise HTTPException(status_code=400, detail="provide at least one goal field")
    if req.status == "active":
        if (STORE.sessions.get(session_id) or {}).get("archived"):
            raise HTTPException(status_code=409, detail="archived chats cannot resume goals")
        await wait_for_queue_recovery_admission(session_id)
    manager, thread_id, control_session = await acquire_codex_control_thread(
        session_id,
        reserve_session=req.status == "active",
        allow_active_goal_mutation=True,
    )
    reservation_id = str(control_session.get("_codex_control_reservation_id") or "")
    subscription = None
    operation_id = ""
    consumer_owns_reservation = False
    goal_activation_attempted = False
    resume_started_published = False
    try:
        native_fields = fields_set & {"objective", "status", "token_budget"}
        stored_session = STORE.sessions.get(session_id, {})
        previous_goal = stored_session.get("codex_goal")
        goal = previous_goal
        previous_time_budget = stored_session.get(
            "codex_goal_time_budget_seconds"
        )
        time_budget_exhausted = bool(
            stored_session.get("codex_goal_time_budget_exhausted")
        )
        validate_codex_goal_resume_budget(stored_session, req)
        if reservation_id:
            async with RUNS.queue_lock:
                promotion = RUNS.queue_start_tasks.get(session_id)
                prior_queue = (
                    RUNS.has_waiting_work(session_id)
                    or (promotion is not None and not promotion.done())
                )
            if prior_queue:
                raise HTTPException(
                    status_code=409,
                    detail="This chat has queued work. Run or remove it before resuming the goal.",
                )
            blocker = await turn_start_blocker(ignore_session_id=session_id)
            if blocker:
                raise HTTPException(status_code=503, detail=f"agent launch deferred: {blocker}")
            operation_id = f"codexgoal_{uuid.uuid4().hex[:16]}"
            subscription = manager.subscribe_thread(thread_id)
            provider_model, provider_effort, provider_service_tier = codex_runtime_settings(stored_session)
            async with RUNS.active_lock:
                active = RUNS.active.get(session_id)
                if not active or str(active.get("codex_control_reservation_id") or "") != reservation_id:
                    raise HTTPException(status_code=409, detail="The goal resume reservation changed; retry.")
                active["run_id"] = operation_id
                active["codex_native_operation_kind"] = "goal_resume"
                active["provider_model"] = provider_model
                active["provider_effort"] = provider_effort
                active["provider_service_tier"] = provider_service_tier
                active["native_steer_queue"] = asyncio.Queue(maxsize=1)
                current_turn = RUNS.current_turns.get(session_id)
                if current_turn is not None:
                    current_turn["run_id"] = operation_id
                    current_turn["purpose"] = "codex_goal_resume"
            await append_event(session_id, "turn_started", {
                "run_id": operation_id,
                "backend": BACKEND_CODEX,
                "purpose": "codex_goal_resume",
                "thread_id": thread_id,
                "message": "Resuming the persistent Codex goal.",
            })
            resume_started_published = True
        if native_fields:
            kwargs: dict[str, Any] = {}
            if "objective" in fields_set:
                kwargs["objective"] = (
                    str(req.objective).strip() if req.objective is not None else None
                )
            if "status" in fields_set:
                kwargs["status"] = req.status
            if "token_budget" in fields_set:
                kwargs["token_budget"] = req.token_budget
            goal_activation_attempted = bool(reservation_id)
            goal = await asyncio.wait_for(
                manager.set_thread_goal(thread_id, **kwargs),
                timeout=CODEX_GOAL_CONTROL_TIMEOUT_SECONDS,
            )
        if not isinstance(goal, dict):
            raise HTTPException(
                status_code=409,
                detail="set an objective before configuring a time limit",
            )
        time_budget_seconds = (
            req.time_budget_seconds
            if "time_budget_seconds" in fields_set
            else previous_time_budget
        )
        previous_objective = (
            str(previous_goal.get("objective") or "")
            if isinstance(previous_goal, dict)
            else ""
        )
        current_objective = str(goal.get("objective") or "")
        if "objective" in fields_set and current_objective != previous_objective:
            time_budget_exhausted = False
        if "time_budget_seconds" in fields_set:
            if time_budget_seconds is None:
                time_budget_exhausted = False
            elif (
                not isinstance(previous_time_budget, (int, float))
                or isinstance(previous_time_budget, bool)
                or float(time_budget_seconds) > float(previous_time_budget)
            ):
                time_budget_exhausted = False
        used = goal.get("timeUsedSeconds")
        if (
            time_budget_seconds is not None
            and isinstance(used, (int, float))
            and not isinstance(used, bool)
            and float(used) >= float(time_budget_seconds)
        ):
            time_budget_exhausted = True
        async with STORE.transaction():
            current = STORE.sessions.get(session_id)
            if current:
                # Native goal notifications may finish or pause this goal
                # before goal/set returns its earlier active snapshot. UI
                # controls are serialized by the lifecycle lock, so a replaced
                # goal object here is newer provider-authoritative state.
                latest_goal = current.get("codex_goal")
                if latest_goal is not previous_goal:
                    goal = latest_goal
                    if goal is None:
                        time_budget_seconds = None
                        time_budget_exhausted = False
                    elif isinstance(goal, dict):
                        latest_used = goal.get("timeUsedSeconds")
                        if (
                            time_budget_seconds is not None
                            and isinstance(latest_used, (int, float))
                            and not isinstance(latest_used, bool)
                            and latest_used >= time_budget_seconds
                        ):
                            time_budget_exhausted = True
                current["codex_goal"] = goal
                current["codex_goal_time_budget_seconds"] = time_budget_seconds
                current["codex_goal_time_budget_exhausted"] = (
                    time_budget_exhausted
                )
                current["updated_at"] = now_iso()
                await STORE.save()
        if fields_set == {"time_budget_seconds"}:
            await append_event(
                session_id,
                "codex_goal_updated",
                {
                    "goal": goal,
                    "time_budget_seconds": time_budget_seconds,
                    "time_budget_owner": "Hyadex",
                },
            )
        if reservation_id and subscription is not None:
            task = start_codex_goal_resume_consumer(
                session_id, operation_id, manager,
                thread_id, reservation_id, subscription,
            )
            consumer_owns_reservation = True
            async with RUNS.active_lock:
                active = RUNS.active.get(session_id)
                if active and str(active.get("codex_control_reservation_id") or "") == reservation_id:
                    active["owner_task"] = task
        return {
            "goal": goal,
            "time_budget_seconds": time_budget_seconds,
            "time_budget_exhausted": time_budget_exhausted,
        }
    except asyncio.TimeoutError as exc:
        raise HTTPException(
            status_code=504,
            detail="Codex goal update timed out. Retry the change.",
        ) from exc
    except Exception as exc:
        if isinstance(exc, HTTPException):
            raise
        raise codex_control_http_error(exc) from exc
    finally:
        if not consumer_owns_reservation:
            try:
                if goal_activation_attempted:
                    await stop_codex_goal_resume(
                        session_id, manager, thread_id, reservation_id,
                    )
            finally:
                if subscription is not None:
                    subscription.close()
                await release_codex_control_thread(
                    session_id, manager, thread_id,
                    reserved_session=bool(reservation_id),
                    reservation_id=reservation_id,
                )
                if resume_started_published:
                    await append_event(session_id, "turn_finished", {
                        "run_id": operation_id,
                        "backend": BACKEND_CODEX,
                        "purpose": "codex_goal_resume",
                        "status": "failed",
                        "message": "The goal could not be resumed.",
                    })


async def _delete_codex_goal_locked(session_id: str) -> dict[str, Any]:
    if not CODEX_GOALS_ENABLED:
        raise HTTPException(
            status_code=409,
            detail=(
                "Persistent Codex goals are disabled for this server. "
                "Enable them in server settings before clearing a saved goal."
            ),
        )
    def cleared_result() -> dict[str, Any]:
        return {
            "goal": None,
            "time_budget_seconds": None,
            "time_budget_exhausted": False,
        }

    try:
        manager, thread_id, _session = await acquire_codex_control_thread(
            session_id,
            allow_active_goal_mutation=True,
        )
    except HTTPException as exc:
        session = STORE.sessions.get(session_id) or {}
        stale_thread_id = str(session_provider_id(session) or "").strip()
        if stale_thread_id and is_codex_resume_failure(str(exc.detail or "")):
            await quarantine_codex_goal_thread(
                session_id,
                stale_thread_id,
                reason=(
                    "Clearing the goal found a stale provider thread: "
                    f"{concise_error_message(exc.detail)}"
                ),
                clear_goal=True,
            )
            return cleared_result()
        raise
    try:
        # Generation reconciliation may already have established that the
        # provider has no goal. In that case the user's requested terminal
        # state is satisfied and another native call is unnecessary.
        if (STORE.sessions.get(session_id) or {}).get("codex_goal") is None:
            return cleared_result()
        await asyncio.wait_for(
            manager.clear_thread_goal(thread_id),
            timeout=CODEX_GOAL_CONTROL_TIMEOUT_SECONDS,
        )
        async with STORE.transaction():
            current = STORE.sessions.get(session_id)
            if current:
                current["codex_goal"] = None
                current["codex_goal_time_budget_seconds"] = None
                current["codex_goal_time_budget_exhausted"] = False
                current["updated_at"] = now_iso()
                await STORE.save()
        return cleared_result()
    except asyncio.TimeoutError:
        # The request may have reached Codex even though its response was lost.
        # Explicit Clear is nevertheless an unambiguous user intent: detach the
        # uncertain provider thread and clear local state so the goal cannot
        # keep the chat stuck behind a retry-only UI.
        await quarantine_codex_goal_thread(
            session_id,
            thread_id,
            reason=(
                "Clearing the Codex goal timed out; the provider result is "
                "ambiguous, so the old thread was detached."
            ),
            clear_goal=True,
        )
        return cleared_result()
    except Exception as exc:
        if is_codex_resume_failure(concise_error_message(exc)):
            await quarantine_codex_goal_thread(
                session_id,
                thread_id,
                reason=(
                    "Clearing the goal found a stale provider thread: "
                    f"{concise_error_message(exc)}"
                ),
                clear_goal=True,
            )
            return cleared_result()
        if isinstance(exc, HTTPException):
            raise
        raise codex_control_http_error(exc) from exc
    finally:
        await release_codex_control_thread(session_id, manager, thread_id)


async def _post_codex_compact_locked(session_id: str) -> dict[str, Any]:
    manager, thread_id, control_session = await acquire_codex_control_thread(
        session_id,
        reserve_session=True,
    )
    reservation_id = str(
        control_session.get("_codex_control_reservation_id") or ""
    )
    operation_id = f"codexop_{uuid.uuid4().hex[:16]}"
    subscription = manager.subscribe_thread(thread_id)
    token_usage_before = current_codex_token_usage_snapshot(session_id)
    try:
        async with RUNS.active_lock:
            active = RUNS.active.get(session_id)
            if active:
                active["run_id"] = operation_id
                active["codex_native_operation_kind"] = "compaction"
                active["provider_turn_id"] = None
                active["provider_turn_ready"] = False
                active["codex_compaction_in_progress"] = True
                active["codex_compaction_token_usage_before"] = (
                    dict(token_usage_before)
                    if token_usage_before is not None
                    else None
                )
                active["codex_compaction_token_usage_after"] = None
        await manager.compact_thread(thread_id)
        task = asyncio.create_task(
            consume_codex_native_turn(
                session_id,
                operation_id,
                "compaction",
                manager,
                thread_id,
                reservation_id,
                subscription,
            )
        )
        register_codex_native_action(session_id, operation_id, task)
        try:
            await append_event(
                session_id,
                "codex_compaction_started",
                {
                    "run_id": operation_id,
                    "operation_id": operation_id,
                    "thread_id": thread_id,
                    "token_usage_before": token_usage_before,
                    "message": (
                        "Codex started compacting this thread's context."
                    ),
                },
            )
        except Exception as exc:
            logger.warning(
                "failed to persist Codex compaction start session=%s: %s",
                session_id,
                concise_error_message(exc),
            )
        return {"accepted": True, "operation_id": operation_id}
    except Exception as exc:
        subscription.close()
        await release_codex_control_thread(
            session_id,
            manager,
            thread_id,
            reserved_session=True,
            reservation_id=reservation_id,
        )
        raise codex_control_http_error(exc) from exc
