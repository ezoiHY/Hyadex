"""Chats: listing, creating, reading, updating, deleting and forking, read state, timelines and the live event WebSocket."""
from __future__ import annotations

import asyncio
from contextlib import suppress
import logging.handlers
import re
from typing import Any

from fastapi import APIRouter, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse

from hyadex.core.config import (
    BACKEND_CLAUDE,
    BACKEND_CODEX,
    CLAUDE_TRANSPORT_AGENT_SDK,
    CODEX_SESSION_CLEANUP_TIMEOUT_SECONDS,
    CODEX_TRANSPORT_APP_SERVER,
    DEFAULT_BACKEND,
    DEFAULT_SESSION_EVENT_LIMIT,
    EVENTS_WEBSOCKET_PROTOCOL,
    require_enabled_backend,
    SUBAGENT_SNAPSHOT_STATE_LIMIT,
    WEBSOCKET_SEND_TIMEOUT_SECONDS,
)
from hyadex.core.event_log import (
    build_timeline_index,
    read_client_events_page,
    read_visible_events_after_page,
    read_visible_events_page,
    reasoning_summary_stream_snapshot,
    send_event_catchup,
    send_event_stream_status,
)
from hyadex.core.http import router, websocket_authorized, websocket_endpoint_subprotocol
from hyadex.core.paths import code_diffs_dir, events_path
from hyadex.core.processes import terminate_process_tree
from hyadex.core.queue import (
    create_session_with_history,
    queued_turns_snapshot,
    schedule_provider_history_sync,
)
from hyadex.core.runs import RUNS
from hyadex.core.server_logging import concise_error_message
from hyadex.core.sessions import (
    cancel_generated_session_title,
    CLAUDE_STOP_FENCE_SESSIONS,
    CODEX_THREAD_SESSION_INDEX,
    CreateSessionRequest,
    empty_chat_dismissed_for,
    ensure_session_not_deleting,
    ensure_session_not_initializing,
    event_delivery_lock,
    existing_codex_app_server_manager,
    iter_session_events,
    last_event_seq_from_file,
    public_session,
    ReadSessionRequest,
    retired_session_runtime_message,
    revoke_provider_authority,
    session_lifecycle_lock,
    sorted_sessions,
    STORE,
    UpdateSessionRequest,
    wait_for_session_tasks,
)
from hyadex.core.state import (
    DELETED_SESSION_TOMBSTONES,
    DELETING_SESSIONS,
    EVENT_STREAM_HEARTBEAT_SECONDS,
    RUN_NOW_COMPLETED_RESULTS,
)
from hyadex.core.turns import (
    read_indexed_run_trace,
    reconcile_idle_queue_session_from_snapshot,
    schedule_claude_stop_fence_retry,
)
from hyadex.features.emergency import broadcast_committed_emergency_removal
from hyadex.features.forks import (
    _fork_session_locked,
    cleanup_aborted_session_fork,
    completed_fork_events,
    ForkSessionRequest,
)
from hyadex.features.history import history_projection_notice, local_history_import_guard
from hyadex.features.jobs import ensure_discardable_empty_session, JOBS
from hyadex.features.search import read_semantic_timeline_page
from hyadex.features.terminal import _update_session, kill_terminal_session
from hyadex.features.usage import (
    close_event_websocket_after_delivery_failure,
    close_event_websocket_over_capacity,
    HUB,
    join_task_despite_caller_cancellation,
)
from hyadex.providers.claude.history import session_provider_id
from hyadex.providers.claude.runtime import (
    cancel_claude_interactions,
    cancel_claude_stop_fence_retry,
    CLAUDE_INTERACTION_HANDLER_TASKS,
    CLAUDE_STOP_FENCE_RETRY_TASKS,
    interrupt_claude_sdk_run_bounded,
    stop_cleanup_in_progress,
)
from hyadex.providers.codex.app_server import CodexAppServerError
from hyadex.providers.codex.commands import (
    codex_app_server_manager,
    discover_session_provider_commands,
    SIDE_QUESTIONS,
)
from hyadex.providers.codex.interactions import (
    cancel_codex_interactions,
    CODEX_APPROVAL_ITEM_CACHE,
    CODEX_INTERACTION_HANDLER_TASKS,
)
from hyadex.providers.codex.managers import (
    cancel_codex_native_actions,
    CODEX_APP_SERVER_THREAD_PIN_COUNTS,
    CODEX_INTERACTIVE_CONTROL_THREAD_COUNTS,
    CODEX_INTERACTIVE_CONTROL_THREADS,
)
from hyadex.providers.codex.goals import (
    CODEX_GOAL_SYNC_GENERATIONS,
    CODEX_QUARANTINED_GOAL_THREADS,
    pause_active_codex_goal_for_stop,
)
from hyadex.providers.codex.threads import (
    CODEX_APP_SERVER_INVALIDATED_THREADS,
    CODEX_APP_SERVER_PINNED_THREADS,
    CODEX_APP_SERVER_THREAD_LRU,
    CODEX_APP_SERVER_THREAD_LRU_LOCK,
)
from hyadex.providers.registry import other_local_instance_provider_keys
from hyadex.providers.state import PROVIDERS
from hyadex.providers.subagents import (
    build_subagent_snapshot,
    CLAUDE_STOP_FENCE_ATTEMPT_LOCKS,
    CLAUDE_STOP_FENCE_ATTEMPT_TASKS,
    evict_claude_sdk_chat,
    reconcile_codex_subagents,
)
import local_session_ownership

logger = logging.getLogger("hyadex-server")

router = APIRouter()


@router.get("/api/sessions")
async def list_sessions(summary: bool = False, placeholder_client_id: str = "") -> dict[str, Any]:
    await STORE.ensure_sort_orders()
    sessions = [
        public_session(session, summary=summary)
        for session in sorted_sessions(list(STORE.sessions.values()))
        if not session.get("_fork_initializing")
        and not session.get("group_member")
        and not empty_chat_dismissed_for(session, placeholder_client_id)
    ]
    return {"sessions": sessions}


@router.post("/api/sessions")
async def create_session(req: CreateSessionRequest) -> dict[str, Any]:
    require_enabled_backend(req.backend)
    provider_keys = local_session_ownership.provider_session_keys(req.model_dump(), DEFAULT_BACKEND)
    if provider_keys:
        with local_history_import_guard():
            foreign_keys = await asyncio.to_thread(other_local_instance_provider_keys)
            if provider_keys & foreign_keys:
                raise HTTPException(
                    status_code=409,
                    detail="This provider conversation is already used by another local HyadexServer instance. Open it there instead.",
                )
            return await create_session_with_history(req)
    return await create_session_with_history(req)


@router.get("/api/sessions/{session_id}/timeline-index")
async def get_timeline_index(session_id: str) -> dict[str, Any]:
    if session_id not in STORE.sessions:
        raise HTTPException(status_code=404, detail="session not found")
    return await asyncio.to_thread(build_timeline_index, session_id)


@router.get("/api/sessions/{session_id}/subagents")
async def get_session_subagents(
    session_id: str,
    limit: int = Query(default=64, ge=1, le=SUBAGENT_SNAPSHOT_STATE_LIMIT),
) -> dict[str, Any]:
    if session_id not in STORE.sessions:
        raise HTTPException(status_code=404, detail="session not found")
    session = STORE.sessions[session_id]
    if str(session.get("backend") or "") == BACKEND_CODEX and session_provider_id(session):
        try:
            # Inspect the process which still owns the children during login
            # handoff. This read must not be rejected as a new model turn.
            manager = await codex_app_server_manager(session, allow_retired_login=True)
            await reconcile_codex_subagents(session_id, manager)
        except Exception as exc:
            logger.warning(
                "Codex subagent snapshot reconciliation failed session=%s error=%s",
                session_id,
                concise_error_message(exc),
            )
    return await asyncio.to_thread(build_subagent_snapshot, session_id, limit)


@router.get("/api/sessions/{session_id}")
async def get_session(
    session_id: str,
    after: int = 0,
    before: int | None = None,
    limit: int = DEFAULT_SESSION_EVENT_LIMIT,
    tail: bool = True,
    visible: bool = False,
    compact: bool = False,
    page_mode: str | None = None,
    semantic_before: int | None = None,
) -> dict[str, Any]:
    ensure_session_not_initializing(session_id)
    sess = STORE.sessions.get(session_id)
    if not sess:
        raise HTTPException(status_code=404, detail="session not found")
    # Opening a chat is the moment to notice that its provider transcript grew
    # somewhere else (the provider's own CLI, or another machine sharing it).
    # Scheduled rather than awaited so the open stays fast; the client's event
    # stream delivers whatever it recovers.
    schedule_provider_history_sync(sess)
    normalized_page_mode = str(page_mode or "").strip().lower()
    if normalized_page_mode not in {"", "semantic"}:
        raise HTTPException(status_code=400, detail="page_mode must be semantic")
    page_tail = tail and after <= 0
    semantic_page: dict[str, Any] | None = None
    if normalized_page_mode == "semantic":
        semantic_page = await asyncio.to_thread(
            read_semantic_timeline_page,
            session_id,
            after=after,
            semantic_before=semantic_before if semantic_before is not None else before,
            limit=limit,
            tail=page_tail,
        )
        events = semantic_page["events"]
        latest_seq = int(semantic_page["latest_seq"])
        event_count = int(semantic_page["semantic_total"])
        omitted_before = int(semantic_page["semantic_omitted_before"])
        omitted_after = int(semantic_page["semantic_omitted_after"])
    elif visible or compact:
        if after > 0 and before is None and not page_tail:
            events, latest_seq, event_count, omitted_before, omitted_after = await asyncio.to_thread(
                read_visible_events_after_page,
                session_id,
                after=after,
                limit=limit,
                compact=compact,
            )
        else:
            events, latest_seq, event_count, omitted_before, omitted_after = await asyncio.to_thread(
                read_visible_events_page,
                session_id,
                after=after,
                before=before,
                limit=limit,
                tail=page_tail,
                compact=compact,
            )
    else:
        events, latest_seq, event_count, omitted_before, omitted_after = await asyncio.to_thread(
            read_client_events_page,
            session_id,
            after=after,
            before=before,
            limit=limit,
            tail=page_tail,
        )
    await reconcile_idle_queue_session_from_snapshot(session_id)
    response = {
        "session": public_session(sess),
        "events": events,
        "queued_turns": await queued_turns_snapshot(session_id),
        "events_omitted_before": omitted_before,
        "events_omitted_after": omitted_after,
        "latest_seq": latest_seq,
        "event_count": event_count,
    }
    if semantic_page is not None:
        response.update({
            "semantic_item_count": semantic_page["semantic_item_count"],
            "semantic_total": semantic_page["semantic_total"],
            "semantic_omitted_before": semantic_page["semantic_omitted_before"],
            "semantic_omitted_after": semantic_page["semantic_omitted_after"],
            "next_semantic_before": semantic_page["next_semantic_before"],
        })
    return response


@router.patch("/api/sessions/{session_id}")
async def update_session(session_id: str, req: UpdateSessionRequest) -> dict[str, Any]:
    return await _update_session(session_id, req)


@router.get("/api/sessions/{session_id}/provider-commands")
async def get_session_provider_commands(
    session_id: str,
    refresh: bool = Query(default=False),
) -> dict[str, Any]:
    """Return one sanitized, session-scoped native command inventory."""

    session = STORE.sessions.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    snapshot, _inventory = await discover_session_provider_commands(
        session_id,
        dict(session),
        refresh=bool(refresh),
    )
    return snapshot


@router.post("/api/sessions/{session_id}/read")
async def mark_session_read(session_id: str, req: ReadSessionRequest) -> dict[str, Any]:
    ensure_session_not_initializing(session_id)
    sess = await STORE.mark_read(session_id, req.last_read_agent_event_seq)
    return {"session": public_session(sess)}


@router.post("/api/sessions/{session_id}/unread")
async def mark_session_unread(session_id: str) -> dict[str, Any]:
    ensure_session_not_initializing(session_id)
    sess = await STORE.mark_unread(session_id)
    return {"session": public_session(sess)}


@router.delete("/api/sessions/{session_id}")
async def delete_session(session_id: str, discard_updated_at: str | None = None) -> dict[str, Any]:
    async with session_lifecycle_lock(session_id):
        ensure_session_not_initializing(session_id)
        if (STORE.sessions.get(session_id) or {}).get("gemini_cleanup_pending_run_id"):
            # Keep the persisted uncertainty fence even though its runtime was
            # removed. Deleting the row must not erase the only cleanup record.
            raise HTTPException(409, "Retired provider cleanup was not confirmed. Verify the old runtime on the server before deleting this chat.")
        if session_id in DELETED_SESSION_TOMBSTONES:
            # A previous delete can have committed before its request was
            # canceled. Retry any tracked bridge retirement on idempotent
            # deletion instead of returning while stale work remains alive.
            await SIDE_QUESTIONS.close_session(session_id)
            SIDE_QUESTIONS.delete_history(session_id)
            await cancel_claude_stop_fence_retry(
                session_id,
                clear_fence=True,
            )
            deleted_jobs = await JOBS.delete_for_session(session_id)
            return {
                "ok": True,
                "deleted": False,
                "deleted_jobs": deleted_jobs,
            }
        if (
            stop_cleanup_in_progress(session_id)
        ):
            raise HTTPException(
                status_code=409,
                detail=(
                    "Explicit Stop cleanup is still finishing; the session "
                    "was not deleted. Retry shortly."
                ),
            )
        DELETING_SESSIONS.add(session_id)
        deleted = False
        try:
            if discard_updated_at is not None:
                await ensure_discardable_empty_session(session_id, discard_updated_at)
            cancel_generated_session_title(session_id)
            if (
                session_id in CLAUDE_STOP_FENCE_SESSIONS
                or session_id in CLAUDE_STOP_FENCE_RETRY_TASKS
                or any(
                    not task.done()
                    for task in CLAUDE_STOP_FENCE_ATTEMPT_TASKS.get(
                        session_id,
                        (),
                    )
                )
            ):
                fence_retry_stopped = await cancel_claude_stop_fence_retry(
                    session_id,
                    clear_fence=False,
                )
                if not fence_retry_stopped:
                    raise HTTPException(
                        status_code=409,
                        detail=(
                            "Claude Stop recovery is still finishing; the "
                            "session was not deleted. Retry shortly."
                        ),
                    )
            session = STORE.sessions.get(session_id) or {}
            provider_thread_ids = {
                thread_id
                for thread_id in (str(session_provider_id(session) or ""),)
                if thread_id
            }
            await SIDE_QUESTIONS.close_session(session_id)
            await cancel_codex_interactions(
                session_id,
                resolution="session_deleted",
            )
            await cancel_claude_interactions(
                session_id,
                resolution="session_deleted",
            )
            interactions_finished = await wait_for_session_tasks(
                CODEX_INTERACTION_HANDLER_TASKS,
                session_id,
            )
            claude_interactions_finished = await wait_for_session_tasks(
                CLAUDE_INTERACTION_HANDLER_TASKS,
                session_id,
            )
            if not interactions_finished or not claude_interactions_finished:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "Provider approval cleanup is still finishing; "
                        "the session was not deleted. Retry shortly."
                    ),
                )
            async with RUNS.run_now_request_lock:
                run_now_entry = RUNS.run_now_requests.get(session_id)
                run_now_task = (
                    run_now_entry[1]
                    if run_now_entry is not None
                    else None
                )

            await cancel_codex_native_actions(session_id)

            async with RUNS.active_lock:
                active = RUNS.active.get(session_id)
                busy = session_id in RUNS.busy

            if active and not active.get("codex_native_operation"):
                goal_pause_ok, _goal_paused, goal_pause_error = (
                    await pause_active_codex_goal_for_stop(session_id, active)
                )
                if not goal_pause_ok:
                    raise HTTPException(
                        status_code=409,
                        detail=goal_pause_error
                        or "The active Codex goal could not be paused. Retry deletion.",
                    )

            async with RUNS.active_lock:
                active = RUNS.active.get(session_id)
                busy = session_id in RUNS.busy
                if active and not active.get("codex_native_operation"):
                    active["stop_requested"] = True
                    if active.get("transport") == CLAUDE_TRANSPORT_AGENT_SDK:
                        active["claude_permissions_open"] = False
                    if active.get("run_id"):
                        RUNS.stopped_runs.add(str(active["run_id"]))
                elif busy and not active:
                    RUNS.stop_requests.add(session_id)

            if active and not active.get("codex_native_operation"):
                native_turn = active.get("codex_app_server_turn")
                proc = active.get("proc")
                if (
                    active.get("transport") == CODEX_TRANSPORT_APP_SERVER
                    and native_turn is not None
                    and getattr(native_turn, "turn_id", "")
                ):
                    with suppress(Exception):
                        await native_turn.interrupt()
                elif (
                    active.get("transport") == CLAUDE_TRANSPORT_AGENT_SDK
                ):
                    claude_sdk_run = active.get("claude_sdk_run")
                    if claude_sdk_run is not None:
                        try:
                            await interrupt_claude_sdk_run_bounded(
                                claude_sdk_run,
                            )
                        except Exception:
                            pass
                    # Delete is terminal, unlike Stop. Retire this one chat's
                    # SDK process immediately even when interrupt returned an
                    # acknowledgement: the provider stream is not required to
                    # emit a terminal Result after that acknowledgement.
                    if not await evict_claude_sdk_chat(
                        session_id,
                        force=True,
                    ):
                        raise HTTPException(
                            status_code=409,
                            detail=(
                                "The Claude chat process is still shutting "
                                "down; the session was not deleted. Retry "
                                "shortly."
                            ),
                        )
                elif proc:
                    await terminate_process_tree(proc)

            turns_finished = await wait_for_session_tasks(
                RUNS.turn_tasks,
                session_id,
            )
            if not turns_finished:
                async with RUNS.active_lock:
                    unsettled_active = RUNS.active.get(session_id)
                if (
                    unsettled_active
                    and unsettled_active.get("transport")
                    == CLAUDE_TRANSPORT_AGENT_SDK
                ):
                    # An SDK interrupt acknowledgement does not guarantee the
                    # message stream will emit a terminal Result. Deletion is
                    # terminal, so retire this chat's process and cancel its
                    # local consumer before deciding cleanup is stuck.
                    claude_evicted = await evict_claude_sdk_chat(
                        session_id,
                        force=True,
                    )
                    if claude_evicted:
                        current_task = asyncio.current_task()
                        unsettled_tasks = {
                            task
                            for task in tuple(
                                RUNS.turn_tasks.get(session_id) or ()
                            )
                            if (
                                task is not current_task
                                and not task.done()
                            )
                        }
                        for task in unsettled_tasks:
                            task.cancel()
                        if unsettled_tasks:
                            await asyncio.wait(
                                unsettled_tasks,
                                timeout=max(
                                    0.01,
                                    CODEX_SESSION_CLEANUP_TIMEOUT_SECONDS,
                                ),
                            )
                        turns_finished = await wait_for_session_tasks(
                            RUNS.turn_tasks,
                            session_id,
                            timeout=0.01,
                        )
            if not turns_finished:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "The active agent turn has not acknowledged interruption "
                        "yet; the session was not deleted. Retry shortly."
                    ),
                )
            if (
                isinstance(run_now_task, asyncio.Task)
                and run_now_task is not asyncio.current_task()
                and not run_now_task.done()
            ):
                _done, pending_run_now = await asyncio.wait(
                    {run_now_task},
                    timeout=max(0.01, CODEX_SESSION_CLEANUP_TIMEOUT_SECONDS),
                )
                if pending_run_now:
                    run_now_task.cancel()
                    _done, still_pending_run_now = await asyncio.wait(
                        {run_now_task},
                        timeout=max(
                            0.01,
                            CODEX_SESSION_CLEANUP_TIMEOUT_SECONDS,
                        ),
                    )
                    if still_pending_run_now:
                        raise HTTPException(
                            status_code=409,
                            detail=(
                                "Force Send cleanup is still finishing; "
                                "the session was not deleted. Retry shortly."
                            ),
                        )
            async with RUNS.run_now_request_lock:
                current_run_now = RUNS.run_now_requests.get(session_id)
                if (
                    current_run_now is not None
                    and current_run_now[1] is run_now_task
                ):
                    RUNS.run_now_requests.pop(session_id, None)
                for cache_key in tuple(RUN_NOW_COMPLETED_RESULTS):
                    if cache_key[0] == session_id:
                        RUN_NOW_COMPLETED_RESULTS.pop(cache_key, None)
            # A turn can bind its provider thread after deletion captured the
            # initial session record but before its task acknowledges stop.
            # The task registry is now drained, so this reread is stable.
            late_session = STORE.sessions.get(session_id) or {}
            late_provider_thread_id = str(
                session_provider_id(late_session) or ""
            )
            if late_provider_thread_id:
                provider_thread_ids.add(late_provider_thread_id)

            manager = existing_codex_app_server_manager(late_session or session)
            if manager is not None:
                for provider_thread_id in provider_thread_ids:
                    if manager.is_thread_loaded(provider_thread_id):
                        with suppress(CodexAppServerError):
                            await manager.unsubscribe_thread(
                                provider_thread_id
                            )
            claude_manager = PROVIDERS.claude_sdk_manager
            if claude_manager is not None:
                claude_evicted = await evict_claude_sdk_chat(
                    session_id,
                    force=True,
                    manager=claude_manager,
                )
                if not claude_evicted:
                    raise HTTPException(
                        status_code=409,
                        detail=(
                            "The Claude chat process is still shutting down; "
                            "the session was not deleted. Retry shortly."
                        ),
                    )
            if discard_updated_at is not None:
                await ensure_discardable_empty_session(session_id, discard_updated_at)
            # All fallible provider/task cleanup has completed. From here the
            # deletion is committed: revoke run authority first, then remove
            # queue/runtime ownership.
            async with RUNS.active_lock:
                deletion_run_ids = {
                    str(value or "")
                    for value in (
                        (RUNS.active.get(session_id) or {}).get("run_id"),
                        (RUNS.current_turns.get(session_id) or {}).get("run_id"),
                    )
                    if str(value or "")
                }
            for deletion_run_id in deletion_run_ids:
                await revoke_provider_authority(deletion_run_id)
            steering_waiter = RUNS.steering_waits.pop(session_id, None)
            if steering_waiter is not None and not steering_waiter[2].done():
                steering_waiter[2].cancel()
            async with RUNS.queue_lock:
                RUNS.queued.pop(session_id, None)
                RUNS.run_now.pop(session_id, None)
                RUNS.steering.discard(session_id)
            async with RUNS.active_lock:
                RUNS.active.pop(session_id, None)
                RUNS.busy.discard(session_id)
                RUNS.current_turns.pop(session_id, None)
                RUNS.stop_requests.discard(session_id)
            if provider_thread_ids:
                async with CODEX_APP_SERVER_THREAD_LRU_LOCK:
                    for provider_thread_id in provider_thread_ids:
                        CODEX_APP_SERVER_THREAD_LRU.pop(
                            provider_thread_id,
                            None,
                        )
                        CODEX_APP_SERVER_THREAD_PIN_COUNTS.pop(
                            provider_thread_id,
                            None,
                        )
                        CODEX_APP_SERVER_PINNED_THREADS.discard(
                            provider_thread_id
                        )
                        CODEX_APP_SERVER_INVALIDATED_THREADS.discard(
                            provider_thread_id
                        )
            for provider_thread_id in provider_thread_ids:
                CODEX_INTERACTIVE_CONTROL_THREAD_COUNTS.pop(
                    provider_thread_id,
                    None,
                )
                CODEX_INTERACTIVE_CONTROL_THREADS.discard(provider_thread_id)
                if CODEX_THREAD_SESSION_INDEX.get(provider_thread_id) == session_id:
                    CODEX_THREAD_SESSION_INDEX.pop(provider_thread_id, None)
                for key in tuple(CODEX_APPROVAL_ITEM_CACHE):
                    if key[0] == provider_thread_id:
                        CODEX_APPROVAL_ITEM_CACHE.pop(key, None)
            CODEX_GOAL_SYNC_GENERATIONS.pop(session_id, None)
            for provider_thread_id, owner_session_id in tuple(
                CODEX_QUARANTINED_GOAL_THREADS.items()
            ):
                if owner_session_id == session_id:
                    CODEX_QUARANTINED_GOAL_THREADS.pop(
                        provider_thread_id,
                        None,
                    )
            with suppress(Exception):
                await asyncio.to_thread(kill_terminal_session, session_id)
            async def finish_committed_delete() -> dict[str, Any]:
                async with event_delivery_lock(session_id):
                    if discard_updated_at is not None:
                        await ensure_discardable_empty_session(session_id, discard_updated_at)
                    committed_deleted = await STORE.delete(session_id)
                    DELETED_SESSION_TOMBSTONES.add(session_id)
                    DELETING_SESSIONS.discard(session_id)
                SIDE_QUESTIONS.delete_history(session_id)
                if session_id in CLAUDE_STOP_FENCE_SESSIONS:
                    # No retry/attempt task remains after the bounded
                    # pre-delete join above. Clear synchronously after the
                    # durable delete so cancellation cannot strand a local
                    # blocker.
                    CLAUDE_STOP_FENCE_SESSIONS.discard(session_id)
                    RUNS.maintenance.discard(session_id)
                    CLAUDE_STOP_FENCE_ATTEMPT_LOCKS.pop(session_id, None)
                if committed_deleted:
                    await broadcast_committed_emergency_removal(session_id)
                committed_deleted_jobs = await JOBS.delete_for_session(
                    session_id
                )
                return {
                    "ok": True,
                    "deleted": committed_deleted,
                    "deleted_jobs": committed_deleted_jobs,
                }

            delete_completion_task = asyncio.create_task(
                finish_committed_delete()
            )
            try:
                delete_result = await asyncio.shield(delete_completion_task)
            except asyncio.CancelledError as cancellation:
                # STORE.delete owns an un-cancellable filesystem replacement.
                # If this request disappears after that write starts, retain
                # ownership through the tombstone, file/sequence cleanup and
                # job removal so no runnable work survives a deleted chat.
                delete_result = await join_task_despite_caller_cancellation(
                    delete_completion_task
                )
                deleted = bool(delete_result.get("deleted"))
                raise cancellation
            deleted = bool(delete_result.get("deleted"))
            return delete_result
        except BaseException:
            if deleted:
                DELETED_SESSION_TOMBSTONES.add(session_id)
            elif session_id in CLAUDE_STOP_FENCE_SESSIONS:
                schedule_claude_stop_fence_retry(session_id)
            DELETING_SESSIONS.discard(session_id)
            raise


@router.post("/api/sessions/{session_id}/fork")
async def fork_session(session_id: str, req: ForkSessionRequest) -> dict[str, Any]:
    if retired := retired_session_runtime_message(STORE.sessions.get(session_id) or {}):
        raise HTTPException(410, retired)
    cleanup_state: dict[str, str | None] = {}
    async with session_lifecycle_lock(session_id):
        try:
            ensure_session_not_deleting(session_id)
            async with RUNS.active_lock:
                active = RUNS.active.get(session_id) or {}
                running = session_id in RUNS.busy or bool(active)
                active_run_id = str(active.get("run_id") or "")
                latest_sequence = (STORE.sessions.get(session_id) or {}).get("latest_event_seq")
            snapshot = None
            if running:
                parent = STORE.sessions.get(session_id) or {}
                if parent.get("backend", DEFAULT_BACKEND) not in {BACKEND_CLAUDE, BACKEND_CODEX}:
                    raise HTTPException(status_code=409, detail="This backend cannot fork a running chat safely; wait for its turn to finish.")
                snapshot = completed_fork_events(
                    await asyncio.to_thread(lambda: list(iter_session_events(session_id))),
                    active_run_id=active_run_id,
                    through_sequence=latest_sequence if isinstance(latest_sequence, int) else None,
                )
            # Holding the lifecycle lock prevents a new turn or deletion from
            # crossing the provider snapshot and local-history snapshot.
            return await _fork_session_locked(
                session_id,
                req,
                cleanup_state=cleanup_state,
                **({"completed_snapshot": snapshot} if snapshot is not None else {}),
            )
        except BaseException:
            await cleanup_aborted_session_fork(cleanup_state)
            raise


@router.get("/api/sessions/{session_id}/runs/{run_id}/trace")
async def get_session_run_trace(
    session_id: str,
    run_id: str,
    anchor_seq: int | None = Query(default=None, ge=1),
    after_seq: int = Query(default=0, ge=0),
    limit: int = Query(default=160, ge=1, le=1_000),
) -> dict[str, Any]:
    if session_id not in STORE.sessions:
        raise HTTPException(status_code=404, detail="session not found")
    try:
        return await asyncio.to_thread(
            read_indexed_run_trace,
            session_id,
            run_id,
            anchor_seq=anchor_seq,
            after_seq=after_seq,
            limit=limit,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc.args[0])) from exc


@router.websocket("/api/sessions/{session_id}/events")
async def session_events(
    session_id: str,
    ws: WebSocket,
    after: int = 0,
    visible: bool | None = None,
    reasoning_stream: bool = False,
    reasoning_text: bool = False,
    liveness: bool = False,
    history_projection: bool = False,
) -> None:
    selected_subprotocol = websocket_endpoint_subprotocol(
        ws,
        EVENTS_WEBSOCKET_PROTOCOL,
    )
    if not websocket_authorized(ws):
        await ws.accept(subprotocol=selected_subprotocol)
        await ws.close(code=4401)
        return
    if session_id not in STORE.sessions:
        await ws.accept(subprotocol=selected_subprotocol)
        await ws.close(code=4404)
        return
    await ws.accept(subprotocol=selected_subprotocol)
    schedule_provider_history_sync(STORE.sessions[session_id])
    try:
        if not await HUB.reserve(session_id, ws):
            await close_event_websocket_over_capacity(ws)
            return
        cursor = max(0, int(after or 0))
        # Omitted ``visible`` is the legacy protocol (including released iOS
        # clients): it receives every client-safe event rather than the newer
        # timeline-visible subset. Both protocols still require complete,
        # sequence-bound catch-up; the old single 500-row read silently skipped
        # the rest of a long offline gap.
        catchup_visible = visible is True
        # A durable import holds this lock until its source-proven projection
        # is ready. Reading its fsynced rows earlier can replay raw scheduled
        # inputs before this socket is registered for the repaired broadcast.
        async with event_delivery_lock(session_id):
            boundary = await asyncio.to_thread(
                last_event_seq_from_file,
                events_path(session_id),
            )
        while True:
            cursor = await send_event_catchup(
                session_id,
                ws,
                after=cursor,
                through=boundary,
                visible=catchup_visible,
            )
            if cursor < boundary:
                # Never register a socket with an acknowledged hole. A short
                # scan means the log could not substantiate its own boundary;
                # live delivery cannot repair that missing historical range.
                raise RuntimeError(
                    "event catch-up could not reach its stable boundary"
                )

            # No socket I/O occurs while holding the delivery lock. If an
            # append raced the page stream, capture its new fixed boundary,
            # release the lock, and drain it in the next pass. Registration is
            # performed only in a lock-held instant where no gap remains, so a
            # later append observes the subscriber before it broadcasts.
            activated = False
            reasoning_snapshot = None
            async with event_delivery_lock(session_id):
                gap_boundary = await asyncio.to_thread(
                    last_event_seq_from_file,
                    events_path(session_id),
                )
                if cursor >= gap_boundary:
                    activated = await HUB.register_accepted(session_id, ws, **(
                        {"reasoning_stream": True, **({"reasoning_text": True} if reasoning_text else {})} if reasoning_stream else {}))
                    if activated and reasoning_stream:
                        reasoning_snapshot = reasoning_summary_stream_snapshot(session_id)
                        if not reasoning_text:
                            reasoning_snapshot["items"] = [item for item in reasoning_snapshot["items"]
                                if item.get("phase") != "reasoning"]
            if cursor >= gap_boundary:
                if not activated:
                    # This should be unreachable after a successful
                    # reservation, but retain retryable overload semantics if
                    # a future lifecycle path releases it early. Socket I/O
                    # remains outside the event-delivery lock.
                    await close_event_websocket_over_capacity(ws)
                    return
                if reasoning_snapshot is not None:
                    await asyncio.wait_for(ws.send_json(reasoning_snapshot), timeout=WEBSOCKET_SEND_TIMEOUT_SECONDS)
                if liveness:
                    await send_event_stream_status(ws, session_id, "ready")
                if history_projection:
                    # A proof may finish between the HTTP page and subscription.
                    # The revision closes that gap without replaying old rows.
                    await asyncio.wait_for(ws.send_json(history_projection_notice(session_id)), timeout=WEBSOCKET_SEND_TIMEOUT_SECONDS)
                break
            boundary = gap_boundary
        while True:
            if not liveness:
                await ws.receive_text()
                continue
            try:
                await asyncio.wait_for(ws.receive_text(), timeout=EVENT_STREAM_HEARTBEAT_SECONDS)
            except asyncio.TimeoutError:
                # No history scan, provider call, or sequence acknowledgement.
                # This confirms that the application event path is still alive.
                await send_event_stream_status(ws, session_id, "heartbeat")
    except WebSocketDisconnect:
        pass
    except (asyncio.TimeoutError, ConnectionError):
        await close_event_websocket_after_delivery_failure(ws)
    finally:
        await HUB.unsubscribe(session_id, ws)


@router.get("/api/sessions/{session_id}/diffs/{run_id}")
async def get_turn_code_diff(session_id: str, run_id: str) -> FileResponse:
    if session_id not in STORE.sessions:
        raise HTTPException(status_code=404, detail="session not found")
    if not re.fullmatch(r"[A-Za-z0-9._-]+", run_id or ""):
        raise HTTPException(status_code=404, detail="code diff not found")
    patch_path = code_diffs_dir(session_id) / f"{run_id}.patch"
    if not patch_path.is_file():
        raise HTTPException(status_code=404, detail="code diff not found")
    return FileResponse(
        patch_path,
        media_type="text/x-diff; charset=utf-8",
        filename=f"{run_id}.patch",
        content_disposition_type="inline",
    )
