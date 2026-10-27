"""Consuming native Codex turns (goal continuations, compactions) and releasing their control threads."""
from __future__ import annotations

import asyncio
from contextlib import suppress
import logging.handlers
import time
from typing import Any

from hyadex.core.config import (
    BACKEND_CODEX,
    CODEX_APP_SERVER_LIFECYCLE_TIMEOUT_SECONDS,
    IDLE_KILL_SECONDS,
)
from hyadex.core.steering import mark_native_steer_accepted
from hyadex.core.runs import RUNS
from hyadex.core.server_logging import concise_error_message
from hyadex.core.sessions import (
    append_event,
    remember_codex_native_compaction_terminal,
    session_lifecycle_lock,
    STORE,
)
from hyadex.core.session_settings import codex_goal_time_budget_remaining
from hyadex.core.turns import schedule_next_queued_turn
from hyadex.core.util import clean_assistant_text
from hyadex.features.usage import join_task_despite_caller_cancellation
from hyadex.providers.authority import NativeSteerHandoffError
from hyadex.providers.codex.app_server import (
    CodexAppServerManager,
    CodexAppServerProtocolError,
    CodexAppServerSubscriptionClosed,
)
from hyadex.providers.codex.goals import (
    apply_codex_goal_time_budget_limit,
    commit_codex_goal_steer,
    send_codex_goal_steer,
    stop_codex_goal_resume,
)
from hyadex.providers.codex.managers import (
    claim_codex_control_terminal_publication,
    CODEX_CONTROL_TERMINAL_FENCES,
    release_codex_control_slot,
    release_codex_interactive_control_lease,
    unpin_codex_app_server_thread,
)
from hyadex.providers.codex.notifications import (
    codex_app_server_reasoning_plaintext,
    codex_app_server_reasoning_summary,
    codex_app_server_tool,
    codex_app_server_tool_output,
    codex_reasoning_text,
    project_codex_notification,
    record_codex_token_usage,
)
from hyadex.providers.codex.reasoning_stream import (
    clear_reasoning_summary_stream,
    finish_reasoning_summary_stream,
    persist_reasoning_summary,
    reasoning_summary_stream_item,
    update_reasoning_summary_stream,
)
from hyadex.providers.codex.runtime import is_codex_app_server_retry_notice

logger = logging.getLogger("hyadex-server")


async def release_codex_control_thread(
    session_id: str,
    manager: CodexAppServerManager,
    thread_id: str,
    *,
    reserved_session: bool = False,
    reservation_id: str = "",
    lease_already_released: bool = False,
    schedule_queue: bool = True,
) -> bool:
    released = False
    session_idle = False
    if not lease_already_released:
        release_codex_interactive_control_lease(thread_id)
    try:
        with suppress(Exception):
            await unpin_codex_app_server_thread(manager, thread_id)
    finally:
        if reserved_session:
            released = await release_codex_control_slot(
                session_id,
                expected_thread_id=thread_id,
                expected_reservation_id=reservation_id,
                allow_prebind=False,
            )
            if schedule_queue and released:
                schedule_next_queued_turn(session_id)
        else:
            async with RUNS.active_lock:
                RUNS.maintenance.discard(session_id)
                session_idle = session_id not in RUNS.busy
            if schedule_queue and session_idle:
                schedule_next_queued_turn(session_id)
    return released if reserved_session else session_idle


async def finish_codex_control_terminal_publication(
    session_id: str,
    *,
    reservation_id: str,
    schedule_queue: bool,
) -> None:
    """Release a claimed native terminal fence and optionally drain queue."""

    expected_reservation = str(reservation_id or "").strip()
    async with RUNS.active_lock:
        if (
            not expected_reservation
            or CODEX_CONTROL_TERMINAL_FENCES.get(session_id)
            != expected_reservation
        ):
            return
        CODEX_CONTROL_TERMINAL_FENCES.pop(session_id, None)
        RUNS.maintenance.discard(session_id)
    if schedule_queue:
        schedule_next_queued_turn(session_id)


async def consume_codex_native_turn(
    session_id: str,
    operation_id: str,
    operation: str,
    manager: CodexAppServerManager,
    thread_id: str,
    reservation_id: str,
    subscription: Any,
    *,
    turn_id: str | None = None,
    interrupted_before_start: bool = False,
    finalize_operation: bool = True,
    initial_sequence: int = 0,
    initial_pending_steer: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Consume native work; an inline ordinary runner retains its own cleanup."""
    assistant_deltas: dict[str, list[str]] = {}
    reasoning_summary_deltas: dict[str, list[str]] = {}
    plan_deltas: dict[str, list[str]] = {}
    completed_reasoning_items: set[str | tuple[str, str]] = set()
    terminal_status = "completed"
    terminal_error: str | None = None
    schedule_queue = True
    goal_resume = operation == "goal_resume"
    last_turn_id = turn_id
    result_text = ""
    goal_turn_running = bool(turn_id)
    goal_clock_started: float | None = None
    goal_clock_used = 0.0
    goal_clock_identity = ""
    seen_goal_turn_ids: set[str] = set()
    goal_activity_deadline = time.monotonic() + max(
        CODEX_APP_SERVER_LIFECYCLE_TIMEOUT_SECONDS, IDLE_KILL_SECONDS,
    )
    goal_steer_queue = (RUNS.active.get(session_id) or {}).get("native_steer_queue") if goal_resume else None
    steer_task: asyncio.Task[Any] | None = None
    notification_task: asyncio.Task[Any] | None = None
    steer_request: dict[str, Any] | None = (
        initial_pending_steer["request"] if initial_pending_steer is not None else None
    )
    pending_steer: dict[str, Any] | None = initial_pending_steer
    handled_sequence = initial_sequence
    synthetic_sequence = initial_sequence

    async def next_goal_notification() -> tuple[int, dict[str, Any]]:
        nonlocal synthetic_sequence
        reader = getattr(subscription, "next_notification_with_sequence", None)
        if callable(reader):
            return await reader(timeout=0.5)
        notification = await subscription.next_notification(timeout=0.5)
        synthetic_sequence += 1
        return synthetic_sequence, notification

    def reject_goal_steer(request: dict[str, Any], exc: BaseException) -> None:
        future = request.get("future")
        if future is not None and not future.done():
            if not isinstance(exc, NativeSteerHandoffError):
                safe = not request.get("_goal_steer_rpc_started")
                exc = NativeSteerHandoffError(
                    concise_error_message(exc), safe_to_requeue=safe, delivery_uncertain=not safe,
                )
            future.set_exception(exc)

    try:
        if interrupted_before_start:
            raise asyncio.CancelledError
        while True:
            if goal_resume:
                if pending_steer is None and steer_request is not None:
                    if not steer_request.get("expected_provider_turn_id"):
                        # Send now may arrive in the gap between native goal
                        # turns. Hold this one command on the existing stream,
                        # then bind it once to the next ready turn. The helper
                        # still rechecks goal/owner/Stop and the exact turn at
                        # the wire boundary; no poller or goal restart is added.
                        async with RUNS.active_lock:
                            active = RUNS.active.get(session_id) or {}
                            if (
                                active.get("run_id") == operation_id
                                and active.get("native_steer_queue") is goal_steer_queue
                                and active.get("provider_turn_ready")
                            ):
                                steer_request["expected_provider_turn_id"] = str(
                                    active.get("provider_turn_id") or ""
                                )
                    if steer_request.get("expected_provider_turn_id"):
                        try:
                            pending_steer = await send_codex_goal_steer(
                                session_id, operation_id, manager, thread_id,
                                reservation_id, subscription, goal_steer_queue, steer_request,
                            )
                        except BaseException as exc:
                            reject_goal_steer(steer_request, exc)
                            steer_request = None
                            if isinstance(exc, asyncio.CancelledError):
                                raise
                        continue
                if pending_steer is not None and handled_sequence >= pending_steer["watermark"]:
                    try:
                        result = await commit_codex_goal_steer(
                            session_id, operation_id, thread_id, reservation_id, pending_steer,
                        )
                        future = pending_steer["request"]["future"]
                        if not future.done():
                            future.set_result(result)
                    except BaseException as exc:
                        reject_goal_steer(pending_steer["request"], exc)
                        if isinstance(exc, asyncio.CancelledError):
                            raise
                    finally:
                        pending_steer = None
                        steer_request = None
                latest = STORE.sessions.get(session_id) or {}
                goal = latest.get("codex_goal")
                goal_status = str(goal.get("status") or "") if isinstance(goal, dict) else ""
                async with RUNS.active_lock:
                    active = RUNS.active.get(session_id)
                    stopped = bool(active and active.get("stop_requested"))
                remaining = codex_goal_time_budget_remaining(latest)
                used = goal.get("timeUsedSeconds") if isinstance(goal, dict) else 0
                used = float(used) if isinstance(used, (int, float)) and not isinstance(used, bool) else 0.0
                identity = str(goal.get("id") or goal.get("objective") or "") if isinstance(goal, dict) else ""
                now = time.monotonic()
                if not goal_turn_running or goal_status != "active":
                    goal_clock_started = None
                elif goal_clock_started is None or identity != goal_clock_identity:
                    goal_clock_started = now
                    goal_clock_used = used
                    goal_clock_identity = identity
                if remaining is not None and goal_clock_started is not None:
                    projected_used = max(used, goal_clock_used + now - goal_clock_started)
                    limit = float(latest["codex_goal_time_budget_seconds"])
                    if remaining == 0 or projected_used >= limit:
                        async with session_lifecycle_lock(session_id):
                            current = STORE.sessions.get(session_id) or {}
                            current_goal = current.get("codex_goal") or {}
                            if (
                                current.get("codex_goal_time_budget_seconds") == limit
                                and str(current_goal.get("id") or current_goal.get("objective") or "") == identity
                                and current_goal.get("status") == "active"
                            ):
                                await apply_codex_goal_time_budget_limit(
                                    session_id, operation_id, manager, thread_id,
                                    None, limit,
                                )
                                await stop_codex_goal_resume(
                                    session_id, manager, thread_id, reservation_id,
                                )
                try:
                    if goal_steer_queue is None:
                        handled_sequence, notification = await next_goal_notification()
                    else:
                        if notification_task is None:
                            notification_task = asyncio.create_task(next_goal_notification())
                        if pending_steer is None and steer_request is None and steer_task is None:
                            steer_task = asyncio.create_task(goal_steer_queue.get())
                        waiters = {notification_task}
                        if pending_steer is None and steer_task is not None:
                            waiters.add(steer_task)
                        done, _ = await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
                        if notification_task in done and (
                            pending_steer is not None or steer_task not in done
                        ):
                            completed_notification = notification_task
                            notification_task = None
                            sequence, notification = await completed_notification
                            if pending_steer is not None and sequence > pending_steer["watermark"]:
                                raise CodexAppServerProtocolError(
                                    "Goal steer notification boundary skipped preceding output",
                                    request_sent=True, safe_to_retry=False,
                                )
                            handled_sequence = sequence
                        else:
                            steer_request = steer_task.result()
                            steer_task = None
                            mark_native_steer_accepted(steer_request)
                            continue
                except asyncio.TimeoutError:
                    # Drain already queued output before using control state:
                    # projection may have persisted complete ahead of us.
                    if not goal_turn_running and (goal_status != "active" or stopped):
                        # Serialize the last owner decision with Resume. A goal
                        # mutation accepted during cleanup must not start work
                        # after this stream's consumer has exited.
                        async with session_lifecycle_lock(session_id):
                            async with RUNS.active_lock:
                                active = RUNS.active.get(session_id)
                                latest_goal = (STORE.sessions.get(session_id) or {}).get("codex_goal")
                                stopped = bool(
                                    operation_id in RUNS.stopped_runs
                                    or (active and active.get("stop_requested"))
                                )
                                enqueued = getattr(subscription, "_last_enqueued_sequence", 0)
                                pending_output = isinstance(enqueued, int) and enqueued > handled_sequence
                                if not stopped and (
                                    pending_output
                                    or (isinstance(latest_goal, dict) and latest_goal.get("status") == "active")
                                ):
                                    continue
                                if active and active.get("run_id") == operation_id:
                                    active["codex_goal_handoff_closed"] = True
                        terminal_status = "interrupted" if stopped else "completed"
                        break
                    if time.monotonic() >= goal_activity_deadline:
                        raise
                    continue
                goal_activity_deadline = time.monotonic() + max(
                    CODEX_APP_SERVER_LIFECYCLE_TIMEOUT_SECONDS, IDLE_KILL_SECONDS,
                )
            else:
                notification = await subscription.next_notification(
                    timeout=max(
                        CODEX_APP_SERVER_LIFECYCLE_TIMEOUT_SECONDS,
                        IDLE_KILL_SECONDS,
                    )
                )
            method = str(notification.get("method") or "")
            params = (
                notification.get("params")
                if isinstance(notification.get("params"), dict)
                else {}
            )
            notification_turn_id = str(
                params.get("turnId")
                or (
                    params.get("turn", {}).get("id")
                    if isinstance(params.get("turn"), dict)
                    else ""
                )
                or ""
            )
            if goal_resume and method == "turn/started" and notification_turn_id:
                if notification_turn_id in seen_goal_turn_ids:
                    continue
                seen_goal_turn_ids.add(notification_turn_id)
                await finish_reasoning_summary_stream(session_id, operation_id, completed_reasoning_items)
                turn_id = notification_turn_id
                goal_turn_running = True
                terminal_status = "completed"
                terminal_error = None
                assistant_deltas.clear()
                reasoning_summary_deltas.clear()
                plan_deltas.clear()
            if turn_id and notification_turn_id and notification_turn_id != turn_id:
                continue
            if method == "turn/started":
                last_turn_id = notification_turn_id or last_turn_id
                interrupt_after_start = False
                interrupt_turn_id = notification_turn_id
                if notification_turn_id and not turn_id:
                    turn_id = notification_turn_id
                    terminal_status = "completed"
                    terminal_error = None
                if notification_turn_id:
                    async with RUNS.active_lock:
                        active = RUNS.active.get(session_id)
                        if (
                            active
                            and str(active.get("run_id") or "") == operation_id
                        ):
                            # The goal projector binds the latest native turn
                            # before RPC completion. A backlogged consumer must
                            # not replace that id with an earlier goal turn.
                            if not goal_resume:
                                active["provider_turn_id"] = notification_turn_id
                                active["provider_turn_ready"] = True
                            else:
                                if not active.get("provider_turn_id"):
                                    # A continuation can arrive before an ordinary
                                    # runner hands its retained stream to us.
                                    active["provider_turn_id"] = notification_turn_id
                                    active["provider_turn_ready"] = True
                                    active["native_interrupt_sent"] = False
                                interrupt_turn_id = str(
                                    active.get("provider_turn_id")
                                    or notification_turn_id
                                )
                            if (
                                active.get("stop_requested")
                                and not active.get("native_interrupt_sent")
                            ):
                                active["native_interrupt_sent"] = True
                                interrupt_after_start = True
                if interrupt_after_start:
                    try:
                        await manager.request(
                            "turn/interrupt",
                            {
                                "threadId": thread_id,
                                "turnId": interrupt_turn_id,
                            },
                        )
                    except Exception as exc:
                        async with RUNS.active_lock:
                            active = RUNS.active.get(session_id)
                            if (
                                active
                                and str(active.get("run_id") or "")
                                == operation_id
                            ):
                                active["native_interrupt_sent"] = False
                        logger.warning(
                            "Deferred Codex native control interrupt failed "
                            "session=%s operation=%s error=%s",
                            session_id,
                            operation_id,
                            concise_error_message(exc),
                        )
                continue
            item = params.get("item") if isinstance(params.get("item"), dict) else {}
            item_id = str(params.get("itemId") or item.get("id") or "")
            if method == "item/agentMessage/delta" and item_id:
                assistant_deltas.setdefault(item_id, []).append(
                    str(params.get("delta") or "")
                )
                continue
            if method == "item/reasoning/textDelta" and item_id:
                if ("reasoning", item_id) not in completed_reasoning_items:
                    await update_reasoning_summary_stream(session_id, operation_id, item_id, params, {
                        "provider_turn_id": turn_id, "purpose": f"codex_{operation}",
                    }, "reasoning")
                continue
            if method in {"item/reasoning/summaryTextDelta", "item/reasoning/summaryPartAdded"} and item_id:
                if item_id in completed_reasoning_items:
                    continue
                reasoning_summary_deltas.setdefault(item_id, []).append(
                    str(params.get("delta") or "")
                )
                await update_reasoning_summary_stream(session_id, operation_id, item_id, params, {
                    "provider_turn_id": turn_id, "purpose": f"codex_{operation}",
                })
                continue
            if method == "item/plan/delta" and item_id:
                plan_deltas.setdefault(item_id, []).append(
                    str(params.get("delta") or "")
                )
                continue
            if method == "item/started" and item:
                tool = codex_app_server_tool(item)
                if tool:
                    await append_event(
                        session_id,
                        "tool_started",
                        {
                            "run_id": operation_id,
                            "provider_turn_id": turn_id,
                            "item_id": item_id,
                            "tool": tool,
                            "purpose": f"codex_{operation}",
                        },
                    )
                continue
            if method == "item/completed" and item:
                item_type = str(item.get("type") or "")
                if item_type == "agentMessage":
                    buffered = assistant_deltas.pop(item_id, [])
                    text = clean_assistant_text(
                        str(
                            item.get("text")
                            or "".join(buffered)
                        )
                    )
                    if text:
                        phase = str(item.get("phase") or "")
                        event_type = (
                            "reasoning_summary"
                            if phase == "commentary"
                            else "assistant_text"
                        )
                        if event_type == "assistant_text":
                            result_text = text
                        await append_event(
                            session_id,
                            event_type,
                            {
                                "run_id": operation_id,
                                "provider_turn_id": turn_id,
                                "item_id": item_id,
                                **({"phase": phase} if phase in {"commentary", "final_answer"} else {}),
                                "text": text,
                                "purpose": f"codex_{operation}",
                            },
                        )
                elif item_type in {"reasoning", "plan"}:
                    if item_type == "reasoning":
                        plaintext_identity = ("reasoning", item_id)
                        if plaintext_identity not in completed_reasoning_items:
                            plaintext_stream = reasoning_summary_stream_item(session_id, operation_id, item_id, "reasoning")
                            plaintext = codex_app_server_reasoning_plaintext(item) or plaintext_stream.get("text")
                            if plaintext:
                                await persist_reasoning_summary(session_id, {
                                    "run_id": operation_id, "provider_turn_id": turn_id,
                                    "item_id": item_id, "phase": "reasoning", "text": plaintext,
                                    **({"reasoning_after_seq": plaintext_stream["after_seq"]} if plaintext_stream else {}),
                                    "purpose": f"codex_{operation}",
                                }, completed_reasoning_items)
                            completed_reasoning_items.add(plaintext_identity)
                            await clear_reasoning_summary_stream(session_id, operation_id, item_id, "reasoning")
                    streamed = {}
                    if item_type == "reasoning":
                        if item_id in completed_reasoning_items:
                            continue
                        streamed = reasoning_summary_stream_item(session_id, operation_id, item_id)
                        buffered = reasoning_summary_deltas.pop(item_id, [])
                        text = codex_app_server_reasoning_summary(item) or streamed.get("text") or "".join(buffered)
                    else:
                        buffered = plan_deltas.pop(item_id, [])
                        text = codex_reasoning_text(item) or "".join(buffered)
                    if text:
                        summary_payload = {
                            "run_id": operation_id,
                            "provider_turn_id": turn_id,
                            "item_id": item_id,
                            "phase": "plan" if item_type == "plan" else "summary",
                            "text": text,
                            **({"reasoning_after_seq": streamed["after_seq"]} if streamed else {}),
                            "purpose": f"codex_{operation}",
                        }
                        if item_type == "reasoning":
                            await persist_reasoning_summary(session_id, summary_payload, completed_reasoning_items)
                        else:
                            await append_event(session_id, "reasoning_summary", summary_payload)
                    if item_type == "reasoning":
                        completed_reasoning_items.add(item_id)
                        await clear_reasoning_summary_stream(session_id, operation_id, item_id)
                elif item_type == "contextCompaction" and operation == "compaction":
                    native_turn_id = str(params.get("turnId") or turn_id or "") or None
                    native_item_id = str(
                        params.get("itemId") or item.get("id") or ""
                    ) or None
                    remember_codex_native_compaction_terminal(
                        session_id,
                        thread_id,
                        native_turn_id,
                        native_item_id,
                    )
                    async with RUNS.active_lock:
                        active = RUNS.active.get(session_id)
                        if (
                            active
                            and str(active.get("run_id") or "") == operation_id
                        ):
                            active["codex_compaction_turn_id"] = native_turn_id
                            active["codex_compaction_item_id"] = native_item_id
                else:
                    tool = codex_app_server_tool(item)
                    if tool:
                        output, exit_code, is_error = codex_app_server_tool_output(item)
                        await append_event(
                            session_id,
                            "tool_finished",
                            {
                                "run_id": operation_id,
                                "provider_turn_id": turn_id,
                                "item_id": item_id,
                                "tool_id": tool["id"],
                                "tool": tool,
                                "output": output,
                                "exit_code": exit_code,
                                "is_error": is_error,
                                "purpose": f"codex_{operation}",
                            },
                        )
                continue
            if method == "error":
                message = concise_error_message(
                    params.get("error") or params.get("message") or params
                )
                if is_codex_app_server_retry_notice(params, message):
                    continue
                terminal_status = "failed"
                terminal_error = message
                continue
            if method == "turn/completed":
                completed_turn = (
                    params.get("turn")
                    if isinstance(params.get("turn"), dict)
                    else {}
                )
                terminal_status = str(completed_turn.get("status") or "failed")
                if completed_turn.get("error"):
                    terminal_error = concise_error_message(completed_turn["error"])
                elif terminal_status == "completed":
                    # The provider's successful terminal packet supersedes
                    # transient error notices from this exact native turn.
                    terminal_error = None
                if goal_resume:
                    goal_turn_running = False
                    goal_clock_started = None
                    async with RUNS.active_lock:
                        active = RUNS.active.get(session_id)
                        if (
                            active and active.get("run_id") == operation_id
                            and active.get("codex_control_reservation_id") == reservation_id
                            and active.get("provider_turn_id") == notification_turn_id
                        ):
                            active["provider_turn_id"] = None
                            active["provider_turn_ready"] = False
                    # Keep the thread-wide subscription and exact local owner
                    # across native goal turns; no synthetic user turn is sent.
                    await manager.wait_for_notification_handler(
                        project_codex_notification, thread_id,
                    )
                    if terminal_status == "completed" and not terminal_error:
                        turn_id = None
                        continue
                break
    except (asyncio.TimeoutError, CodexAppServerSubscriptionClosed) as exc:
        terminal_status = "failed"
        terminal_error = concise_error_message(exc)
    except asyncio.CancelledError:
        terminal_status = "interrupted"
        schedule_queue = False
        raise
    except Exception as exc:
        terminal_status = "failed"
        terminal_error = concise_error_message(exc)
    finally:
        summary_cleanup = asyncio.create_task(finish_reasoning_summary_stream(
            session_id, operation_id, completed_reasoning_items))
        try:
            await asyncio.shield(summary_cleanup)
        except asyncio.CancelledError:
            await join_task_despite_caller_cancellation(summary_cleanup)
        if goal_steer_queue is not None:
            async def settle_goal_steering() -> None:
                async with RUNS.active_lock:
                    active = RUNS.active.get(session_id)
                    if (
                        active and active.get("run_id") == operation_id
                        and active.get("codex_control_reservation_id") == reservation_id
                        and active.get("native_steer_queue") is goal_steer_queue
                    ):
                        active["native_steer_queue"] = None
                        active["provider_turn_ready"] = False
                if notification_task is not None:
                    notification_task.cancel()
                    await asyncio.gather(notification_task, return_exceptions=True)
                if steer_task is not None:
                    if steer_task.done() and not steer_task.cancelled():
                        with suppress(Exception):
                            reject_goal_steer(steer_task.result(), RuntimeError("Goal steering consumer finished before delivery"))
                    else:
                        steer_task.cancel()
                        await asyncio.gather(steer_task, return_exceptions=True)
                if steer_request is not None:
                    reject_goal_steer(steer_request, RuntimeError("Goal steering consumer finished during delivery"))
                while not goal_steer_queue.empty():
                    reject_goal_steer(goal_steer_queue.get_nowait(), RuntimeError("Goal steering consumer finished before delivery"))

            steering_cleanup = asyncio.create_task(settle_goal_steering())
            try:
                await asyncio.shield(steering_cleanup)
            except BaseException:
                await join_task_despite_caller_cancellation(steering_cleanup)
        if goal_resume and terminal_status != "completed":
            await stop_codex_goal_resume(
                session_id, manager, thread_id, reservation_id,
            )
        subscription.close()
        if finalize_operation:
            terminal_claimed = False
            try:
                # The subscription is intentionally low-latency and can observe
                # terminal delivery before async projection finishes. Drain the
                # per-thread projection tail before reading usage state or
                # claiming terminal publication so attribution cannot race.
                await manager.wait_for_notification_handler(
                    project_codex_notification,
                    thread_id,
                )
                compaction_usage: dict[str, Any] = {}
                if operation == "compaction":
                    async with RUNS.active_lock:
                        active = RUNS.active.get(session_id)
                        if (
                            active
                            and str(active.get("run_id") or "") == operation_id
                            and str(
                                active.get("codex_control_reservation_id") or ""
                            ).strip() == str(reservation_id or "").strip()
                        ):
                            before = active.get(
                                "codex_compaction_token_usage_before"
                            )
                            after = active.get(
                                "codex_compaction_token_usage_after"
                            )
                            compaction_usage = {
                                "token_usage_before": (
                                    dict(before)
                                    if isinstance(before, dict)
                                    else None
                                ),
                                "token_usage_after": (
                                    dict(after)
                                    if isinstance(after, dict)
                                    else None
                                ),
                            }
                terminal_claimed = await claim_codex_control_terminal_publication(
                    session_id,
                    operation_id,
                    thread_id,
                    reservation_id,
                )
                if terminal_claimed and session_id in STORE.sessions:
                    after_snapshot = compaction_usage.get("token_usage_after")
                    if isinstance(after_snapshot, dict):
                        await record_codex_token_usage(
                            session_id,
                            after_snapshot,
                            force_checkpoint=True,
                        )
                    if terminal_error:
                        await append_event(
                            session_id,
                            "error",
                            {
                                "run_id": operation_id,
                                "message": terminal_error,
                                "purpose": f"codex_{operation}",
                            },
                        )
                    terminal_event_type = (
                        "codex_compaction_completed"
                        if operation == "compaction"
                        else "turn_finished"
                        if goal_resume
                        else f"codex_{operation}_finished"
                    )
                    operation_label = {
                        "compaction": "Context compaction",
                        "review": "Code review",
                        "shell": "Shell command",
                    }.get(operation, operation.replace("_", " ").title())
                    terminal_message = (
                        f"{operation_label} completed."
                        if terminal_status == "completed"
                        else f"{operation_label} {terminal_status}."
                    )
                    await append_event(
                        session_id,
                        terminal_event_type,
                        {
                            "run_id": operation_id,
                            "operation_id": operation_id,
                            "turn_id": turn_id,
                            "status": terminal_status,
                            **({"backend": BACKEND_CODEX, "purpose": "codex_goal_resume"} if goal_resume else {}),
                            "error": terminal_error,
                            "message": terminal_message,
                            **compaction_usage,
                        },
                    )
            finally:
                RUNS.stopped_runs.discard(operation_id)
                if terminal_claimed:
                    # The slot is already exactly released. Remove the admission
                    # fence before touching shared-thread LRU state: unpin can be
                    # slow or cancellation-hostile, but must never park the chat.
                    release_codex_interactive_control_lease(thread_id)
                    try:
                        publication_cleanup = asyncio.create_task(
                            finish_codex_control_terminal_publication(
                                session_id,
                                reservation_id=reservation_id,
                                schedule_queue=schedule_queue,
                            )
                        )
                        try:
                            await asyncio.shield(publication_cleanup)
                        except asyncio.CancelledError:
                            await join_task_despite_caller_cancellation(
                                publication_cleanup
                            )
                            raise
                    finally:
                        await release_codex_control_thread(
                            session_id,
                            manager,
                            thread_id,
                            reserved_session=True,
                            reservation_id=reservation_id,
                            lease_already_released=True,
                            schedule_queue=False,
                        )
                else:
                    await release_codex_control_thread(
                        session_id,
                        manager,
                        thread_id,
                        reserved_session=True,
                        reservation_id=reservation_id,
                        schedule_queue=schedule_queue,
                    )
        else:
            await manager.wait_for_notification_handler(
                project_codex_notification, thread_id,
            )
    return {
        "status": terminal_status, "error": terminal_error,
        "turn_id": last_turn_id, "result_text": result_text,
    }
