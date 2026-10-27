"""The turn engine: starting turns, running providers, finishing and starting the next queued turn."""
from __future__ import annotations

import asyncio
from collections import deque
from contextlib import suppress
import errno
import hashlib
import json
import logging.handlers
import os
from pathlib import Path
import re
import secrets
import time
from typing import Any, Literal
import uuid

from fastapi import HTTPException
from fastapi.responses import JSONResponse

from claude_background_reconciliation import (
    pending_task_reconciliations,
    reconciliation_envelope,
    SESSION_FIELD as CLAUDE_BACKGROUND_SESSION_FIELD,
)
from hyadex.core.config import (
    BACKEND_CLAUDE,
    BACKEND_CODEX,
    CLAUDE_SDK_IDLE_TIMEOUT_SECONDS,
    CLAUDE_SDK_IDLE_WARN_SECONDS,
    CLAUDE_SDK_INTERACTIVE_CLIENT_CAPABILITY,
    CLAUDE_SDK_POST_ACK_FIRST_ACTIVITY_TIMEOUT_SECONDS,
    CLAUDE_SDK_PRE_ACK_TIMEOUT_SECONDS,
    CLAUDE_SDK_TURN_TIMEOUT_SECONDS,
    CLAUDE_TRANSPORT,
    CLAUDE_TRANSPORT_AGENT_SDK,
    CLAUDE_TRANSPORT_PRINT,
    CODEX_APP_SERVER_AMBIGUOUS_ACCEPT_SECONDS,
    CODEX_APP_SERVER_FIRST_ACTIVITY_TIMEOUT_SECONDS,
    CODEX_APP_SERVER_LIFECYCLE_TIMEOUT_SECONDS,
    CODEX_APP_SERVER_TIMEOUT_SECONDS,
    CODEX_APP_SERVER_TOOL_OUTPUT_MAX_CHARS,
    CODEX_APPROVAL_REVIEWERS,
    CODEX_BIN,
    CODEX_DEFAULT_APPROVALS_REVIEWER,
    CODEX_DEFAULT_PERMISSION_PROFILE,
    CODEX_DEFAULT_SANDBOX_MODE,
    CODEX_GOAL_CONTROL_TIMEOUT_SECONDS,
    CODEX_GOALS_ENABLED,
    CODEX_INTERACTIVE_CLIENT_CAPABILITY,
    CODEX_NONINTERACTIVE_APPROVAL_POLICY,
    CODEX_RESUME_ACTIVITY_TIMEOUT_SECONDS,
    CODEX_SANDBOX_POLICY_TYPES,
    CODEX_TRANSPORT,
    CODEX_TRANSPORT_APP_SERVER,
    CODEX_TRANSPORT_AUTO,
    CODEX_TRANSPORT_EXEC,
    DEFAULT_BACKEND,
    DEFAULT_CWD,
    HEALTH_QUEUE_RECONCILE_INTERVAL_SECONDS,
    IDLE_KILL_SECONDS,
    IDLE_WARN_SECONDS,
    JOB_BUSY_RETRY_SECONDS,
    PROCESS_STREAM_LIMIT,
    RETIRED_TURN_PURPOSES,
)
from hyadex.core.event_log import (
    _build_timeline_index_locked,
    client_safe_event,
    evict_timeline_index_cache_locked,
    fork_internal_run_ids,
    is_fork_internal_event,
    reasoning_summary_stream_snapshot,
    scheduled_job_occurrence_key,
    timeline_index_cached_entry,
)
from hyadex.core.paths import codex_manifest_path, events_path, existing_cwd, now_iso
from hyadex.core.processes import process_group_for_pid, terminate_process_tree
from hyadex.core.queue import (
    append_durable_event,
    append_durable_event_batch,
    collect_manifest,
    collect_recent_leftover_manifests,
    fence_native_steer_delivery,
    force_send_task_owner,
    log_queue_promotion_fence,
    mark_native_steer_accepted,
    queue_promotion_fence,
    queued_turn_run_metadata,
    queued_turn_unqueued_payload,
    request_fields_set,
    requeue_turn_front,
    restore_missing_durable_force_send_row,
    scan_queued_turns_from_events,
    should_schedule_queue_after_finish,
    stop_idle_claude_background_subagents_bounded,
    terminally_discard_queued_turn,
    turn_steering_lineage,
    wait_for_queue_recovery_admission,
    watch_manifest_artifacts,
)
from hyadex.core.runs import RUNS
from hyadex.core.server import SERVER
from hyadex.core.server_logging import concise_error_message
from hyadex.core.sessions import (
    append_event,
    CLAUDE_STOP_FENCE_SESSIONS,
    codex_goal_time_budget_remaining,
    codex_output_text,
    codex_runtime_settings,
    CODEX_SUBAGENT_INDEX_LOCK,
    CODEX_SUBAGENT_LIVE_GENERATIONS,
    CODEX_SUBAGENT_LIVE_MANAGERS,
    CODEX_SUBAGENT_SESSION_INDEX,
    CODEX_SUBAGENT_STATE,
    effective_claude_permission_mode,
    effective_provider_jobs_access,
    ensure_session_not_deleting,
    event_delivery_lock,
    event_files_belong_to_session,
    event_output_text,
    last_event_seq_from_file,
    managed_server_restart_blocks_work,
    mark_provider_context_usage_unavailable,
    preview_session_runtime_update,
    public_session,
    refresh_native_session_title,
    register_session_task,
    remember_codex_native_compaction_terminal,
    retired_session_runtime_message,
    revoke_provider_authority,
    revoke_terminal_run_authority,
    schedule_generated_session_title,
    server_lifecycle_admission_blocker,
    session_lifecycle_lock,
    STORE,
    timeline_index_session_lock,
)
from hyadex.core.state import (
    DELETED_SESSION_TOMBSTONES,
    DELETING_SESSIONS,
    EVENT_SEQ_CACHE,
    HEALTH_QUEUE_RECONCILE_STATE,
    LIVE_STDOUT_MAX_LINE_CHARS,
    LIVE_STDOUT_MAX_LINES,
    QUEUE_RECOVERY_READY,
    QUEUE_RETRY_TASKS,
    REASONING_SUMMARY_STREAM_LAST_SENT,
    REASONING_SUMMARY_STREAM_PENDING,
    REASONING_SUMMARY_STREAMS,
    RUN_NOW_IDLE_OWNER_CLEANUP_TIMEOUT_SECONDS,
    RUN_NOW_IDLE_OWNER_TIMEOUT_SECONDS,
    RUN_NOW_PROVIDER_HANDOFF_TIMEOUT_SECONDS,
    RUN_TRACE_EVENT_TYPES,
    SESSION_SNAPSHOT_RECONCILE_AT,
    TIMELINE_INDEX_CACHE_LOCK,
    TURN_RECEIPT_TASKS,
)
from hyadex.core.util import (
    clean_assistant_text,
    compact_memory_text,
    SkillSelection,
    TransientAdmissionWait,
)
from hyadex.core.wiring import WIRING
from hyadex.features.files import (
    capture_git_baseline,
    discard_owned_git_baselines,
    publish_turn_code_diff,
    run_event_metadata,
    validate_session_file_ids,
)
from hyadex.features.history import history_file_stamp
from hyadex.features.side_questions import codex_app_server_service_tier, CODEX_GOALS_RECONFIGURING
from hyadex.features.terminal import scrub_tmux_global_secret_environment
from hyadex.features.usage import HUB, join_task_despite_caller_cancellation
from hyadex.jobs.context import VALID_JOB_CONTEXT_MODES
from hyadex.providers.authority import (
    agent_runner_env,
    issue_provider_authority,
    NativeSteerHandoffError,
    provider_authority_runtime_env,
)
from hyadex.providers.claude.history import session_provider_id
from hyadex.providers.claude.runtime import (
    acknowledge_claude_background_reconciliation,
    build_claude_cmd,
    cancel_claude_interactions,
    claude_empty_turn_failure_message,
    CLAUDE_GOAL_PENDING,
    claude_provider_id_for_session,
    claude_result_error,
    claude_run_has_pending_interaction,
    claude_runtime_snapshot,
    claude_sdk_manager,
    claude_sdk_result_details,
    CLAUDE_STOP_FENCE_RETRY_DELAYS_SECONDS,
    CLAUDE_STOP_FENCE_RETRY_TASKS,
    claude_supports_no_session_persistence,
    ClaudeGoalRequest,
    interrupt_claude_sdk_run_bounded,
    is_expected_claude_interruption_result,
    mark_claude_sdk_turn_ready_after_ack,
    mark_provider_turn_ready,
    persist_claude_background_task_receipts,
    project_claude_sdk_message,
    refresh_claude_goal,
    require_claude_goal_session,
    resolve_claude_resume_provider,
    sample_claude_context_usage,
    stop_cleanup_in_progress,
    tool_changed_paths,
    TurnRequest,
)
from hyadex.providers.claude.sdk import (
    ClaudeSDKQueryError,
    ClaudeSDKSupervisorClosed,
    ClaudeSDKSupervisorManager,
    ClaudeSDKUnavailable,
)
from hyadex.providers.codex.app_server import (
    CodexAppServerDisconnected,
    CodexAppServerError,
    CodexAppServerManager,
    CodexAppServerProtocolError,
    CodexAppServerRequestError,
    CodexAppServerSubscriptionClosed,
)
from hyadex.providers.codex.history import find_codex_history
from hyadex.providers.codex.runtime import (
    acquire_codex_control_thread,
    acquire_codex_run_thread,
    apply_codex_goal_time_budget_limit,
    archive_rejected_codex_session,
    build_claude_sdk_options,
    build_codex_cmd,
    claim_codex_control_terminal_publication,
    codex_app_server_changed_paths,
    codex_app_server_manager,
    codex_app_server_reasoning_plaintext,
    codex_app_server_reasoning_summary,
    codex_app_server_tool,
    codex_app_server_tool_output,
    codex_call_tool,
    codex_compaction_error_message,
    codex_compaction_retry_mode,
    codex_control_http_error,
    CODEX_CONTROL_TERMINAL_FENCES,
    codex_exec_agent_message,
    codex_exec_raw_event_text,
    codex_native_archive_rejection,
    codex_output_exit_code,
    codex_provider_command_turn_input,
    codex_provider_mcp_run_proof,
    codex_reasoning_text,
    codex_result_error,
    codex_turn_matches_client_user_message,
    CodexExecMessageBuffer,
    CodexGoalRequest,
    commit_codex_goal_steer,
    current_codex_token_usage_snapshot,
    discover_session_provider_commands,
    effective_codex_approval_policy,
    is_codex_app_server_retry_notice,
    is_codex_compaction_failure,
    is_codex_resume_failure,
    is_silent_codex_completion,
    mark_codex_exec_context_usage_unavailable,
    migrate_retired_codex_selection,
    prepare_codex_login_turn,
    project_codex_notification,
    quarantine_codex_goal_thread,
    record_codex_token_usage,
    register_codex_native_action,
    release_codex_control_slot,
    release_codex_interactive_control_lease,
    resolve_provider_command_selection,
    retain_codex_goal_run_owner,
    rollover_codex_provider_session,
    send_codex_goal_steer,
    should_recover_codex_resume,
    stop_codex_goal_resume,
    turn_start_blocker,
    unpin_codex_app_server_thread,
    validate_codex_goal_resume_budget,
)
from hyadex.providers.registry import (
    build_turn_provider_prompt,
    build_user_provider_prompt,
    ensure_runtime_available,
    interactive_backend_supported,
    issue_native_steer_provider_authority,
    normalize_steering_lineage,
    persist_run_provider_session,
    provider_command_prompt_matches,
    provider_context_goal_is_exhausted,
    provider_turn_may_raise_emergency,
    ProviderCommandSelectionInvalid,
    ProviderCommandSelectionUnavailable,
    queued_turn_uses_retired_routing,
    record_runtime_failure,
    record_runtime_success,
    redacted_provider_argv,
    standalone_provider_session,
    turn_slot_is_owned,
)
from hyadex.providers.runtime import (
    assert_provider_user_message_unchanged,
    ProviderTurnPayload,
    validate_provider_runtime_env,
)
from hyadex.providers.subagents import (
    claude_sdk_field,
    claude_sdk_type,
    codex_child_status_from_thread,
    codex_collaboration_states,
    codex_collaboration_thread_ids,
    codex_subagent_thread_identity,
    discard_unused_claude_stop_fence_attempt_lock,
    evict_claude_sdk_chat,
    normalize_subagent_status,
    schedule_codex_subagent_finalization,
)
from provider_commands import ProviderCommandRecord

logger = logging.getLogger("hyadex-server")


def explicit_stop_session_ids() -> set[str]:
    """Snapshot chats whose detached Stop cleanup still blocks replacement."""

    outer = {
        session_id
        for session_id, operation in RUNS.explicit_stops.items()
        if not operation.done()
    }
    inner = {
        session_id
        for session_id, tasks in RUNS.detached_stop_tasks_by_session.items()
        if any(not task.done() for task in tasks)
    }
    return outer | inner


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


def build_memory_augmented_prompt(label: str, memory: str, prompt: str) -> str:
    """Wrap fallback memory with an exact character length for collision-free recovery."""
    return (
        f"[{label}; chars={len(memory)}]\n"
        f"{memory}\n"
        f"[End {label}]\n\n"
        "[Current user prompt]\n"
        f"{prompt}"
    )


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


async def recover_queued_turns_after_start() -> tuple[int, int]:
    QUEUE_RECOVERY_READY.clear()
    sessions = [(sid, dict(sess)) for sid, sess in STORE.sessions.items()]
    # Recover small/checkpointed chats first. A legacy giant only fences itself.
    def recovery_cost(item):
        path = events_path(item[0])
        try:
            checkpoint = json.loads(path.with_name("queue-recovery.json").read_text())
            return max(0, path.stat().st_size - int(checkpoint.get("offset", 0)))
        except (OSError, ValueError, TypeError):
            return path.stat().st_size if path.exists() else 0
    sessions = await asyncio.to_thread(lambda: sorted(sessions, key=recovery_cost))
    rebuilt = scheduled = 0
    for sid, sess in sessions:
        count, started = await _recover_queued_turns_for_sessions([(sid, sess)])
        rebuilt += count
        scheduled += started
        QUEUE_RECOVERY_READY.add(sid)
    return rebuilt, scheduled


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


def jsonable_active_run(session_id: str, active: dict[str, Any]) -> dict[str, Any]:
    started = float(active.get("started_at") or time.time())
    return {
        "session_id": session_id,
        "run_id": active.get("run_id"),
        "backend": active.get("backend"),
        "transport": active.get("transport"),
        "pid": active.get("pid"),
        "pgid": active.get("pgid"),
        "cwd": active.get("cwd"),
        "argv": active.get("argv") or [],
        "started_at": active.get("started_at_iso"),
        "elapsed_seconds": int(max(0, time.time() - started)),
        "stop_requested": bool(active.get("stop_requested")),
        "stdout_total_lines": int(active.get("stdout_total_lines") or 0),
        "stdout_updated_at": active.get("stdout_updated_at"),
    }


async def active_run_summaries() -> list[dict[str, Any]]:
    async with RUNS.active_lock:
        return [jsonable_active_run(session_id, active) for session_id, active in RUNS.active.items()]


async def append_active_stdout(session_id: str, text: str) -> None:
    line = text.rstrip("\r\n")
    if len(line) > LIVE_STDOUT_MAX_LINE_CHARS:
        line = line[:LIVE_STDOUT_MAX_LINE_CHARS] + "\n... stdout line truncated ..."
    async with RUNS.active_lock:
        active = RUNS.active.get(session_id)
        if not active:
            return
        tail = active.get("stdout_tail")
        if tail is None:
            tail = deque(maxlen=LIVE_STDOUT_MAX_LINES)
            active["stdout_tail"] = tail
        tail.append(line)
        active["stdout_total_lines"] = int(active.get("stdout_total_lines") or 0) + 1
        active["stdout_updated_at"] = now_iso()


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


async def bind_active_turn(
    session_id: str,
    run_id: str,
    record: dict[str, Any],
) -> tuple[bool, bool]:
    """Install a provider ACTIVE record only while its reservation is owned.

    Provider startup awaits can outlive a hard Stop. By the time a subprocess
    or shared transport becomes ready, the old reservation may be gone and a
    queued successor may already own the chat. Never recreate BUSY or replace
    that successor from the delayed startup path.
    """

    async with RUNS.active_lock:
        current_turn = RUNS.current_turns.get(session_id)
        current_run_id = (
            str(current_turn.get("run_id") or "")
            if isinstance(current_turn, dict)
            else ""
        )
        active = RUNS.active.get(session_id)
        active_run_id = (
            str(active.get("run_id") or "")
            if isinstance(active, dict)
            else ""
        )
        if (
            session_id not in RUNS.busy
            or current_run_id != run_id
            or active_run_id not in {"", run_id}
        ):
            return False, False
        stop_requested = (
            session_id in RUNS.stop_requests or run_id in RUNS.stopped_runs
        )
        if stop_requested:
            RUNS.stop_requests.discard(session_id)
            RUNS.stopped_runs.add(run_id)
        record["stop_requested"] = stop_requested
        RUNS.active[session_id] = record
        return True, stop_requested


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


async def finalize_owned_turn_finished(
    session_id: str,
    run_id: str,
    *,
    stopped: bool,
    payload: dict[str, Any],
) -> bool:
    """Publish one terminal event and drain only for the releasing run owner."""

    drain_queue = should_schedule_queue_after_finish(session_id, stopped)
    released = await release_turn_slot(
        session_id,
        expected_run_id=run_id,
    )
    try:
        if released:
            await append_turn_finished_event(session_id, payload)
    finally:
        RUNS.run_metadata.pop(run_id, None)
        RUNS.stopped_runs.discard(run_id)
        if released and drain_queue:
            schedule_next_queued_turn(session_id)
    return released


async def clear_active_process(
    session_id: str,
    *,
    expected_run_id: str,
) -> bool:
    """Clear only the process record still owned by ``expected_run_id``.

    A hard Stop can release the old slot and promote a successor while the
    cancelled subprocess runner is still unwinding. Its delayed ``finally``
    must not erase the successor's ACTIVE record.
    """

    async with RUNS.active_lock:
        active = RUNS.active.get(session_id)
        if (
            not isinstance(active, dict)
            or str(active.get("run_id") or "") != expected_run_id
        ):
            return False
        RUNS.active.pop(session_id, None)
        return True


def read_indexed_run_trace(
    session_id: str,
    run_id: str,
    *,
    anchor_seq: int | None = None,
    after_seq: int = 0,
    limit: int = 160,
) -> dict[str, Any]:
    """Read trace detail only within byte bounds recorded by the main index."""
    clean_run_id = str(run_id or "").strip()
    if not clean_run_id:
        raise KeyError("run trace not found")
    normalized_after = max(0, int(after_seq or 0))
    bounded_limit = max(1, min(int(limit or 160), 1_000))
    lock = timeline_index_session_lock(session_id)
    with lock:
        _build_timeline_index_locked(session_id)
        timeline_cache = timeline_index_cached_entry(session_id, touch=True) or {}
        run_records = timeline_cache.get("run_history_records") or {}
        candidate_keys = list(
            (timeline_cache.get("run_history_keys_by_run_id") or {}).get(
                clean_run_id
            )
            or [f"run:{clean_run_id}"]
        )
        candidates = [
            run_records[key]
            for key in candidate_keys
            if isinstance(run_records.get(key), dict)
        ]
        normalized_anchor = max(0, int(anchor_seq or 0))
        if normalized_anchor > 0:
            record = next(
                (
                    candidate
                    for candidate in sorted(
                        candidates,
                        key=lambda item: (
                            int(item.get("start_seq") or 0),
                            int(item.get("end_seq") or 0),
                        ),
                        reverse=True,
                    )
                    if int(candidate.get("start_seq") or 0)
                    <= normalized_anchor
                    <= int(candidate.get("end_seq") or 0)
                ),
                None,
            )
        else:
            record = candidates[-1] if candidates else None
        if not isinstance(record, dict):
            raise KeyError("run trace not found")
        bounds = {
            "start_offset": max(0, int(record.get("start_offset") or 0)),
            "end_offset": max(0, int(record.get("end_offset") or 0)),
            "end_seq": max(0, int(record.get("end_seq") or 0)),
            "occurrence_key": str(record.get("occurrence_key") or "").strip(),
        }
        fork_internal_run_ids = set(
            timeline_cache.get("fork_internal_run_ids") or ()
        )
    with TIMELINE_INDEX_CACHE_LOCK:
        evict_timeline_index_cache_locked()

    path = events_path(session_id)
    if not path.exists():
        raise KeyError("run trace not found")
    selected: list[dict[str, Any]] = []
    has_more = False
    with path.open("rb") as source:
        source.seek(bounds["start_offset"])
        while source.tell() < bounds["end_offset"]:
            line_start = source.tell()
            raw_line = source.readline()
            if not raw_line or line_start >= bounds["end_offset"]:
                break
            if not raw_line.endswith(b"\n"):
                break
            try:
                event = json.loads(raw_line.decode("utf-8", "replace"))
            except Exception:
                continue
            seq = int(event.get("seq") or 0)
            if (
                seq <= normalized_after
                or str(event.get("run_id") or "").strip() != clean_run_id
                or (
                    bounds["occurrence_key"]
                    and scheduled_job_occurrence_key(event)
                    != bounds["occurrence_key"]
                )
                or str(event.get("type") or "") not in RUN_TRACE_EVENT_TYPES
                or is_fork_internal_event(event, fork_internal_run_ids)
                or not event_files_belong_to_session(event, session_id)
            ):
                continue
            if len(selected) >= bounded_limit:
                has_more = True
                break
            selected.append(client_safe_event(event))

    next_after = (
        int(selected[-1].get("seq") or normalized_after)
        if has_more and selected
        else max(normalized_after, bounds["end_seq"])
    )
    return {
        "events": selected,
        "has_more": has_more,
        "next_after": next_after,
    }


async def append_turn_finished_event(session_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    # Persist before the terminal notification. Existing session-list refresh /
    # polling paths pick up the title without a new protocol or synthetic turn.
    try:
        if (
            not payload.get("imported")
            and not str(payload.get("run_id") or "").startswith("import_")
        ):
            await refresh_native_session_title(session_id)
        event = await append_event(session_id, "turn_finished", payload)
        # Optional metadata work cannot delay/fail the terminal event or add a
        # hidden user turn. Its own task owns and reaps independent providers.
        try:
            schedule_generated_session_title(session_id, event)
        except Exception as exc:
            logger.debug("title scheduling skipped error=%s", type(exc).__name__)
    except BaseException as write_error:
        # Execution has ended even when its terminal record cannot be written
        # (for example ENOSPC). Do not invent a persisted event or forward a
        # result, but do retire this exact run's authority and cached activity.
        run_id = str(payload.get("run_id") or "")
        if run_id:
            async def cleanup_unrecorded_terminal() -> None:
                async with RUNS.active_lock:
                    owners = {str((record or {}).get("run_id") or "") for record in
                              (RUNS.active.get(session_id), RUNS.current_turns.get(session_id))}
                    session = STORE.sessions.get(session_id)
                    cached = session.get("active_run") if isinstance(session, dict) else None
                    if (owners <= {"", run_id} and isinstance(cached, dict)
                            and str(cached.get("run_id") or "") == run_id):
                        session.pop("active_run", None)
                await revoke_provider_authority(run_id)

            cleanup = asyncio.create_task(cleanup_unrecorded_terminal())
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                with suppress(Exception):
                    await join_task_despite_caller_cancellation(cleanup)
            except Exception:
                logger.exception("Exact-run cleanup failed after terminal storage failure")
        logger.error("Turn completion recording did not finish (error_type=%s, errno=%s)",
                     type(write_error).__name__, getattr(write_error, "errno", None))
        raise
    await revoke_terminal_run_authority(session_id, event)
    return event


async def retry_claude_stop_fence(session_id: str) -> None:
    """Retry an idle Claude child-stop marker until it is durable or obsolete."""

    attempt = 0
    while True:
        delay = CLAUDE_STOP_FENCE_RETRY_DELAYS_SECONDS[
            min(attempt, len(CLAUDE_STOP_FENCE_RETRY_DELAYS_SECONDS) - 1)
        ]
        attempt += 1
        await asyncio.sleep(max(0.0, float(delay)))

        session = STORE.sessions.get(session_id)
        async with RUNS.active_lock:
            if session_id not in CLAUDE_STOP_FENCE_SESSIONS:
                return
            if (
                not isinstance(session, dict)
                or str(session.get("backend") or DEFAULT_BACKEND)
                != BACKEND_CLAUDE
            ):
                CLAUDE_STOP_FENCE_SESSIONS.discard(session_id)
                RUNS.maintenance.discard(session_id)
                discard_unused_claude_stop_fence_attempt_lock(session_id)
                return
            if (
                session_id in RUNS.active
                or session_id in RUNS.busy
                or stop_cleanup_in_progress(session_id)
            ):
                continue
            # Reassert the fail-closed admission lease in case an unrelated
            # late cleanup discarded the coarse maintenance membership.
            RUNS.maintenance.add(session_id)

        try:
            result = await stop_idle_claude_background_subagents_bounded(
                session_id,
                emit_event=False,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "Claude stop fence retry raised session=%s attempt=%s error=%s",
                session_id,
                attempt,
                concise_error_message(exc),
            )
            continue
        if not bool(result.get("fence_committed", False)):
            logger.warning(
                "Claude stop fence retry deferred session=%s attempt=%s errors=%s",
                session_id,
                attempt,
                result.get("errors") or [],
            )
            continue

        released = False
        async with RUNS.active_lock:
            if (
                session_id in CLAUDE_STOP_FENCE_SESSIONS
                and session_id not in RUNS.active
                and session_id not in RUNS.busy
                and not stop_cleanup_in_progress(session_id)
            ):
                CLAUDE_STOP_FENCE_SESSIONS.discard(session_id)
                RUNS.maintenance.discard(session_id)
                released = True
        if released:
            discard_unused_claude_stop_fence_attempt_lock(session_id)
            logger.info(
                "Claude stop fence recovered session=%s attempts=%s",
                session_id,
                attempt,
            )
            schedule_next_queued_turn(session_id)
            return


def schedule_claude_stop_fence_retry(session_id: str) -> None:
    """Start the single retry owner for one failed Claude stop fence."""

    if SERVER.shutting_down:
        return
    current = CLAUDE_STOP_FENCE_RETRY_TASKS.get(session_id)
    if current is not None and not current.done():
        return
    task = asyncio.create_task(
        retry_claude_stop_fence(session_id),
        name=f"claude-stop-fence-retry:{session_id}",
    )
    CLAUDE_STOP_FENCE_RETRY_TASKS[session_id] = task

    def retry_finished(completed: asyncio.Task[Any]) -> None:
        if CLAUDE_STOP_FENCE_RETRY_TASKS.get(session_id) is completed:
            CLAUDE_STOP_FENCE_RETRY_TASKS.pop(session_id, None)
        if completed.cancelled():
            return
        error = completed.exception()
        if error is not None:
            logger.error(
                "Claude stop fence retry failed session=%s",
                session_id,
                exc_info=(type(error), error, error.__traceback__),
            )

    task.add_done_callback(retry_finished)


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


async def broadcast_reasoning_summary_stream(session_id: str, packet: dict[str, Any]) -> None:
    try:
        await HUB.broadcast(session_id, packet)
    except Exception:
        logger.debug("reasoning summary live delivery failed session=%s", session_id)


async def flush_reasoning_summary_stream(session_id: str, delay: float) -> None:
    try:
        await asyncio.sleep(delay)
        async with event_delivery_lock(session_id):
            if REASONING_SUMMARY_STREAM_PENDING.get(session_id) is not asyncio.current_task():
                return
            REASONING_SUMMARY_STREAM_PENDING.pop(session_id, None)
            REASONING_SUMMARY_STREAM_LAST_SENT[session_id] = time.monotonic()
            packet = reasoning_summary_stream_snapshot(session_id)
        await broadcast_reasoning_summary_stream(session_id, packet)
    finally:
        if REASONING_SUMMARY_STREAM_PENDING.get(session_id) is asyncio.current_task():
            REASONING_SUMMARY_STREAM_PENDING.pop(session_id, None)


async def update_reasoning_summary_stream(
    session_id: str, run_id: str, item_id: str, params: dict[str, Any],
    metadata: dict[str, Any] | None = None,
    phase: str = "summary",
) -> None:
    """Keep provider-exposed summary and plaintext reasoning channels distinct."""
    if phase not in {"summary", "reasoning"}:
        return
    index = params.get("contentIndex" if phase == "reasoning" else "summaryIndex", 0)
    delta = params.get("delta", "")
    if not run_id or not item_id or not isinstance(index, int) or isinstance(index, bool) or not 0 <= index < 1024:
        return
    if not isinstance(delta, str):
        return
    async with event_delivery_lock(session_id):
        if session_id in DELETING_SESSIONS or session_id in DELETED_SESSION_TOMBSTONES:
            return
        streams = REASONING_SUMMARY_STREAMS.setdefault(session_id, {})
        state = streams.get((run_id, item_id, phase))
        first = state is None or not state["item"]["text"].strip()
        if state is None:
            after_seq = EVENT_SEQ_CACHE.get(session_id)
            if after_seq is None:
                after_seq = await asyncio.to_thread(last_event_seq_from_file, events_path(session_id))
            state = {"parts": {}, "item": {
                **dict(metadata or {}), "run_id": run_id, "item_id": item_id,
                "backend": (metadata or {}).get("backend", BACKEND_CODEX), "phase": phase, "text": "",
                "ts": now_iso(), "after_seq": after_seq,
            }}
            streams[(run_id, item_id, phase)] = state
        parts = state["parts"]
        parts[index] = parts.get(index, "") + delta
        state["item"]["text"] = "\n".join(parts[index] for index in sorted(parts))
        now = time.monotonic()
        delay = max(0.0, 0.05 - (now - REASONING_SUMMARY_STREAM_LAST_SENT.get(session_id, 0.0)))
        if not first and delay:
            if session_id not in REASONING_SUMMARY_STREAM_PENDING:
                REASONING_SUMMARY_STREAM_PENDING[session_id] = asyncio.create_task(
                    flush_reasoning_summary_stream(session_id, delay))
            return
        REASONING_SUMMARY_STREAM_LAST_SENT[session_id] = now
        packet = reasoning_summary_stream_snapshot(session_id)
    # Snapshot revisions fence a reconnect snapshot racing a newer update.
    # Socket writes remain outside the durable event delivery lock.
    await broadcast_reasoning_summary_stream(session_id, packet)


def reasoning_summary_stream_item(session_id: str, run_id: str, item_id: str, phase: str = "summary") -> dict[str, Any]:
    state = REASONING_SUMMARY_STREAMS.get(session_id, {}).get((run_id, item_id, phase))
    return dict(state["item"]) if state else {}


async def clear_reasoning_summary_stream(session_id: str, run_id: str, item_id: str | None = None, phase: str = "summary") -> None:
    async with event_delivery_lock(session_id):
        streams = REASONING_SUMMARY_STREAMS.get(session_id)
        if not streams:
            return
        removed = [key for key in streams if key[0] == run_id and (item_id is None or (key[1] == item_id and key[2] == phase))]
        if not removed:
            return
        for key in removed:
            del streams[key]
        if not streams:
            REASONING_SUMMARY_STREAMS.pop(session_id, None)
            REASONING_SUMMARY_STREAM_LAST_SENT.pop(session_id, None)
        pending = REASONING_SUMMARY_STREAM_PENDING.pop(session_id, None)
        if pending:
            pending.cancel()
        packet = reasoning_summary_stream_snapshot(session_id)
    if pending:
        await asyncio.gather(pending, return_exceptions=True)
    await broadcast_reasoning_summary_stream(session_id, packet)


async def persist_reasoning_summary(
    session_id: str, payload: dict[str, Any], completed_item_ids: set[str | tuple[str, str]],
) -> None:
    # Join the append and identity update together: cancellation after the
    # ledger write must not make the finalizer append the item a second time.
    async def commit() -> None:
        await append_event(session_id, "reasoning_text" if payload.get("phase") == "reasoning" else "reasoning_summary", payload)
        phase = payload.get("phase", "summary")
        completed_item_ids.add(("reasoning", payload["item_id"]) if phase == "reasoning" else payload["item_id"])
        await clear_reasoning_summary_stream(session_id, payload["run_id"], payload["item_id"], phase)
    task = asyncio.create_task(commit())
    try:
        await asyncio.shield(task)
    except asyncio.CancelledError:
        await join_task_despite_caller_cancellation(task)
        raise


async def finish_reasoning_summary_stream(
    session_id: str, run_id: str, completed_item_ids: set[str | tuple[str, str]],
) -> None:
    """Keep exposed text already received when interruption omits item/completed."""
    items = [dict(state["item"]) for (owner, _, _), state in
             REASONING_SUMMARY_STREAMS.get(session_id, {}).items() if owner == run_id]
    try:
        for item in items:
            item_id = item["item_id"]
            identity = ("reasoning", item_id) if item.get("phase") == "reasoning" else item_id
            if identity in completed_item_ids or not item["text"].strip():
                continue
            payload = {key: value for key, value in item.items() if key not in {"ts", "after_seq"}}
            payload.update(partial=True, reasoning_after_seq=item["after_seq"])
            await persist_reasoning_summary(session_id, payload, completed_item_ids)
    except Exception:
        # Storage failure must not strand the provider subscription or turn
        # ownership while finalizers are releasing this chat.
        logger.exception("could not preserve partial reasoning summary session=%s run=%s", session_id, run_id)
    finally:
        await clear_reasoning_summary_stream(session_id, run_id)


async def run_claude_print(
    session_id: str,
    run_id: str,
    prompt: str,
    sess: dict[str, Any],
    manifest_path: Path,
    *,
    standalone_provider_context: bool = False,
    provider_runtime_env: dict[str, str] | None = None,
) -> None:
    runtime_env = validate_provider_runtime_env(provider_runtime_env)
    if standalone_provider_context:
        sess = standalone_provider_session(sess)
    requested_cwd = str(sess.get("cwd") or DEFAULT_CWD)
    cwd = existing_cwd(requested_cwd)
    diff_baseline = await capture_git_baseline(session_id, run_id, cwd)
    resume_provider_id, resume_skip_message = resolve_claude_resume_provider(sess, cwd)
    no_session_persistence = (
        standalone_provider_context
        and await asyncio.to_thread(claude_supports_no_session_persistence)
    )
    cmd = build_claude_cmd(
        session_id,
        sess,
        manifest_path,
        provider_id=resume_provider_id,
        no_session_persistence=no_session_persistence,
        disable_provider_subagents=bool(runtime_env),
    )
    if str(Path(requested_cwd).expanduser()) != cwd:
        await append_event(session_id, "cwd_fallback", {"run_id": run_id, "requested_cwd": requested_cwd, "cwd": cwd})
    if resume_skip_message:
        await append_event(session_id, "cwd_fallback", {
            "run_id": run_id,
            "requested_cwd": requested_cwd,
            "cwd": cwd,
            "provider_session_id": claude_provider_id_for_session(sess),
            "message": resume_skip_message,
        })
    public_cmd = redacted_provider_argv(cmd, BACKEND_CLAUDE)
    await append_event(session_id, "process_started", {
        "run_id": run_id,
        "backend": BACKEND_CLAUDE,
        "transport": CLAUDE_TRANSPORT_PRINT,
        "argv": public_cmd,
        "cwd": cwd,
    })
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            env=agent_runner_env(session_id, runtime_env),
            limit=PROCESS_STREAM_LIMIT,
            start_new_session=True,
        )
    except Exception as e:
        released = await release_turn_slot(
            session_id,
            expected_run_id=run_id,
        )
        try:
            if released:
                record_runtime_failure(BACKEND_CLAUDE, e, spawn_failure=True)
                await append_event(session_id, "error", {"run_id": run_id, "backend": BACKEND_CLAUDE, "message": f"failed to start Claude: {e}", **run_event_metadata(run_id)})
                await append_turn_finished_event(session_id, {
                    "run_id": run_id,
                    "backend": BACKEND_CLAUDE,
                    "exit_code": None,
                    "result_text": "",
                    **run_event_metadata(run_id),
                })
        finally:
            RUNS.run_metadata.pop(run_id, None)
            RUNS.stopped_runs.discard(run_id)
            if released:
                schedule_next_queued_turn(session_id)
        return
    pgid = process_group_for_pid(proc.pid)
    bound, stop_requested = await bind_active_turn(
        session_id,
        run_id,
        {
            "proc": proc,
            "run_id": run_id,
            "backend": BACKEND_CLAUDE,
            "transport": CLAUDE_TRANSPORT_PRINT,
            "pid": proc.pid,
            "pgid": pgid,
            "cwd": cwd,
            "argv": public_cmd,
            "started_at": time.time(),
            "started_at_iso": now_iso(),
            "provider_turn_ready": False,
            "stdout_tail": deque(maxlen=LIVE_STDOUT_MAX_LINES),
            "stdout_total_lines": 0,
            "stdout_updated_at": None,
        },
    )
    if not bound:
        await terminate_process_tree(proc, grace=0.5)
        return
    if stop_requested:
        await terminate_process_tree(proc)
    if not stop_requested and proc.stdin:
        proc.stdin.write(prompt.encode())
        await proc.stdin.drain()
        proc.stdin.close()

    final_text = ""
    provider_id: str | None = None
    current_tools: dict[str, dict[str, Any]] = {}
    had_tool_activity = False
    changed_paths: set[str] = set()
    last_event = time.time()
    idle_killed = False
    stream_error: str | None = None
    result_error: str | None = None
    terminal_result_received = False
    seen_artifacts: set[str] = set()
    manifest_watch_task = asyncio.create_task(watch_manifest_artifacts(session_id, run_id, manifest_path, seen_artifacts))

    try:
        while True:
            try:
                raw = await asyncio.wait_for(proc.stdout.readline(), timeout=5)  # type: ignore[union-attr]
            except asyncio.TimeoutError:
                idle = time.time() - last_event
                if idle >= IDLE_WARN_SECONDS:
                    await append_event(session_id, "idle_warning", {"run_id": run_id, "idle_seconds": int(idle)})
                if idle >= IDLE_KILL_SECONDS:
                    idle_killed = True
                    await terminate_process_tree(proc)
                    break
                continue
            if not raw:
                break
            last_event = time.time()
            decoded = raw.decode("utf-8", "replace").rstrip("\r\n")
            await append_active_stdout(session_id, decoded)
            line = decoded.strip()
            if not line:
                continue
            await append_event(session_id, "raw_event", {"run_id": run_id, "backend": BACKEND_CLAUDE, "raw": line})
            try:
                event = json.loads(line)
            except Exception:
                continue
            if event.get("session_id"):
                if not provider_id:
                    provider_id = event["session_id"]
                await mark_provider_turn_ready(
                    session_id,
                    run_id,
                    str(event["session_id"]),
                )
            etype = event.get("type")
            if etype == "assistant":
                for block in event.get("message", {}).get("content", []):
                    btype = block.get("type")
                    if btype == "text" and block.get("text"):
                        text = clean_assistant_text(block["text"])
                        if text:
                            await append_event(session_id, "assistant_text", {
                                "run_id": run_id,
                                "text": text,
                                "backend": BACKEND_CLAUDE,
                                **run_event_metadata(run_id),
                            })
                    elif btype == "thinking" and block.get("thinking"):
                        await append_event(session_id, "reasoning_summary", {"run_id": run_id, "text": block["thinking"]})
                    elif btype in ("tool_use", "server_tool_use"):
                        had_tool_activity = True
                        tid = block.get("id") or f"tool_{uuid.uuid4().hex[:8]}"
                        tool = {"id": tid, "name": block.get("name", "tool"), "input": block.get("input", {})}
                        current_tools[tid] = tool
                        changed_paths.update(tool_changed_paths(tool))
                        await append_event(session_id, "tool_started", {"run_id": run_id, "tool": tool})
            elif etype == "user":
                for block in event.get("message", {}).get("content", []):
                    if block.get("type") == "tool_result":
                        had_tool_activity = True
                        tid = block.get("tool_use_id")
                        content = event_output_text(block.get("content", ""))
                        await append_event(session_id, "tool_finished", {
                            "run_id": run_id,
                            "tool_id": tid,
                            "tool": current_tools.pop(tid, None),
                            "output": content,
                            "is_error": block.get("is_error") is True,
                        })
            elif etype == "result":
                terminal_result_received = True
                if run_id in RUNS.stopped_runs and is_expected_claude_interruption_result(event):
                    continue
                result_error = claude_result_error(event)
                if result_error:
                    provider_id = None
                    await append_event(session_id, "error", {"run_id": run_id, "backend": BACKEND_CLAUDE, "message": result_error, **run_event_metadata(run_id)})
                    continue
                final_text = event.get("result", "") or final_text
                if event.get("session_id"):
                    provider_id = event["session_id"]
    except Exception as e:
        stream_error = f"{type(e).__name__}: {e}"
        logger.exception("Claude run failed session=%s run=%s", session_id, run_id)
    finally:
        manifest_watch_task.cancel()
        with suppress(asyncio.CancelledError):
            await manifest_watch_task
        await terminate_process_tree(proc, grace=0.5)
        await clear_active_process(session_id, expected_run_id=run_id)

    stderr = ""
    if proc.stderr:
        stderr = (await proc.stderr.read()).decode("utf-8", "replace").strip()
    stopped = run_id in RUNS.stopped_runs
    if not stopped and not terminal_result_received and not stream_error:
        stream_error = (
            "Claude print stream ended before a terminal result became "
            "available."
        )
    result_text = clean_assistant_text(final_text)
    empty_result_error = claude_empty_turn_failure_message(
        prompt=prompt,
        result_text=result_text,
        had_tool_activity=had_tool_activity,
        stopped=stopped,
        terminal_result_received=terminal_result_received,
        existing_error=bool(
            result_error
            or stream_error
            or idle_killed
            or proc.returncode not in (0, None)
        ),
    )
    if stream_error and not stopped:
        await append_event(session_id, "error", {"run_id": run_id, "message": f"Claude stream failed: {stream_error}", **run_event_metadata(run_id)})
    if idle_killed:
        await append_event(session_id, "error", {"run_id": run_id, "message": "killed after idle timeout", **run_event_metadata(run_id)})
    if not stopped and proc.returncode not in (0, None) and stderr:
        await append_event(session_id, "error", {"run_id": run_id, "message": stderr[:4000], "exit_code": proc.returncode, **run_event_metadata(run_id)})
    if empty_result_error:
        await append_event(session_id, "error", {
            "run_id": run_id,
            "backend": BACKEND_CLAUDE,
            "transport": CLAUDE_TRANSPORT_PRINT,
            "message": empty_result_error,
            **run_event_metadata(run_id),
        })
    if not stopped and (
        result_error
        or stream_error
        or proc.returncode not in (0, None)
        or empty_result_error
    ):
        record_runtime_failure(
            BACKEND_CLAUDE,
            result_error
            or stream_error
            or stderr
            or empty_result_error
            or f"exit {proc.returncode}",
        )
    elif not stopped:
        record_runtime_success(BACKEND_CLAUDE)
    if provider_id and not result_error:
        await persist_run_provider_session(
            session_id,
            run_id,
            BACKEND_CLAUDE,
            provider_id,
            cwd=cwd,
            standalone_provider_context=standalone_provider_context,
        )
    await collect_manifest(session_id, run_id, manifest_path, seen_artifacts=seen_artifacts, final=True)
    await collect_recent_leftover_manifests(session_id, run_id, manifest_path, seen_artifacts=seen_artifacts)
    await publish_turn_code_diff(session_id, run_id, BACKEND_CLAUDE, cwd, diff_baseline, changed_paths)
    terminal_payload = {
        "run_id": run_id,
        "backend": BACKEND_CLAUDE,
        "transport": CLAUDE_TRANSPORT_PRINT,
        "exit_code": (
            1
            if (
                empty_result_error
                or stream_error
                or result_error
                or idle_killed
            ) and proc.returncode in (0, None)
            else proc.returncode
        ),
        "result_text": result_text,
        "stopped": stopped,
        **run_event_metadata(run_id),
    }
    finalize_task = asyncio.create_task(finalize_owned_turn_finished(
        session_id,
        run_id,
        stopped=stopped,
        payload=terminal_payload,
    ))
    try:
        await asyncio.shield(finalize_task)
    except asyncio.CancelledError:
        await join_task_despite_caller_cancellation(finalize_task)
        raise


async def finish_claude_sdk_start_failure(
    session_id: str,
    run_id: str,
    message: str,
    *,
    delivery_uncertain: bool = False,
    ownership_token: str | None = None,
) -> None:
    async with RUNS.active_lock:
        active = RUNS.active.get(session_id)
        if active and str(active.get("run_id") or "") == run_id:
            active["provider_turn_ready"] = False
            active["native_steer_queue"] = None
            active["claude_permissions_open"] = False
    released = await release_turn_slot(
        session_id,
        expected_run_id=run_id,
    )
    try:
        if not released:
            return
        await cancel_claude_interactions(
            session_id,
            resolution="turn_failed",
            expected_run_id=run_id,
            ownership_token=ownership_token,
        )
        record_runtime_failure(BACKEND_CLAUDE, message, spawn_failure=True)
        await append_event(session_id, "error", {
            "run_id": run_id,
            "backend": BACKEND_CLAUDE,
            "transport": CLAUDE_TRANSPORT_AGENT_SDK,
            "message": message,
            "delivery_unknown": delivery_uncertain,
            **run_event_metadata(run_id),
        })
        await append_turn_finished_event(session_id, {
            "run_id": run_id,
            "backend": BACKEND_CLAUDE,
            "transport": CLAUDE_TRANSPORT_AGENT_SDK,
            "exit_code": None,
            "result_text": "",
            "delivery_unknown": delivery_uncertain,
            **run_event_metadata(run_id),
        })
    finally:
        RUNS.run_metadata.pop(run_id, None)
        RUNS.stopped_runs.discard(run_id)
        if released:
            schedule_next_queued_turn(session_id)


async def finish_cancelled_claude_sdk_start(
    session_id: str,
    run_id: str,
    manager: ClaudeSDKSupervisorManager,
    *,
    ownership_token: str | None = None,
) -> None:
    """Retire a possibly accepted query and clear its reservation safely."""

    RUNS.stopped_runs.add(run_id)
    if await turn_slot_is_owned(session_id, run_id):
        if ownership_token:
            await evict_claude_sdk_chat(
                session_id,
                force=True,
                manager=manager,
                ownership_token=ownership_token,
            )
        await cancel_claude_interactions(
            session_id,
            resolution="turn_stopped",
            expected_run_id=run_id,
            ownership_token=ownership_token,
        )
    drain_queue = should_schedule_queue_after_finish(
        session_id,
        stopped=True,
    )
    released = await release_turn_slot(
        session_id,
        expected_run_id=run_id,
    )
    try:
        if released:
            await append_event(session_id, "turn_stopped", {
                "run_id": run_id,
                "backend": BACKEND_CLAUDE,
                "transport": CLAUDE_TRANSPORT_AGENT_SDK,
                "message": "Claude was stopped while the SDK turn was starting.",
                **run_event_metadata(run_id),
            })
    finally:
        RUNS.run_metadata.pop(run_id, None)
        RUNS.stopped_runs.discard(run_id)
        # This cleanup owns the successful release. The outer task supervisor
        # deliberately sees no remaining owner, so the drain must happen here.
        # A concurrent Stop may request the same drain; QUEUE_START_TASKS keeps
        # promotion single-owner and prevents a double launch.
        if released and drain_queue:
            schedule_next_queued_turn(session_id)


async def run_claude_sdk(
    session_id: str,
    run_id: str,
    prompt: str,
    sess: dict[str, Any],
    manifest_path: Path,
    *,
    provider_command: ProviderCommandRecord | None = None,
    provider_runtime_env: dict[str, str] | None = None,
) -> None:
    """Run Claude on one persistent, chat-owned Agent SDK process."""

    requested_cwd = str(sess.get("cwd") or DEFAULT_CWD)
    cwd = existing_cwd(requested_cwd)
    resume_provider_id, resume_skip_message = resolve_claude_resume_provider(
        sess,
        cwd,
    )
    diff_baseline = await capture_git_baseline(session_id, run_id, cwd)
    try:
        options, configuration_key, cli_path = build_claude_sdk_options(
            session_id,
            sess,
            cwd,
            manifest_path,
            provider_runtime_env,
        )
        manager = await claude_sdk_manager()
    except ClaudeSDKUnavailable:
        raise
    except Exception as exc:
        raise ClaudeSDKUnavailable(
            f"Claude Agent SDK setup failed before prompt delivery: {exc}"
        ) from exc
    if str(Path(requested_cwd).expanduser()) != cwd:
        await append_event(session_id, "cwd_fallback", {
            "run_id": run_id,
            "requested_cwd": requested_cwd,
            "cwd": cwd,
        })
    if resume_skip_message:
        await append_event(session_id, "cwd_fallback", {
            "run_id": run_id,
            "requested_cwd": requested_cwd,
            "cwd": cwd,
            "provider_session_id": claude_provider_id_for_session(sess),
            "message": resume_skip_message,
        })
    await append_event(session_id, "process_started", {
        "run_id": run_id,
        "backend": BACKEND_CLAUDE,
        "transport": CLAUDE_TRANSPORT_AGENT_SDK,
        "argv": [
            cli_path,
            "<ClaudeSDKClient>",
            "--permission-mode",
            effective_claude_permission_mode(sess),
        ],
        "cwd": cwd,
    })

    # Publish the chat-owned SDK reservation before query delivery. The SDK
    # may invoke ``can_use_tool`` from inside ``client.query()`` and therefore
    # before ``manager.start_run()`` returns a handle. The approval callback
    # must already be able to verify an opted-in ACTIVE turn at that point.
    native_steer_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=1)
    startup_active = {
        "proc": None,
        "run_id": run_id,
        "backend": BACKEND_CLAUDE,
        "transport": CLAUDE_TRANSPORT_AGENT_SDK,
        "claude_sdk_run": None,
        # Authority-bearing runs receive a fresh provider-tool owner binding.
        # Force Send therefore follows Stop -> queued start for those runs;
        # retain the legacy native path only for callers without authority.
        "native_steer_queue": (
            None
            if provider_runtime_env or provider_command is not None
            else native_steer_queue
        ),
        "interactive_agent_sdk": True,
        "provider_model": str(sess.get("model") or ""),
        "provider_effort": str(sess.get("effort") or ""),
        # Freeze process policy for the lifetime of this provider turn. The
        # persisted session value may change concurrently for the next turn.
        "claude_permission_mode": effective_claude_permission_mode(sess),
        "configuration_key": configuration_key,
        "provider_session_id": resume_provider_id,
        "provider_turn_ready": False,
        "provider_tools_ready": asyncio.Event(),
        "claude_sdk_owner_token": "",
        "claude_permission_run_id": run_id,
        "claude_permissions_open": False,
        "cwd": cwd,
        "argv": [cli_path, "<ClaudeSDKClient>"],
        "started_at": time.time(),
        "started_at_iso": now_iso(),
        "native_interrupt_sent": False,
        "stop_requested": False,
        "provider_starting": True,
        "stdout_tail": deque(maxlen=LIVE_STDOUT_MAX_LINES),
        "stdout_total_lines": 0,
        "stdout_updated_at": None,
    }
    bound, stop_requested = await bind_active_turn(
        session_id,
        run_id,
        startup_active,
    )
    if not bound:
        return
    if stop_requested:
        # Stop won before provider delivery. Do not submit the prompt merely
        # to acquire a handle that would immediately be interrupted.
        await finish_cancelled_claude_sdk_start(
            session_id,
            run_id,
            manager,
            ownership_token=(
                str(startup_active.get("claude_sdk_owner_token") or "")
                or None
            ),
        )
        return

    async def activate_initial_supervisor(ownership_token: str) -> None:
        async with RUNS.active_lock:
            active = RUNS.active.get(session_id)
            if (
                active is not startup_active
                or str(active.get("run_id") or "") != run_id
                or active.get("stop_requested")
                or session_id not in RUNS.busy
            ):
                # This hook runs before supervisor.start_run/query. Raising
                # cancellation guarantees that a Stop/delete winner cannot
                # deliver the prompt afterward.
                raise asyncio.CancelledError
            active["claude_sdk_owner_token"] = str(ownership_token)
            active["claude_permission_run_id"] = run_id
            active["claude_permissions_open"] = True
    initial_reconciliation_batches = pending_task_reconciliations(
        (STORE.sessions.get(session_id) or {}).get(CLAUDE_BACKGROUND_SESSION_FIELD),
        provider_session_id=str(resume_provider_id or ""),
    )
    try:
        provider_command_start_options = (
            {
                "validated_provider_command_name": str(
                    provider_command.native.get("name") or ""
                ),
                "expected_provider_command_generation": str(
                    provider_command.native.get("control_generation") or ""
                ),
            }
            if provider_command is not None
            else {}
        )
        handle = await manager.start_run(
            session_id,
            prompt,
            run_id=run_id,
            options=options,
            configuration_key=configuration_key,
            on_supervisor_ready=activate_initial_supervisor,
            **provider_command_start_options,
            **({"background_task_reconciliation": reconciliation_envelope(initial_reconciliation_batches)}
               if initial_reconciliation_batches else {}),
        )
    except asyncio.CancelledError:
        cleanup_task = asyncio.create_task(
            finish_cancelled_claude_sdk_start(
                session_id,
                run_id,
                manager,
                ownership_token=(
                    str(startup_active.get("claude_sdk_owner_token") or "")
                    or None
                ),
            )
        )
        await join_task_despite_caller_cancellation(cleanup_task)
        raise
    except ClaudeSDKUnavailable:
        raise
    except ClaudeSDKQueryError as exc:
        startup_ownership_token = str(
            startup_active.get("claude_sdk_owner_token") or ""
        )
        if startup_ownership_token and await turn_slot_is_owned(
            session_id,
            run_id,
        ):
            await evict_claude_sdk_chat(
                session_id,
                force=True,
                manager=manager,
                ownership_token=startup_ownership_token,
            )
        await finish_claude_sdk_start_failure(
            session_id,
            run_id,
            (
                "Claude SDK prompt delivery could not be confirmed. The prompt "
                "was not replayed through the fallback transport."
            ),
            delivery_uncertain=True,
            ownership_token=startup_ownership_token or None,
        )
        logger.warning(
            "Claude SDK query delivery uncertain session=%s run=%s: %s",
            session_id,
            run_id,
            concise_error_message(exc),
        )
        return
    except Exception as exc:
        startup_ownership_token = str(
            startup_active.get("claude_sdk_owner_token") or ""
        )
        if startup_ownership_token and await turn_slot_is_owned(
            session_id,
            run_id,
        ):
            await evict_claude_sdk_chat(
                session_id,
                force=True,
                manager=manager,
                ownership_token=startup_ownership_token,
            )
        await finish_claude_sdk_start_failure(
            session_id,
            run_id,
            f"Claude SDK could not start: {concise_error_message(exc)}",
            ownership_token=startup_ownership_token or None,
        )
        return

    attached = False
    initial_provider_ready = bool(
        getattr(handle, "acknowledged", False)
        and not getattr(handle, "done", False)
    )
    async with RUNS.active_lock:
        active = RUNS.active.get(session_id)
        if (
            active is startup_active
            and str(active.get("run_id") or "") == run_id
            and session_id in RUNS.busy
        ):
            active.update({
                "claude_sdk_run": handle,
                "provider_turn_ready": initial_provider_ready,
                "provider_starting": not initial_provider_ready,
            })
            tools_ready = active.get("provider_tools_ready")
            if initial_provider_ready and isinstance(tools_ready, asyncio.Event):
                tools_ready.set()
            stop_requested = bool(active.get("stop_requested"))
            attached = True
        else:
            stop_requested = True
    if not attached:
        # Stop/delete may have hard-terminalized and released the placeholder
        # while a cancellation-hostile SDK query was returning. Interrupt only
        # this old handle; chat-ID eviction could target a newer replacement.
        with suppress(Exception):
            await interrupt_claude_sdk_run_bounded(handle)
        return
    if stop_requested:
        try:
            await interrupt_claude_sdk_run_bounded(handle)
        except Exception:
            await finish_cancelled_claude_sdk_start(
                session_id,
                run_id,
                manager,
                ownership_token=(
                    str(startup_active.get("claude_sdk_owner_token") or "")
                    or None
                ),
            )
            return

    sdk_ownership_token = str(
        startup_active.get("claude_sdk_owner_token") or ""
    )
    current_run_id = run_id
    current_handle = handle
    current_reconciliation_batches = initial_reconciliation_batches
    receipts_persisted_run_ids: set[str] = set()
    current_diff_baseline = diff_baseline
    current_prompt = prompt
    text_parts: list[str] = []
    current_tools: dict[str, dict[str, Any]] = {}
    tool_activity_run_ids: set[str] = set()
    projection_error_run_ids: set[str] = set()
    changed_paths: set[str] = set()
    seen_artifacts: set[str] = set()
    provider_id = str(resume_provider_id or "")
    provider_persisted_id = str(resume_provider_id or "")
    result_details: dict[str, Any] | None = None
    stream_error: str | None = None
    delivery_unknown = False
    cancelled_error: asyncio.CancelledError | None = None
    retire_supervisor = False
    pending_steer: dict[str, Any] | None = None
    message_task: asyncio.Task[Any] | None = None
    steer_task: asyncio.Task[Any] | None = None
    first_activity_task: asyncio.Task[bool] | None = None
    provider_ready_tasks: set[asyncio.Task[bool]] = set()
    outputs_finished_run_ids: set[str] = set()
    shutdown_interrupted_run_ids: set[str] = set()
    logical_started_monotonic = time.monotonic()
    last_activity_monotonic = logical_started_monotonic
    deadline_clock_checked_monotonic = logical_started_monotonic
    deadline_clock_was_paused = False
    provider_acknowledged = initial_provider_ready
    idle_warning_emitted = False
    manifest_watch_task: asyncio.Task[Any] | None = asyncio.create_task(
        watch_manifest_artifacts(
            session_id,
            current_run_id,
            manifest_path,
            seen_artifacts,
        )
    )

    def record_sdk_stream_exception(exc: Exception) -> bool:
        nonlocal stream_error
        # Latch the cause at failure time. A later shutdown must not relabel
        # an earlier provider/projection fault, and an unexpected closed
        # supervisor must remain an error. This never retries the prompt.
        shutdown_interrupted = bool(
            SERVER.shutting_down
            and isinstance(exc, ClaudeSDKSupervisorClosed)
            and not (
                stream_error
                or (result_details or {}).get("error")
                or (result_details or {}).get("is_error")
                or current_run_id in projection_error_run_ids
            )
        )
        if shutdown_interrupted:
            RUNS.stopped_runs.add(current_run_id)
            shutdown_interrupted_run_ids.add(current_run_id)
        stream_error = stream_error or concise_error_message(exc)
        return shutdown_interrupted

    def watch_provider_readiness(
        logical_run_id: str,
        logical_handle: Any,
        *,
        already_published_ready: bool,
    ) -> None:
        if already_published_ready:
            return
        task = asyncio.create_task(
            mark_claude_sdk_turn_ready_after_ack(
                session_id,
                logical_run_id,
                logical_handle,
            )
        )
        provider_ready_tasks.add(task)
        task.add_done_callback(provider_ready_tasks.discard)

    async def first_activity_timeout_after_ack(
        logical_handle: Any,
    ) -> bool:
        """Return true only when an acknowledged turn stays fully silent."""

        wait_acknowledged = getattr(logical_handle, "wait_acknowledged", None)
        if not callable(wait_acknowledged):
            return False
        await wait_acknowledged()
        await asyncio.sleep(
            CLAUDE_SDK_POST_ACK_FIRST_ACTIVITY_TIMEOUT_SECONDS
        )
        # A terminal result can complete immediately before this timeout wins
        # its scheduling race. That completion is provider activity even if
        # the iterator consumer has not projected the frame yet.
        return not bool(getattr(logical_handle, "done", False))

    async def cancel_first_activity_watchdog() -> None:
        nonlocal first_activity_task

        task = first_activity_task
        first_activity_task = None
        if task is None:
            return
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def reset_first_activity_watchdog(logical_handle: Any) -> None:
        nonlocal first_activity_task

        await cancel_first_activity_watchdog()
        first_activity_task = asyncio.create_task(
            first_activity_timeout_after_ack(logical_handle)
        )

    watch_provider_readiness(
        current_run_id,
        current_handle,
        already_published_ready=initial_provider_ready,
    )
    await reset_first_activity_watchdog(current_handle)

    async def finish_outputs(
        logical_run_id: str,
        baseline: dict[str, Any],
        paths: set[str],
        artifacts: set[str],
        logical_manifest_path: Path,
    ) -> None:
        if logical_run_id in outputs_finished_run_ids:
            return
        await collect_manifest(
            session_id,
            logical_run_id,
            logical_manifest_path,
            seen_artifacts=artifacts,
            final=True,
        )
        await publish_turn_code_diff(
            session_id,
            logical_run_id,
            BACKEND_CLAUDE,
            cwd,
            baseline,
            paths,
        )
        outputs_finished_run_ids.add(logical_run_id)

    async def release_transition(event: asyncio.Event) -> None:
        event.set()
        async with RUNS.active_lock:
            active = RUNS.active.get(session_id)
            if active and active.get("logical_transition_ready") is event:
                active.pop("logical_transition_ready", None)
                active.pop("logical_transition_candidate_run_id", None)
                active.pop("logical_transition_accepting_permissions", None)
                active.pop("logical_transition_authority_open", None)
                active.pop("logical_transition_predecessor_run_id", None)
                active.pop("logical_transition_authority_nonce", None)

    async def sdk_deadline_expired(
        now_monotonic: float | None = None,
    ) -> bool:
        nonlocal provider_acknowledged
        nonlocal logical_started_monotonic
        nonlocal last_activity_monotonic
        nonlocal deadline_clock_checked_monotonic
        nonlocal deadline_clock_was_paused
        nonlocal idle_warning_emitted
        nonlocal stream_error
        nonlocal retire_supervisor
        nonlocal delivery_unknown

        observed_at = (
            time.monotonic()
            if now_monotonic is None
            else now_monotonic
        )
        if deadline_clock_was_paused:
            # Exclude elapsed time during which this exact run was waiting on
            # an unresolved user interaction. Once that wait settles, both
            # watchdog clocks resume normally.
            paused_for = max(
                0.0,
                observed_at - deadline_clock_checked_monotonic,
            )
            logical_started_monotonic += paused_for
            last_activity_monotonic += paused_for
        watchdog_paused = await claude_run_has_pending_interaction(
            session_id,
            current_run_id,
        )
        deadline_clock_checked_monotonic = observed_at
        deadline_clock_was_paused = watchdog_paused
        acknowledged_now = bool(
            getattr(current_handle, "acknowledged", False)
        )
        if acknowledged_now and not provider_acknowledged:
            provider_acknowledged = True
            last_activity_monotonic = observed_at
            idle_warning_emitted = False
        if watchdog_paused:
            # Publishing an exact-run approval is proof that this logical
            # provider turn is active, even if the SDK emits no frame while it
            # waits.
            provider_acknowledged = True
            last_activity_monotonic = observed_at
            idle_warning_emitted = False
            # The provider request itself is first activity. Do not let a
            # completed post-ACK watchdog spin or fail as soon as the user
            # answers after a long wait.
            await cancel_first_activity_watchdog()
            return False
        elapsed = observed_at - logical_started_monotonic
        idle = observed_at - last_activity_monotonic
        if (
            not provider_acknowledged
            and elapsed >= CLAUDE_SDK_PRE_ACK_TIMEOUT_SECONDS
        ):
            stream_error = (
                "Claude SDK did not confirm prompt delivery within "
                f"{CLAUDE_SDK_PRE_ACK_TIMEOUT_SECONDS:g}s. The prompt "
                "was not replayed."
            )
            delivery_unknown = True
            retire_supervisor = True
            logger.warning(
                "Claude SDK pre-ACK timeout session=%s run=%s",
                session_id,
                current_run_id,
            )
            return True
        if (
            provider_acknowledged
            and idle >= CLAUDE_SDK_IDLE_WARN_SECONDS
            and not idle_warning_emitted
        ):
            await append_event(session_id, "idle_warning", {
                "run_id": current_run_id,
                "idle_seconds": int(idle),
            })
            idle_warning_emitted = True
        if (
            provider_acknowledged
            and idle >= CLAUDE_SDK_IDLE_TIMEOUT_SECONDS
        ):
            stream_error = (
                "Claude SDK produced no provider activity for "
                f"{CLAUDE_SDK_IDLE_TIMEOUT_SECONDS:g}s."
            )
            delivery_unknown = not provider_acknowledged
            retire_supervisor = True
            logger.warning(
                "Claude SDK idle timeout session=%s run=%s",
                session_id,
                current_run_id,
            )
            return True
        if (
            CLAUDE_SDK_TURN_TIMEOUT_SECONDS > 0
            and elapsed >= CLAUDE_SDK_TURN_TIMEOUT_SECONDS
        ):
            stream_error = (
                "Claude SDK exceeded the absolute turn timeout of "
                f"{CLAUDE_SDK_TURN_TIMEOUT_SECONDS:g}s."
            )
            delivery_unknown = not provider_acknowledged
            retire_supervisor = True
            logger.warning(
                "Claude SDK absolute timeout session=%s run=%s",
                session_id,
                current_run_id,
            )
            return True
        return False

    try:
        while True:
            current_reconciliation_batches = await acknowledge_claude_background_reconciliation(
                session_id, current_run_id, provider_id, current_handle,
                current_reconciliation_batches,
            )
            now_monotonic = time.monotonic()
            if await sdk_deadline_expired(now_monotonic):
                break
            if message_task is None:
                message_task = asyncio.create_task(current_handle.__anext__())
            if pending_steer is None and steer_task is None:
                steer_task = asyncio.create_task(native_steer_queue.get())
            waiters = {message_task}
            if steer_task is not None:
                waiters.add(steer_task)
            if first_activity_task is not None:
                waiters.add(first_activity_task)
            elapsed = now_monotonic - logical_started_monotonic
            idle = now_monotonic - last_activity_monotonic
            timeout_candidates = [5.0]
            if CLAUDE_SDK_TURN_TIMEOUT_SECONDS > 0:
                timeout_candidates.append(
                    CLAUDE_SDK_TURN_TIMEOUT_SECONDS
                    if deadline_clock_was_paused
                    else max(
                        0.01,
                        CLAUDE_SDK_TURN_TIMEOUT_SECONDS - elapsed,
                    )
                )
            if provider_acknowledged:
                idle_deadline = (
                    CLAUDE_SDK_IDLE_TIMEOUT_SECONDS
                    if idle_warning_emitted
                    else CLAUDE_SDK_IDLE_WARN_SECONDS
                )
                timeout_candidates.append(max(0.01, idle_deadline - idle))
            else:
                timeout_candidates.append(
                    max(0.01, CLAUDE_SDK_PRE_ACK_TIMEOUT_SECONDS - elapsed)
                )
            done, _pending = await asyncio.wait(
                waiters,
                timeout=max(0.01, min(timeout_candidates)),
                return_when=asyncio.FIRST_COMPLETED,
            )

            if not done:
                if await sdk_deadline_expired():
                    break
                continue
            if message_task in done:
                # A provider frame ready at the deadline is current activity;
                # give it the idle lease while still enforcing the absolute
                # and pre-ACK bounds below.
                last_activity_monotonic = time.monotonic()
                idle_warning_emitted = False
            if await sdk_deadline_expired():
                break

            if (
                first_activity_task is not None
                and first_activity_task in done
                and message_task not in done
            ):
                timed_out = first_activity_task.result()
                first_activity_task = None
                if timed_out:
                    stream_error = (
                        "Claude SDK acknowledged the turn but produced no provider "
                        "activity within "
                        f"{CLAUDE_SDK_POST_ACK_FIRST_ACTIVITY_TIMEOUT_SECONDS:g}s."
                    )
                    retire_supervisor = True
                    logger.warning(
                        "Claude SDK post-ACK first-activity timeout "
                        "session=%s run=%s",
                        session_id,
                        current_run_id,
                    )
                    break

            if steer_task is not None and steer_task in done:
                request = steer_task.result()
                steer_task = None
                mark_native_steer_accepted(request)
                selected = request.get("selected") or {}
                future = request.get("future")
                candidate_run_id = f"run_{uuid.uuid4().hex[:16]}"
                transition_nonce = secrets.token_hex(16)
                try:
                    request_prompt = build_user_provider_prompt(
                        session_id,
                        str(selected.get("prompt") or ""),
                        list(selected.get("file_ids") or []),
                    )
                except Exception as exc:
                    if isinstance(future, asyncio.Future) and not future.done():
                        future.set_exception(NativeSteerHandoffError(
                            concise_error_message(exc),
                            safe_to_requeue=True,
                        ))
                    continue

                transition_ready = asyncio.Event()
                transition_reserved = False
                async with RUNS.active_lock:
                    active = RUNS.active.get(session_id)
                    if not active or active.get("stop_requested"):
                        if isinstance(future, asyncio.Future) and not future.done():
                            future.set_exception(NativeSteerHandoffError(
                                "the active Claude turn is already stopping",
                                safe_to_requeue=True,
                            ))
                        continue
                    active["logical_transition_ready"] = transition_ready
                    active["logical_transition_candidate_run_id"] = candidate_run_id
                    active["logical_transition_predecessor_run_id"] = current_run_id
                    active["logical_transition_authority_nonce"] = transition_nonce
                    active["logical_transition_accepting_permissions"] = False
                    active["claude_permissions_open"] = False
                    transition_reserved = True
                interrupt_sent = False
                try:
                    await cancel_claude_interactions(
                        session_id,
                        resolution="turn_steered",
                    )
                    interrupt_sent = await interrupt_claude_sdk_run_bounded(
                        current_handle,
                    )
                except asyncio.TimeoutError as exc:
                    if transition_reserved:
                        await release_transition(transition_ready)
                    if isinstance(future, asyncio.Future) and not future.done():
                        future.set_exception(NativeSteerHandoffError(
                            (
                                "Claude steering interrupt timed out before "
                                "the next prompt was sent"
                            ),
                            safe_to_requeue=True,
                        ))
                    # The selected steering prompt has not been queried, but
                    # the old run's interrupt state is unknowable. Retire only
                    # this chat supervisor and finish the current logical run.
                    retire_supervisor = True
                    stream_error = concise_error_message(exc)
                    RUNS.stopped_runs.add(current_run_id)
                    break
                except Exception as exc:
                    async with RUNS.active_lock:
                        active = RUNS.active.get(session_id)
                        if (
                            active
                            and not active.get("stop_requested")
                            and active.get("logical_transition_ready")
                            is transition_ready
                        ):
                            active["claude_permissions_open"] = True
                    if transition_reserved:
                        await release_transition(transition_ready)
                    if isinstance(future, asyncio.Future) and not future.done():
                        future.set_exception(NativeSteerHandoffError(
                            f"Claude steering interrupt failed: {concise_error_message(exc)}",
                            safe_to_requeue=True,
                        ))
                    continue
                pending_steer = {
                    "selected": selected,
                    "remaining": int(request.get("remaining") or 0),
                    "future": future,
                    "candidate_run_id": candidate_run_id,
                    "predecessor_run_id": current_run_id,
                    "transition_nonce": transition_nonce,
                    "request_prompt": request_prompt,
                    "transition_ready": transition_ready,
                    "transition_reserved": transition_reserved,
                    "interrupt_sent": interrupt_sent,
                }

            if message_task not in done:
                continue
            try:
                message = message_task.result()
            except StopAsyncIteration:
                message_task = None
                if not bool(getattr(current_handle, "done", False)):
                    stream_error = (
                        "Claude SDK message stream ended before a terminal result "
                        "became available."
                    )
                    retire_supervisor = True
                    result_details = None
                else:
                    result_details = claude_sdk_result_details(
                        await current_handle.wait_result()
                    )
            except Exception as exc:
                message_task = None
                if bool(getattr(exc, "delivery_uncertain", False)):
                    retire_supervisor = True
                record_sdk_stream_exception(exc)
                result_details = None
            else:
                message_task = None
                last_activity_monotonic = time.monotonic()
                provider_acknowledged = bool(
                    getattr(current_handle, "acknowledged", False)
                ) or provider_acknowledged
                idle_warning_emitted = False
                await cancel_first_activity_watchdog()
                message_provider_id = str(
                    claude_sdk_field(message, "session_id") or ""
                )
                if claude_sdk_type(message) == "SystemMessage":
                    system_data = claude_sdk_field(message, "data", {})
                    if isinstance(system_data, dict):
                        message_provider_id = str(
                            system_data.get("session_id")
                            or message_provider_id
                        )
                if message_provider_id:
                    provider_id = message_provider_id
                    if message_provider_id != provider_persisted_id:
                        await persist_run_provider_session(
                            session_id,
                            current_run_id,
                            BACKEND_CLAUDE,
                            message_provider_id,
                            cwd=cwd,
                        )
                        provider_persisted_id = message_provider_id
                if provider_id and claude_sdk_type(message) != "StreamEvent":
                    with suppress(Exception):
                        await refresh_claude_goal(session_id, provider_id=provider_id)
                projected_result = await project_claude_sdk_message(
                    session_id,
                    current_run_id,
                    message,
                    text_parts=text_parts,
                    current_tools=current_tools,
                    changed_paths=changed_paths,
                    tool_activity_run_ids=tool_activity_run_ids,
                    error_run_ids=projection_error_run_ids,
                )
                if projected_result is None:
                    continue
                result_details = projected_result

            if result_details and result_details.get("session_id"):
                provider_id = str(result_details["session_id"])
            if provider_id and provider_id != provider_persisted_id:
                await persist_run_provider_session(
                    session_id,
                    current_run_id,
                    BACKEND_CLAUDE,
                    provider_id,
                    cwd=cwd,
                )
                provider_persisted_id = provider_id
            if result_details is not None and provider_id:
                # The SDK's authoritative usage RPC is only sampled after its
                # terminal ResultMessage and remains serialized through the
                # chat-owned actor. Failure is telemetry-only and cannot hold
                # up finalization beyond the short bounded timeout.
                await sample_claude_context_usage(
                    session_id,
                    current_run_id,
                    provider_id,
                    manager,
                )
            if pending_steer is None:
                # Stop advertising native steering as soon as this provider
                # result is terminal. A Force Send that raced the result is
                # rejected below and safely requeued onto the next turn.
                async with RUNS.active_lock:
                    active = RUNS.active.get(session_id)
                    if (
                        active
                        and str(active.get("run_id") or "") == current_run_id
                    ):
                        active["provider_turn_ready"] = False
                        active["native_steer_queue"] = None
                        active["claude_permissions_open"] = False
                break

            steer_state = pending_steer
            selected = steer_state["selected"]
            future = steer_state["future"]
            transition_ready = steer_state["transition_ready"]
            # Prefer the provider's terminal reason when available. An
            # interrupt acknowledgement can lose a race with natural
            # completion; it is only a compatibility fallback for older CLIs
            # that omit terminal_reason.
            terminal_reason = str(
                (result_details or {}).get("terminal_reason") or ""
            )
            prior_aborted = (
                bool((result_details or {}).get("aborted"))
                if terminal_reason
                else bool(steer_state.get("interrupt_sent"))
            )
            active_stopping = False
            async with RUNS.active_lock:
                active = RUNS.active.get(session_id)
                active_stopping = not active or bool(active.get("stop_requested"))
            if active_stopping or stream_error or (
                result_details
                and result_details.get("is_error")
                and not prior_aborted
            ):
                await release_transition(transition_ready)
                if isinstance(future, asyncio.Future) and not future.done():
                    future.set_exception(NativeSteerHandoffError(
                        stream_error or str((result_details or {}).get("error") or "Claude ended before steering"),
                        safe_to_requeue=True,
                    ))
                break

            candidate_run_id = str(steer_state["candidate_run_id"])
            if provider_id:
                await persist_run_provider_session(
                    session_id,
                    current_run_id,
                    BACKEND_CLAUDE,
                    provider_id,
                    cwd=cwd,
                )
            candidate_query_allowed = False
            async with RUNS.active_lock:
                active = RUNS.active.get(session_id)
                if (
                    active
                    and not active.get("stop_requested")
                    and active.get("logical_transition_ready")
                    is transition_ready
                    and str(
                        active.get("logical_transition_candidate_run_id") or ""
                    ) == candidate_run_id
                ):
                    # The old response is terminal. Open approval routing for
                    # the candidate before query(), which may synchronously
                    # invoke can_use_tool before returning its run handle.
                    active["logical_transition_accepting_permissions"] = True
                    candidate_query_allowed = True
            if not candidate_query_allowed:
                await release_transition(transition_ready)
                if isinstance(future, asyncio.Future) and not future.done():
                    future.set_exception(NativeSteerHandoffError(
                        "the active Claude turn stopped before steering delivery",
                        safe_to_requeue=True,
                    ))
                break

            # Seal every old-run observable before candidate delivery. A fast
            # steered query can write the manifest/worktree before query()
            # returns; rolling or diffing afterward would attribute those
            # writes to the interrupted run and contaminate its diff.
            previous_run_id = current_run_id
            previous_metadata = run_event_metadata(previous_run_id)
            previous_watcher = manifest_watch_task
            manifest_watch_task = None
            if previous_watcher is not None:
                previous_watcher.cancel()
                await asyncio.gather(previous_watcher, return_exceptions=True)
            previous_manifest_path = manifest_path.with_name(
                f".{previous_run_id}.json"
            )
            with suppress(OSError):
                previous_manifest_path.unlink()
            with suppress(OSError):
                manifest_path.replace(previous_manifest_path)
            previous_baseline = current_diff_baseline
            previous_paths = set(changed_paths)
            previous_artifacts = set(seen_artifacts)
            previous_result_details = dict(result_details or {})
            previous_prompt = current_prompt
            try:
                await finish_outputs(
                    previous_run_id,
                    previous_baseline,
                    previous_paths,
                    previous_artifacts,
                    previous_manifest_path,
                )
                candidate_baseline = await capture_git_baseline(
                    session_id,
                    candidate_run_id,
                    cwd,
                )
            except Exception as exc:
                # Keep the rolled old manifest reachable by the ordinary
                # finalizer if sealing itself failed. A partial collection may
                # already have consumed it, in which case retrying remains
                # harmless and diff publication stays idempotent.
                with suppress(Exception):
                    await finish_outputs(
                        previous_run_id,
                        previous_baseline,
                        previous_paths,
                        previous_artifacts,
                        previous_manifest_path,
                    )
                if previous_run_id not in outputs_finished_run_ids:
                    with suppress(OSError):
                        previous_manifest_path.replace(manifest_path)
                async with RUNS.active_lock:
                    active = RUNS.active.get(session_id)
                    if (
                        active
                        and active.get("logical_transition_ready")
                        is transition_ready
                    ):
                        active["claude_permissions_open"] = False
                await release_transition(transition_ready)
                if isinstance(future, asyncio.Future) and not future.done():
                    future.set_exception(NativeSteerHandoffError(
                        f"Claude steering preparation failed: {concise_error_message(exc)}",
                        safe_to_requeue=True,
                    ))
                break
            candidate_paths: set[str] = set()
            candidate_artifacts: set[str] = set()
            current_reconciliation_batches = await acknowledge_claude_background_reconciliation(
                session_id, current_run_id, provider_id, current_handle,
                current_reconciliation_batches,
            )
            previous_task_batch = await persist_claude_background_task_receipts(
                session_id, current_run_id, provider_id, current_handle,
                tracking_lost=prior_aborted,
            )
            if previous_task_batch is not None:
                receipts_persisted_run_ids.add(current_run_id)
            candidate_reconciliation_batches = (
                [previous_task_batch] if previous_task_batch is not None
                else current_reconciliation_batches
            )
            steer_state.update({
                "candidate_baseline": candidate_baseline,
                "candidate_paths": candidate_paths,
                "candidate_artifacts": candidate_artifacts,
                "candidate_query_attempted": False,
                "candidate_failure_handled": False,
            })
            with suppress(OSError):
                manifest_path.unlink()
            manifest_watch_task = asyncio.create_task(
                watch_manifest_artifacts(
                    session_id,
                    candidate_run_id,
                    manifest_path,
                    candidate_artifacts,
                )
            )

            async def activate_candidate_supervisor(
                ownership_token: str,
            ) -> None:
                steer_state["candidate_ownership_token"] = str(
                    ownership_token
                )
                async with RUNS.active_lock:
                    active = RUNS.active.get(session_id)
                    if (
                        not active
                        or active.get("stop_requested")
                        or active.get("logical_transition_ready")
                        is not transition_ready
                        or str(
                            active.get("logical_transition_candidate_run_id")
                            or ""
                        ) != candidate_run_id
                    ):
                        raise NativeSteerHandoffError(
                            "the active Claude turn stopped before steering delivery",
                            safe_to_requeue=True,
                        )
                    active["claude_sdk_owner_token"] = str(ownership_token)
                    active["claude_permission_run_id"] = candidate_run_id
                    active["claude_permissions_open"] = True
            try:
                try:
                    await fence_native_steer_delivery(
                        session_id,
                        selected,
                        backend=BACKEND_CLAUDE,
                    )
                except BaseException as exc:
                    if isinstance(exc, asyncio.CancelledError):
                        raise
                    raise NativeSteerHandoffError(
                        (
                            "Claude steering was not sent because its durable "
                            f"delivery fence failed: {concise_error_message(exc)}"
                        ),
                        safe_to_requeue=True,
                    ) from exc
                try:
                    candidate_user_prompt = str(
                        steer_state["request_prompt"]
                    )
                    (
                        steer_state["request_prompt"],
                        steer_state["candidate_authority_path"],
                        steer_state["candidate_runtime_env"],
                    ) = await issue_native_steer_provider_authority(
                        session_id,
                        candidate_run_id,
                        selected,
                        str(steer_state["request_prompt"]),
                        str(steer_state["transition_nonce"]),
                        backend=BACKEND_CLAUDE,
                    )
                    assert_provider_user_message_unchanged(
                        str(steer_state["request_prompt"]),
                        candidate_user_prompt,
                    )
                except BaseException as exc:
                    if isinstance(exc, asyncio.CancelledError):
                        raise
                    if isinstance(exc, NativeSteerHandoffError):
                        raise
                    raise NativeSteerHandoffError(
                        (
                            "Claude steering was not sent because replacement "
                            "authority issuance failed: "
                            f"{concise_error_message(exc)}"
                        ),
                        safe_to_requeue=True,
                    ) from exc
                async with RUNS.active_lock:
                    active = RUNS.active.get(session_id)
                    delivery_still_allowed = bool(
                        active
                        and not active.get("stop_requested")
                        and active.get("logical_transition_ready")
                        is transition_ready
                        and str(
                            active.get("logical_transition_candidate_run_id")
                            or ""
                        ) == candidate_run_id
                    )
                    if delivery_still_allowed:
                        active["logical_transition_authority_open"] = True
                if not delivery_still_allowed:
                    await revoke_provider_authority(candidate_run_id)
                    steer_state.pop("candidate_authority_path", None)
                    raise NativeSteerHandoffError(
                        "the active Claude turn stopped before steering delivery",
                        safe_to_requeue=True,
                    )
                candidate_sess = dict(
                    STORE.sessions.get(session_id) or sess
                )
                if provider_id:
                    candidate_sess.update({
                        "backend": BACKEND_CLAUDE,
                        "session_id": provider_id,
                        "claude_session_id": provider_id,
                        "claude_session_cwd": cwd,
                    })
                (
                    candidate_options,
                    candidate_configuration_key,
                    _candidate_cli_path,
                ) = build_claude_sdk_options(
                    session_id,
                    candidate_sess,
                    cwd,
                    manifest_path,
                    steer_state["candidate_runtime_env"],
                )
                # Once manager.start_run is entered, cancellation cannot
                # prove whether client.query accepted the prompt: the SDK
                # actor deliberately shields its command response. Preserve
                # this boundary for Force Send replay decisions and cleanup.
                steer_state["candidate_query_attempted"] = True
                candidate_handle = await manager.start_run(
                    session_id,
                    str(steer_state["request_prompt"]),
                    run_id=candidate_run_id,
                    options=candidate_options,
                    configuration_key=candidate_configuration_key,
                    on_supervisor_ready=activate_candidate_supervisor,
                    **({"background_task_reconciliation": reconciliation_envelope(candidate_reconciliation_batches)}
                       if candidate_reconciliation_batches else {}),
                )
                # start_run returns only after query() acceptance. From this
                # exact point onward the steering message is never safe to
                # replay, even if local projection or bookkeeping fails.
                steer_state["candidate_accepted"] = True
                await reset_first_activity_watchdog(candidate_handle)
            except Exception as exc:
                steer_state["candidate_failure_handled"] = True
                if steer_state.get("candidate_authority_path") is not None:
                    await revoke_provider_authority(candidate_run_id)
                    steer_state.pop("candidate_authority_path", None)
                safe_to_requeue = bool(getattr(exc, "safe_to_requeue", False))
                if not safe_to_requeue:
                    await revoke_provider_authority(previous_run_id)
                candidate_watcher = manifest_watch_task
                manifest_watch_task = None
                if candidate_watcher is not None:
                    candidate_watcher.cancel()
                    await asyncio.gather(
                        candidate_watcher,
                        return_exceptions=True,
                    )
                with suppress(Exception):
                    await finish_outputs(
                        candidate_run_id,
                        candidate_baseline,
                        candidate_paths,
                        candidate_artifacts,
                        manifest_path,
                    )
                async with RUNS.active_lock:
                    active = RUNS.active.get(session_id)
                    if (
                        active
                        and active.get("logical_transition_ready")
                        is transition_ready
                    ):
                        active["claude_permissions_open"] = False
                await release_transition(transition_ready)
                if bool(getattr(exc, "delivery_uncertain", False)):
                    retire_supervisor = True
                if isinstance(future, asyncio.Future) and not future.done():
                    future.set_exception(NativeSteerHandoffError(
                        concise_error_message(exc),
                        safe_to_requeue=safe_to_requeue,
                        delivery_uncertain=not safe_to_requeue,
                    ))
                if not safe_to_requeue:
                    stream_error = (
                        "Claude steering delivery could not be confirmed. The "
                        "message was not replayed."
                    )
                pending_steer = None
                break

            remaining = int(steer_state.get("remaining") or 0)

            metadata = queued_turn_run_metadata(selected)
            metadata["steer_interrupted_run_id"] = previous_run_id
            RUNS.run_metadata[candidate_run_id] = metadata
            display_prompt = str(
                selected.get("display_prompt")
                if selected.get("display_prompt") is not None
                else selected.get("prompt") or ""
            )
            display_file_ids = list(
                selected.get("display_file_ids")
                if selected.get("display_file_ids") is not None
                else selected.get("file_ids") or []
            )
            lineage = turn_steering_lineage(RUNS.current_turns.get(session_id))
            steering_message = {
                "prompt": str(selected.get("prompt") or ""),
                "file_ids": list(selected.get("file_ids") or []),
            }
            if not lineage or lineage[-1] != steering_message:
                lineage.append(steering_message)

            current_run_id = candidate_run_id
            current_handle = candidate_handle
            current_reconciliation_batches = candidate_reconciliation_batches
            options = candidate_options
            configuration_key = candidate_configuration_key
            sdk_ownership_token = str(
                steer_state.get("candidate_ownership_token") or ""
            )
            provider_runtime_env = dict(
                steer_state.get("candidate_runtime_env") or {}
            )
            current_diff_baseline = candidate_baseline
            current_prompt = str(steer_state["request_prompt"])
            text_parts = []
            current_tools = {}
            changed_paths = candidate_paths
            seen_artifacts = candidate_artifacts
            result_details = None
            stream_error = None
            stopped_during_handoff = False
            candidate_provider_ready = bool(
                getattr(candidate_handle, "acknowledged", False)
                and not getattr(candidate_handle, "done", False)
            )
            logical_started_monotonic = time.monotonic()
            last_activity_monotonic = logical_started_monotonic
            deadline_clock_checked_monotonic = logical_started_monotonic
            deadline_clock_was_paused = False
            provider_acknowledged = candidate_provider_ready
            idle_warning_emitted = False
            delivery_unknown = False
            async with RUNS.active_lock:
                active = RUNS.active.get(session_id)
                if active:
                    stopped_during_handoff = bool(active.get("stop_requested"))
                    active.update({
                        "run_id": candidate_run_id,
                        "claude_sdk_run": candidate_handle,
                        "started_at": time.time(),
                        "started_at_iso": now_iso(),
                        "native_interrupt_sent": False,
                        "stop_requested": stopped_during_handoff,
                        "provider_turn_ready": candidate_provider_ready,
                        "provider_starting": not candidate_provider_ready,
                    })
                    if stopped_during_handoff:
                        RUNS.stopped_runs.add(candidate_run_id)
                    current = RUNS.current_turns.get(session_id)
                    if current is not None:
                        current.update({
                            "run_id": candidate_run_id,
                            "prompt": str(selected.get("prompt") or ""),
                            "display_prompt": display_prompt,
                            "file_ids": list(selected.get("file_ids") or []),
                            "queued_id": selected.get("queued_id"),
                            "skill_selection": selected.get("skill_selection"),
                            "steering_lineage": lineage,
                        })
                    steer_state["candidate_committed"] = True

            watch_provider_readiness(
                candidate_run_id,
                candidate_handle,
                already_published_ready=candidate_provider_ready,
            )

            if prior_aborted:
                await append_event(session_id, "turn_stopped", {
                    "run_id": previous_run_id,
                    "backend": BACKEND_CLAUDE,
                    "transport": CLAUDE_TRANSPORT_AGENT_SDK,
                    "native_steer": True,
                    "provider_session_id": provider_id or None,
                    "superseded_by_run_id": candidate_run_id,
                    **previous_metadata,
                })
            else:
                previous_result_text = clean_assistant_text(
                    str(previous_result_details.get("result_text") or "")
                )
                previous_projection_error = (
                    previous_run_id in projection_error_run_ids
                )
                previous_empty_error = claude_empty_turn_failure_message(
                    prompt=previous_prompt,
                    result_text=previous_result_text,
                    had_tool_activity=(
                        previous_run_id in tool_activity_run_ids
                    ),
                    stopped=False,
                    terminal_result_received=True,
                    existing_error=bool(
                        previous_result_details.get("error")
                        or previous_result_details.get("is_error")
                        or previous_projection_error
                    ),
                )
                if previous_empty_error:
                    await append_event(session_id, "error", {
                        "run_id": previous_run_id,
                        "backend": BACKEND_CLAUDE,
                        "transport": CLAUDE_TRANSPORT_AGENT_SDK,
                        "message": previous_empty_error,
                        **previous_metadata,
                    })
                if previous_empty_error or previous_projection_error:
                    record_runtime_failure(
                        BACKEND_CLAUDE,
                        previous_empty_error or "Claude assistant error",
                    )
                await append_turn_finished_event(session_id, {
                    "run_id": previous_run_id,
                    "backend": BACKEND_CLAUDE,
                    "transport": CLAUDE_TRANSPORT_AGENT_SDK,
                    "exit_code": (
                        1
                        if previous_empty_error or previous_projection_error
                        else 0
                    ),
                    "result_text": previous_result_text,
                    **previous_metadata,
                })
            await revoke_provider_authority(previous_run_id)
            await append_durable_event_batch(session_id, [
                ("turn_queue_run_now", {
                    "queued_id": selected.get("queued_id"),
                    "backend": BACKEND_CLAUDE,
                    "prompt": display_prompt,
                    "request_prompt": str(selected.get("prompt") or ""),
                    "display_prompt": display_prompt,
                    "file_ids": list(selected.get("file_ids") or []),
                    "display_file_ids": display_file_ids,
                    "interrupted_run_id": previous_run_id,
                    "replays_interrupted_message": False,
                    "native_steer": True,
                    "message": "Steering message sent to the active Claude chat.",
                    "remaining": remaining,
                    "superseded_queued_ids": [],
                }),
                ("turn_started", {
                    "run_id": candidate_run_id,
                    "backend": BACKEND_CLAUDE,
                    "prompt": display_prompt,
                    "file_ids": display_file_ids,
                    "queued_id": selected.get("queued_id"),
                    "native_steer": True,
                    **metadata,
                }),
            ])
            await release_transition(transition_ready)
            if stopped_during_handoff:
                with suppress(Exception):
                    await interrupt_claude_sdk_run_bounded(candidate_handle)
            RUNS.run_metadata.pop(previous_run_id, None)
            RUNS.stopped_runs.discard(previous_run_id)
            if isinstance(future, asyncio.Future) and not future.done():
                future.set_result({
                    "ok": True,
                    "queued_id": selected.get("queued_id"),
                    "run_id": candidate_run_id,
                    "interrupted": prior_aborted,
                    "native_steer": True,
                    "replays_interrupted_message": False,
                    "superseded_queued_ids": [],
                })
            # Keep the accepted handoff visible to cancellation/error cleanup
            # until its HTTP waiter has been resolved. Clearing this earlier
            # can orphan Force Send after provider delivery succeeded but
            # before local timeline bookkeeping completed.
            pending_steer = None
    except asyncio.CancelledError as exc:
        cancelled_error = exc
        retire_supervisor = True
        RUNS.stopped_runs.add(current_run_id)
    except Exception as exc:
        shutdown_interrupted = record_sdk_stream_exception(exc)
        retire_supervisor = True
        if not shutdown_interrupted:
            logger.exception(
                "Claude SDK run failed session=%s run=%s",
                session_id,
                current_run_id,
            )

    async def cleanup_live_sdk_state() -> None:
        nonlocal retire_supervisor, stream_error
        unhandled_steer: dict[str, Any] | None = None
        readiness_tasks = tuple(provider_ready_tasks)
        for task in readiness_tasks:
            if not task.done():
                task.cancel()
        if readiness_tasks:
            await asyncio.gather(*readiness_tasks, return_exceptions=True)
        if (
            pending_steer is None
            and steer_task is not None
            and steer_task.done()
            and not steer_task.cancelled()
        ):
            with suppress(Exception):
                candidate = steer_task.result()
                if isinstance(candidate, dict):
                    unhandled_steer = candidate
        for task in (message_task, steer_task, first_activity_task):
            if task is not None and not task.done():
                task.cancel()
        if (
            message_task is not None
            or steer_task is not None
            or first_activity_task is not None
        ):
            await asyncio.gather(
                *(
                    task
                    for task in (
                        message_task,
                        steer_task,
                        first_activity_task,
                    )
                    if task is not None
                ),
                return_exceptions=True,
            )
        if pending_steer is not None:
            transition_ready = pending_steer.get("transition_ready")
            future = pending_steer.get("future")
            candidate_accepted = bool(
                pending_steer.get("candidate_accepted")
            )
            candidate_delivery_uncertain = bool(
                candidate_accepted
                or (
                    pending_steer.get("candidate_query_attempted")
                    and not pending_steer.get("candidate_failure_handled")
                )
            )
            candidate_run_id = str(
                pending_steer.get("candidate_run_id") or ""
            )
            if (
                candidate_run_id
                and pending_steer.get("candidate_authority_path") is not None
            ):
                with suppress(Exception):
                    await revoke_provider_authority(candidate_run_id)
                pending_steer.pop("candidate_authority_path", None)
            if candidate_delivery_uncertain:
                predecessor_run_id = str(
                    pending_steer.get("predecessor_run_id") or ""
                )
                if predecessor_run_id:
                    with suppress(Exception):
                        await revoke_provider_authority(predecessor_run_id)
            if isinstance(transition_ready, asyncio.Event):
                await release_transition(transition_ready)
            if candidate_delivery_uncertain:
                retire_supervisor = True
                stream_error = stream_error or (
                    "Claude steering may have been accepted, but its local "
                    "handoff did not complete. The message was not replayed."
                )
                candidate_baseline = pending_steer.get("candidate_baseline")
                candidate_paths = pending_steer.get("candidate_paths")
                candidate_artifacts = pending_steer.get("candidate_artifacts")
                if (
                    candidate_run_id
                    and isinstance(candidate_baseline, dict)
                    and isinstance(candidate_paths, set)
                    and isinstance(candidate_artifacts, set)
                ):
                    with suppress(Exception):
                        await finish_outputs(
                            candidate_run_id,
                            candidate_baseline,
                            candidate_paths,
                            candidate_artifacts,
                            manifest_path,
                        )
            if isinstance(future, asyncio.Future) and not future.done():
                future.set_exception(NativeSteerHandoffError(
                    stream_error or "Claude steering ended before delivery",
                    safe_to_requeue=not candidate_delivery_uncertain,
                    delivery_uncertain=candidate_delivery_uncertain,
                ))
        elif unhandled_steer is None:
            # A queue.put() can win just after asyncio.wait() selects the
            # terminal ResultMessage. Drain the detached queue so its HTTP
            # waiter is never stranded after ACTIVE is released.
            with suppress(asyncio.QueueEmpty):
                candidate = native_steer_queue.get_nowait()
                if isinstance(candidate, dict):
                    unhandled_steer = candidate
        if unhandled_steer is not None:
            future = unhandled_steer.get("future")
            if isinstance(future, asyncio.Future) and not future.done():
                future.set_exception(NativeSteerHandoffError(
                    "Claude finished before the steering message was delivered",
                    safe_to_requeue=True,
                ))
        if manifest_watch_task is not None:
            manifest_watch_task.cancel()
            await asyncio.gather(manifest_watch_task, return_exceptions=True)
        await cancel_claude_interactions(
            session_id,
            resolution=(
                "turn_stopped"
                if cancelled_error is not None or current_run_id in shutdown_interrupted_run_ids
                else "turn_finished"
            ),
            expected_run_id=current_run_id,
            ownership_token=sdk_ownership_token or None,
        )

    cleanup_task = asyncio.create_task(cleanup_live_sdk_state())
    try:
        await asyncio.shield(cleanup_task)
    except asyncio.CancelledError as exc:
        cancelled_error = cancelled_error or exc
        retire_supervisor = True
        RUNS.stopped_runs.add(current_run_id)
        await join_task_despite_caller_cancellation(cleanup_task)

    async def finalize_sdk_run() -> None:
        nonlocal current_reconciliation_batches, cancelled_error
        if retire_supervisor:
            with suppress(Exception):
                await interrupt_claude_sdk_run_bounded(current_handle)
            # Token-qualified eviction cannot target a newer SDK generation.
            # Without a supervisor token, fail closed: interrupting the exact
            # handle above is safe, while chat-wide eviction is not.
            if sdk_ownership_token:
                await evict_claude_sdk_chat(
                    session_id,
                    force=True,
                    manager=manager,
                    ownership_token=sdk_ownership_token or None,
                )
        if provider_id:
            with suppress(Exception):
                await persist_run_provider_session(
                    session_id,
                    current_run_id,
                    BACKEND_CLAUDE,
                    provider_id,
                    cwd=cwd,
                )

        # Native goal verdicts are persisted before the terminal result. The
        # provider, not an app-owned continuation loop, decides when to finish.
        with suppress(Exception):
            await refresh_claude_goal(session_id, provider_id=provider_id)
            if (CLAUDE_GOAL_PENDING.get(session_id) or {}).get("run_id") == current_run_id:
                CLAUDE_GOAL_PENDING.pop(session_id, None)
                await append_event(session_id, "claude_goal_changed", {})

        stopped = bool(
            cancelled_error is not None
            or current_run_id in RUNS.stopped_runs
            or (result_details or {}).get("aborted")
        )
        try:
            current_reconciliation_batches = await acknowledge_claude_background_reconciliation(
                session_id, current_run_id, provider_id, current_handle,
                current_reconciliation_batches,
            )
            if current_run_id not in receipts_persisted_run_ids:
                await persist_claude_background_task_receipts(
                    session_id, current_run_id, provider_id, current_handle,
                    tracking_lost=stopped or retire_supervisor,
                )
        except asyncio.CancelledError as exc:
            cancelled_error = cancelled_error or exc
            stopped = True
        except Exception:
            # A failed receipt write must not retain the parent execution
            # slot. Its unconsumed checkpoint remains available for retry.
            logger.exception("Claude background task reconciliation persistence failed")
        result_text = clean_assistant_text(
            str((result_details or {}).get("result_text") or "")
        )
        result_error = str((result_details or {}).get("error") or "")
        projection_error = current_run_id in projection_error_run_ids
        empty_result_error = claude_empty_turn_failure_message(
            prompt=current_prompt,
            result_text=result_text,
            had_tool_activity=current_run_id in tool_activity_run_ids,
            stopped=stopped,
            terminal_result_received=result_details is not None,
            existing_error=bool(
                stream_error
                or result_error
                or (result_details or {}).get("is_error")
                or projection_error
            ),
        )
        if stream_error and not stopped:
            with suppress(Exception):
                await append_event(session_id, "error", {
                    "run_id": current_run_id,
                    "backend": BACKEND_CLAUDE,
                    "transport": CLAUDE_TRANSPORT_AGENT_SDK,
                    "message": f"Claude SDK stream failed: {stream_error}",
                    **({"delivery_unknown": True} if delivery_unknown else {}),
                    **run_event_metadata(current_run_id),
                })
        if result_error and not stopped:
            with suppress(Exception):
                await append_event(session_id, "error", {
                    "run_id": current_run_id,
                    "backend": BACKEND_CLAUDE,
                    "transport": CLAUDE_TRANSPORT_AGENT_SDK,
                    "message": result_error,
                    **run_event_metadata(current_run_id),
                })
        if empty_result_error and not stopped:
            with suppress(Exception):
                await append_event(session_id, "error", {
                    "run_id": current_run_id,
                    "backend": BACKEND_CLAUDE,
                    "transport": CLAUDE_TRANSPORT_AGENT_SDK,
                    "message": empty_result_error,
                    **run_event_metadata(current_run_id),
                })
        if (
            stream_error
            or (result_details or {}).get("is_error")
            or projection_error
            or empty_result_error
        ):
            if not stopped:
                record_runtime_failure(
                    BACKEND_CLAUDE,
                    stream_error
                    or result_error
                    or empty_result_error
                    or "Claude assistant error",
                )
        elif not stopped:
            record_runtime_success(BACKEND_CLAUDE)
        with suppress(Exception):
            await finish_outputs(
                current_run_id,
                current_diff_baseline,
                changed_paths,
                seen_artifacts,
                manifest_path,
            )
            await collect_recent_leftover_manifests(
                session_id,
                current_run_id,
                manifest_path,
                seen_artifacts=seen_artifacts,
            )
        drain_queue = should_schedule_queue_after_finish(session_id, stopped)
        released = await release_turn_slot(
            session_id,
            expected_run_id=current_run_id,
        )
        if released:
            try:
                await append_turn_finished_event(session_id, {
                    "run_id": current_run_id,
                    "backend": BACKEND_CLAUDE,
                    "transport": CLAUDE_TRANSPORT_AGENT_SDK,
                    "provider_session_id": provider_id or None,
                    "exit_code": (
                        None
                        if stopped
                        else 1 if (
                            stream_error
                            or result_error
                            or projection_error
                            or empty_result_error
                        ) else 0
                    ),
                    "result_text": result_text,
                    "stopped": stopped,
                    **({"reason": "server_shutdown"} if current_run_id in shutdown_interrupted_run_ids else {}),
                    **({"delivery_unknown": True} if delivery_unknown else {}),
                    **run_event_metadata(current_run_id),
                })
            except Exception:
                # Retire ownership, but do not automatically run more work
                # after its predecessor's completion could not be recorded.
                drain_queue = False
                logger.exception("Claude completion recording failed; automatic queue drain deferred")
        RUNS.run_metadata.pop(current_run_id, None)
        RUNS.stopped_runs.discard(current_run_id)
        if released and drain_queue:
            schedule_next_queued_turn(session_id)

    finalize_task = asyncio.create_task(finalize_sdk_run())
    try:
        await asyncio.shield(finalize_task)
    except asyncio.CancelledError as exc:
        cancelled_error = cancelled_error or exc
        retire_supervisor = True
        RUNS.stopped_runs.add(current_run_id)
        await join_task_despite_caller_cancellation(finalize_task)
    if cancelled_error is not None:
        raise cancelled_error


async def run_claude(
    session_id: str,
    run_id: str,
    prompt: str,
    sess: dict[str, Any],
    manifest_path: Path,
    *,
    interactive_agent_sdk: bool = False,
    standalone_provider_context: bool = False,
    provider_command: ProviderCommandRecord | None = None,
    provider_runtime_env: dict[str, str] | None = None,
) -> None:
    use_sdk = bool(
        interactive_agent_sdk
        and not standalone_provider_context
        and CLAUDE_TRANSPORT != CLAUDE_TRANSPORT_PRINT
    )
    if not use_sdk:
        if provider_command is not None:
            await finish_claude_sdk_start_failure(
                session_id,
                run_id,
                "Claude provider commands require the interactive Agent SDK transport.",
            )
            return
        # A compatibility print turn advances the same provider conversation
        # outside the in-memory SDK client. Retire an idle supervisor first so
        # a later desktop SDK turn reconnects from the newly persisted ID
        # instead of continuing stale process-local context.
        if not await evict_claude_sdk_chat(session_id, force=True):
            await finish_claude_sdk_start_failure(
                session_id,
                run_id,
                (
                    "Claude's previous chat process is still shutting down. "
                    "No compatibility turn was started; retry shortly."
                ),
            )
            return
        if not standalone_provider_context:
            # The print transport advances the same Claude conversation but
            # cannot provide an authoritative live context sample. Never keep
            # advertising a stale SDK measurement after this compatibility
            # path wins.
            await mark_provider_context_usage_unavailable(
                session_id,
                BACKEND_CLAUDE,
            )
        await run_claude_print(
            session_id,
            run_id,
            prompt,
            sess,
            manifest_path,
            standalone_provider_context=standalone_provider_context,
            provider_runtime_env=provider_runtime_env,
        )
        return
    try:
        await run_claude_sdk(
            session_id,
            run_id,
            prompt,
            sess,
            manifest_path,
            provider_runtime_env=provider_runtime_env,
            **(
                {"provider_command": provider_command}
                if provider_command is not None
                else {}
            ),
        )
    except ClaudeSDKUnavailable as exc:
        # Interactive desktop clients opted into approval/question semantics.
        # The legacy print path deliberately skips permissions, so silently
        # downgrading an opted-in turn would execute with weaker safety than
        # the UI promised. Old clients and standalone jobs still select print
        # before entering this function and remain fully compatible.
        await finish_claude_sdk_start_failure(
            session_id,
            run_id,
            f"Claude Agent SDK is unavailable: {concise_error_message(exc)}",
        )


async def run_codex_exec(
    session_id: str,
    run_id: str,
    prompt: str,
    sess: dict[str, Any],
    manifest_path: Path,
    *,
    allow_compaction_rollover: bool = True,
    diff_baseline: dict[str, str] | None = None,
    standalone_provider_context: bool = False,
    provider_runtime_env: dict[str, str] | None = None,
) -> None:
    runtime_env = validate_provider_runtime_env(provider_runtime_env)
    if not standalone_provider_context:
        # codex exec exposes no authoritative live context measurement. This
        # also covers the automatic app-server -> exec compatibility fallback.
        await mark_codex_exec_context_usage_unavailable(session_id)
    if standalone_provider_context:
        sess = standalone_provider_session(sess)
    resumed_provider_id = session_provider_id(sess)
    memory_seed = str(sess.get("memory_seed") or "").strip()
    if memory_seed and (not sess.get("memory_seed_used") or not resumed_provider_id):
        prompt = build_memory_augmented_prompt(
            "Fork memory context",
            memory_seed,
            prompt,
        )
        async with STORE.transaction():
            current = STORE.sessions.get(session_id)
            if current:
                current["memory_seed_used"] = True
                current["updated_at"] = now_iso()
                sess = dict(current)
                await STORE.save()
        await append_event(session_id, "history_imported", {
            "run_id": run_id,
            "backend": BACKEND_CODEX,
            "message": "Applied memory fork context to this first Codex exec turn.",
        })
    cmd = build_codex_cmd(
        session_id,
        sess,
        prompt,
        manifest_path,
        disable_provider_subagents=bool(runtime_env),
    )
    requested_cwd = str(sess.get("cwd") or DEFAULT_CWD)
    cwd = existing_cwd(requested_cwd)
    if diff_baseline is None:
        diff_baseline = await capture_git_baseline(session_id, run_id, cwd)
    if str(Path(requested_cwd).expanduser()) != cwd:
        await append_event(session_id, "cwd_fallback", {"run_id": run_id, "requested_cwd": requested_cwd, "cwd": cwd})
    public_cmd = redacted_provider_argv(cmd, BACKEND_CODEX)
    await append_event(session_id, "process_started", {"run_id": run_id, "backend": BACKEND_CODEX, "argv": public_cmd, "cwd": cwd})
    env = agent_runner_env(session_id, runtime_env)
    codex_dir = os.path.dirname(os.path.abspath(CODEX_BIN))
    if codex_dir and codex_dir not in env.get("PATH", "").split(os.pathsep):
        env["PATH"] = codex_dir + os.pathsep + env.get("PATH", "")
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            env=env,
            limit=PROCESS_STREAM_LIMIT,
            start_new_session=True,
        )
    except Exception as e:
        released = await release_turn_slot(
            session_id,
            expected_run_id=run_id,
        )
        try:
            if released:
                record_runtime_failure(BACKEND_CODEX, e, spawn_failure=True)
                await append_event(session_id, "error", {"run_id": run_id, "backend": BACKEND_CODEX, "message": f"failed to start Codex: {e}", **run_event_metadata(run_id)})
                await append_turn_finished_event(session_id, {
                    "run_id": run_id,
                    "backend": BACKEND_CODEX,
                    "exit_code": None,
                    "result_text": "",
                    **run_event_metadata(run_id),
                })
        finally:
            RUNS.run_metadata.pop(run_id, None)
            RUNS.stopped_runs.discard(run_id)
            if released:
                schedule_next_queued_turn(session_id)
        return
    pgid = process_group_for_pid(proc.pid)
    bound, stop_requested = await bind_active_turn(
        session_id,
        run_id,
        {
            "proc": proc,
            "run_id": run_id,
            "backend": BACKEND_CODEX,
            "transport": CODEX_TRANSPORT_EXEC,
            "pid": proc.pid,
            "pgid": pgid,
            "cwd": cwd,
            "argv": public_cmd,
            "started_at": time.time(),
            "started_at_iso": now_iso(),
            "provider_turn_ready": False,
            "stdout_tail": deque(maxlen=LIVE_STDOUT_MAX_LINES),
            "stdout_total_lines": 0,
            "stdout_updated_at": None,
        },
    )
    if not bound:
        await terminate_process_tree(proc, grace=0.5)
        return
    if stop_requested:
        await terminate_process_tree(proc)

    text_parts: list[str] = []
    provider_id: str | None = sess.get("codex_thread_id") or (
        sess.get("session_id") if sess.get("backend") == BACKEND_CODEX else None
    )
    last_event = time.time()
    resume_started_at = last_event
    idle_killed = False
    resume_stalled = False
    stream_error: str | None = None
    codex_error: str | None = None
    seen_artifacts: set[str] = set()
    seen_raw_lines: set[str] = set()
    seen_text_parts: set[str] = set()
    seen_reasoning: set[str] = set()
    message_buffer = CodexExecMessageBuffer()
    tool_calls: dict[str, dict[str, Any]] = {}
    started_tool_ids: set[str] = set()
    finished_tool_ids: set[str] = set()
    changed_paths: set[str] = set()
    codex_history_path = find_codex_history(provider_id) if provider_id else None
    codex_history_pos = codex_history_path.stat().st_size if codex_history_path and codex_history_path.exists() else 0
    manifest_watch_task = asyncio.create_task(watch_manifest_artifacts(session_id, run_id, manifest_path, seen_artifacts))

    async def emit_provider_session(new_provider_id: str) -> None:
        nonlocal provider_id, codex_history_path, codex_history_pos
        if not new_provider_id:
            return
        provider_id = new_provider_id
        await persist_run_provider_session(
            session_id,
            run_id,
            BACKEND_CODEX,
            provider_id,
            standalone_provider_context=standalone_provider_context,
        )
        if not codex_history_path:
            codex_history_path = find_codex_history(provider_id)
            codex_history_pos = codex_history_path.stat().st_size if codex_history_path and codex_history_path.exists() else 0

    async def emit_assistant_text(text: str) -> None:
        text = clean_assistant_text(text)
        if not text or text in seen_text_parts:
            return
        seen_text_parts.add(text)
        text_parts.append(text)
        await append_event(session_id, "assistant_text", {"run_id": run_id, "text": text, **run_event_metadata(run_id)})

    async def emit_reasoning_text(text: str, *, phase: str = "") -> None:
        text = str(text or "").strip()
        if not text or text in seen_reasoning:
            return
        seen_reasoning.add(text)
        payload = {"run_id": run_id, "text": text}
        if phase:
            payload["phase"] = phase
        await append_event(session_id, "reasoning_summary", payload)

    async def emit_buffered_messages(
        messages: list[tuple[str, str]],
    ) -> None:
        for phase, text in messages:
            if phase == "final_answer":
                await emit_assistant_text(text)
            else:
                await emit_reasoning_text(text, phase="commentary")

    async def observe_agent_message(text: str, phase: str = "") -> None:
        await emit_buffered_messages(message_buffer.observe(text, phase))

    async def flush_agent_message(*, final: bool) -> None:
        await emit_buffered_messages(message_buffer.flush(final=final))

    async def emit_tool_started(tool: dict[str, Any]) -> None:
        await flush_agent_message(final=False)
        tool_id = str(tool.get("id") or f"tool_{uuid.uuid4().hex[:8]}")
        tool["id"] = tool_id
        tool_calls[tool_id] = tool
        changed_paths.update(tool_changed_paths(tool))
        if tool_id in started_tool_ids:
            return
        started_tool_ids.add(tool_id)
        await append_event(session_id, "tool_started", {"run_id": run_id, "tool": tool})

    async def emit_tool_finished(tool_id: str, output: Any, exit_code: int | None = None) -> None:
        await flush_agent_message(final=False)
        tool_id = str(tool_id or f"tool_{uuid.uuid4().hex[:8]}")
        if tool_id in finished_tool_ids:
            return
        finished_tool_ids.add(tool_id)
        output_text = codex_output_text(output)
        tool = tool_calls.get(tool_id) or {"id": tool_id, "name": "Tool", "input": {}}
        if exit_code is None:
            exit_code = codex_output_exit_code(output_text)
        await append_event(session_id, "tool_finished", {
            "run_id": run_id,
            "tool_id": tool_id,
            "tool": tool,
            "output": output_text,
            "exit_code": exit_code,
        })

    async def handle_codex_event(event: dict[str, Any]) -> bool:
        handled = False
        etype = str(event.get("type") or "")
        result_error = codex_result_error(event)
        if result_error:
            nonlocal codex_error
            codex_error = result_error
            return True
        if etype == "thread.started" and event.get("thread_id"):
            await emit_provider_session(str(event["thread_id"]))
            return True
        if etype == "turn.started":
            await mark_provider_turn_ready(session_id, run_id, provider_id)
            return True
        agent_message = codex_exec_agent_message(event)
        if agent_message is not None:
            await observe_agent_message(*agent_message)
            return True
        if etype in ("item.started", "item.completed"):
            item = event.get("item", {}) or {}
            itype = item.get("type", "")
            if itype == "command_execution":
                tool = {
                    "id": item.get("id") or f"cmd_{uuid.uuid4().hex[:8]}",
                    "name": "Bash",
                    "input": {"command": item.get("command", "")},
                }
                if etype == "item.started":
                    await emit_tool_started(tool)
                else:
                    await emit_tool_finished(str(tool["id"]), item.get("aggregated_output", ""), item.get("exit_code"))
                handled = True
            elif itype in ("reasoning", "agent_reasoning") and etype == "item.completed":
                await flush_agent_message(final=False)
                await emit_reasoning_text(codex_reasoning_text(item))
                handled = True
            return handled
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        if etype == "event_msg":
            payload_type = str(payload.get("type") or "")
            if payload_type == "agent_reasoning":
                await flush_agent_message(final=False)
                await emit_reasoning_text(str(payload.get("text") or ""))
                return True
            return False
        if etype == "response_item":
            payload_type = str(payload.get("type") or "")
            if payload_type in {"function_call", "custom_tool_call"}:
                call_id = str(payload.get("call_id") or payload.get("id") or f"tool_{uuid.uuid4().hex[:8]}")
                arguments = payload.get("arguments") if "arguments" in payload else payload.get("input")
                await emit_tool_started(codex_call_tool(call_id, str(payload.get("name") or "tool"), arguments))
                return True
            if payload_type in {"function_call_output", "custom_tool_call_output"}:
                await emit_tool_finished(str(payload.get("call_id") or payload.get("id") or ""), payload.get("output"))
                return True
            if payload_type in {"reasoning", "agent_reasoning"}:
                await flush_agent_message(final=False)
                await emit_reasoning_text(codex_reasoning_text(payload))
                return True
        return False

    async def handle_codex_line(line: str, *, record_raw: bool) -> bool:
        line = line.strip()
        if not line or not line.startswith("{") or line in seen_raw_lines:
            return False
        seen_raw_lines.add(line)
        try:
            event = json.loads(line)
        except Exception:
            raw_event = codex_exec_raw_event_text(line, handled=False)
            if record_raw and raw_event:
                await append_event(session_id, "raw_event", {
                    "run_id": run_id,
                    "backend": BACKEND_CODEX,
                    "raw": raw_event,
                })
            return False
        handled = await handle_codex_event(event)
        raw_event = codex_exec_raw_event_text(line, handled=handled)
        if record_raw and raw_event:
            await append_event(session_id, "raw_event", {
                "run_id": run_id,
                "backend": BACKEND_CODEX,
                "raw": raw_event,
            })
        return handled

    async def drain_codex_history() -> bool:
        nonlocal codex_history_path, codex_history_pos
        if not codex_history_path and provider_id:
            codex_history_path = find_codex_history(provider_id)
            codex_history_pos = codex_history_path.stat().st_size if codex_history_path and codex_history_path.exists() else 0
        if not codex_history_path or not codex_history_path.exists():
            return False
        try:
            size = codex_history_path.stat().st_size
            if size < codex_history_pos:
                codex_history_pos = 0
            if size <= codex_history_pos:
                return False
            with codex_history_path.open("r", encoding="utf-8", errors="ignore") as f:
                f.seek(codex_history_pos)
                lines = f.readlines()
                codex_history_pos = f.tell()
        except OSError:
            return False
        consumed = False
        for history_line in lines:
            if await handle_codex_line(history_line, record_raw=False):
                consumed = True
            elif history_line.strip():
                consumed = True
        return consumed

    try:
        while True:
            try:
                raw = await asyncio.wait_for(proc.stdout.readline(), timeout=5)  # type: ignore[union-attr]
            except asyncio.TimeoutError:
                if await drain_codex_history():
                    last_event = time.time()
                produced_activity = bool(
                    text_parts
                    or seen_reasoning
                    or message_buffer.has_pending
                    or started_tool_ids
                    or finished_tool_ids
                    or seen_artifacts
                )
                if (
                    resumed_provider_id
                    and CODEX_RESUME_ACTIVITY_TIMEOUT_SECONDS > 0
                    and not produced_activity
                    and time.time() - resume_started_at >= CODEX_RESUME_ACTIVITY_TIMEOUT_SECONDS
                ):
                    resume_stalled = True
                    logger.warning(
                        "Codex resume produced no activity before startup timeout session=%s run=%s provider=%s timeout=%ss",
                        session_id,
                        run_id,
                        resumed_provider_id,
                        CODEX_RESUME_ACTIVITY_TIMEOUT_SECONDS,
                    )
                    await terminate_process_tree(proc)
                    break
                idle = time.time() - last_event
                if idle >= IDLE_WARN_SECONDS:
                    await append_event(session_id, "idle_warning", {"run_id": run_id, "idle_seconds": int(idle)})
                if idle >= IDLE_KILL_SECONDS:
                    idle_killed = True
                    await terminate_process_tree(proc)
                    break
                continue
            if not raw:
                break
            last_event = time.time()
            decoded = raw.decode("utf-8", "replace").rstrip("\r\n")
            await append_active_stdout(session_id, decoded)
            line = decoded.strip()
            if line.startswith("{"):
                await handle_codex_line(line, record_raw=True)
            if await drain_codex_history():
                last_event = time.time()
    except Exception as e:
        stream_error = f"{type(e).__name__}: {e}"
        logger.exception("Codex run failed session=%s run=%s", session_id, run_id)
    finally:
        with suppress(Exception):
            await drain_codex_history()
        manifest_watch_task.cancel()
        with suppress(asyncio.CancelledError):
            await manifest_watch_task
        await terminate_process_tree(proc, grace=0.5)
        await clear_active_process(session_id, expected_run_id=run_id)

    stderr = ""
    if proc.stderr:
        stderr = (await proc.stderr.read()).decode("utf-8", "replace").strip()
    stopped = run_id in RUNS.stopped_runs
    await flush_agent_message(
        final=(
            not stopped
            and not stream_error
            and not idle_killed
            and not resume_stalled
            and not codex_error
            and proc.returncode in (0, None)
        )
    )
    terminal_error = codex_error or stderr
    produced_activity = bool(
        text_parts
        or seen_reasoning
        or started_tool_ids
        or finished_tool_ids
        or seen_artifacts
    )
    silent_completion = is_silent_codex_completion(
        stopped=stopped,
        stream_error=stream_error,
        idle_killed=idle_killed,
        returncode=proc.returncode,
        produced_activity=produced_activity,
    )
    should_rollover = should_recover_codex_resume(
        allow_rollover=allow_compaction_rollover,
        resumed_provider_id=resumed_provider_id,
        stopped=stopped,
        stream_error=stream_error,
        idle_killed=idle_killed,
        resume_stalled=resume_stalled,
        returncode=proc.returncode,
        produced_activity=produced_activity,
        terminal_error=terminal_error,
    )
    if should_rollover:
        recovery_reason = (
            f"Codex resume produced no activity for {CODEX_RESUME_ACTIVITY_TIMEOUT_SECONDS} seconds."
            if resume_stalled
            else terminal_error or "Codex resume exited successfully without producing any response events."
        )
        if resume_stalled:
            recovery_message = "Codex resume stalled before producing a reply. Retrying this turn once on a fresh thread with bounded recent memory."
        elif silent_completion:
            recovery_message = "Codex resume produced no reply. Retrying this turn once on a fresh thread with bounded recent memory."
        else:
            recovery_message = "Codex could not continue the provider thread. Retrying this turn once on a fresh thread with bounded recent memory."
        logger.warning(
            "recovering empty Codex resume session=%s run=%s provider=%s exit=%s reason=%s",
            session_id,
            run_id,
            resumed_provider_id,
            proc.returncode,
            compact_memory_text(recovery_reason, 500),
        )
        rollover = await rollover_codex_provider_session(
            session_id,
            run_id,
            str(resumed_provider_id),
            recovery_reason,
            message=recovery_message,
        )
        if rollover:
            fresh_session, memory = rollover
            retry_prompt = build_memory_augmented_prompt(
                "Codex rollover memory",
                memory,
                prompt,
            )
            await run_codex_exec(
                session_id,
                run_id,
                retry_prompt,
                fresh_session,
                manifest_path,
                allow_compaction_rollover=False,
                diff_baseline=diff_baseline,
                standalone_provider_context=standalone_provider_context,
                provider_runtime_env=runtime_env,
            )
            return
    if stream_error and not stopped:
        await append_event(session_id, "error", {"run_id": run_id, "message": f"Codex stream failed: {stream_error}", **run_event_metadata(run_id)})
    if idle_killed:
        await append_event(session_id, "error", {"run_id": run_id, "message": "killed after idle timeout", **run_event_metadata(run_id)})
    if resume_stalled:
        await append_event(session_id, "error", {
            "run_id": run_id,
            "backend": BACKEND_CODEX,
            "message": f"Codex resume produced no activity for {CODEX_RESUME_ACTIVITY_TIMEOUT_SECONDS} seconds.",
            "exit_code": proc.returncode,
            **run_event_metadata(run_id),
        })
    elif not stopped and proc.returncode not in (0, None):
        if codex_error:
            await append_event(session_id, "error", {"run_id": run_id, "message": codex_error, "exit_code": proc.returncode, **run_event_metadata(run_id)})
        elif stderr:
            await append_event(session_id, "error", {"run_id": run_id, "message": stderr[:4000], "exit_code": proc.returncode, **run_event_metadata(run_id)})
        else:
            await append_event(session_id, "error", {"run_id": run_id, "message": f"Codex exited {proc.returncode} without error output.", "exit_code": proc.returncode, **run_event_metadata(run_id)})
    elif silent_completion:
        await append_event(session_id, "error", {
            "run_id": run_id,
            "backend": BACKEND_CODEX,
            "message": "Codex exited without producing a reply. Retry the message; if this was a resumed external thread, create a fresh memory-backed continuation.",
            "exit_code": proc.returncode,
            **run_event_metadata(run_id),
        })
    if not stopped and (stream_error or idle_killed or resume_stalled or proc.returncode not in (0, None) or silent_completion):
        record_runtime_failure(BACKEND_CODEX, terminal_error or stream_error or f"exit {proc.returncode}")
    elif not stopped:
        record_runtime_success(BACKEND_CODEX)
    if provider_id and not standalone_provider_context:
        await persist_run_provider_session(
            session_id,
            run_id,
            BACKEND_CODEX,
            provider_id,
        )
    await collect_manifest(session_id, run_id, manifest_path, seen_artifacts=seen_artifacts, final=True)
    await collect_recent_leftover_manifests(session_id, run_id, manifest_path, seen_artifacts=seen_artifacts)
    await publish_turn_code_diff(session_id, run_id, BACKEND_CODEX, cwd, diff_baseline, changed_paths)
    terminal_payload = {
        "run_id": run_id,
        "backend": BACKEND_CODEX,
        "exit_code": proc.returncode,
        "result_text": clean_assistant_text("\n\n".join(text_parts).strip()),
        "stopped": stopped,
        **run_event_metadata(run_id),
    }
    finalize_task = asyncio.create_task(finalize_owned_turn_finished(
        session_id,
        run_id,
        stopped=stopped,
        payload=terminal_payload,
    ))
    try:
        await asyncio.shield(finalize_task)
    except asyncio.CancelledError:
        await join_task_despite_caller_cancellation(finalize_task)
        raise


async def run_codex_app_server(
    session_id: str,
    run_id: str,
    prompt: str,
    sess: dict[str, Any],
    manifest_path: Path,
    *,
    allow_exec_fallback: bool,
    interactive_app_server: bool = False,
    allow_resume_rollover: bool = True,
    allow_compaction_retry: bool = True,
    compaction_continuation: bool = False,
    diff_baseline: dict[str, Any] | None = None,
    standalone_provider_context: bool = False,
    provider_command: ProviderCommandRecord | None = None,
    provider_runtime_env: dict[str, str] | None = None,
) -> None:
    runtime_env = validate_provider_runtime_env(provider_runtime_env)
    if standalone_provider_context:
        sess = standalone_provider_session(sess)
    requested_cwd = str(sess.get("cwd") or DEFAULT_CWD)
    cwd = existing_cwd(requested_cwd)
    approval_policy = (
        effective_codex_approval_policy(sess)
        if interactive_app_server
        else CODEX_NONINTERACTIVE_APPROVAL_POLICY
    )
    if diff_baseline is None:
        diff_baseline = await capture_git_baseline(session_id, run_id, cwd)
    if str(Path(requested_cwd).expanduser()) != cwd:
        await append_event(session_id, "cwd_fallback", {
            "run_id": run_id,
            "requested_cwd": requested_cwd,
            "cwd": cwd,
        })

    public_cmd = [CODEX_BIN, "app-server", "--listen", "stdio://"]
    await append_event(session_id, "process_started", {
        "run_id": run_id,
        "backend": BACKEND_CODEX,
        "transport": CODEX_TRANSPORT_APP_SERVER,
        "argv": public_cmd,
        "cwd": cwd,
    })

    manager = await codex_app_server_manager(sess)
    provider_id = str(session_provider_id(sess) or "")
    resumed_provider_id = provider_id or None
    model, effort, service_tier = codex_runtime_settings(sess)
    thread_pinned = False
    turn = None
    turn_start_attempted = False
    safe_pre_accept_failure = False
    ambiguous_turn_start = False
    ambiguous_accept_deadline = 0.0
    ambiguous_reconcile_at = 0.0
    ambiguous_reconcile_attempted = False
    turn_start_epoch = 0.0
    terminal_status = "failed"
    terminal_error: str | None = None
    pending_compaction_items: set[str] = set()
    compaction_failed = False
    compaction_execution_seen = False
    error_emitted = False
    cancelled_provisional_turn = False
    provisional_thread_invalidated = False
    turn_start_generation = 0
    # Set when the turn was accepted but produced no notification at all
    # within CODEX_APP_SERVER_FIRST_ACTIVITY_TIMEOUT_SECONDS. Treated as a
    # stalled resume below so a resumed thread gets the same one-shot
    # rollover-onto-a-fresh-thread recovery the exec transport already has.
    first_activity_stalled = False
    delivery_unknown = False
    turn_completed = False
    current_run_id = run_id
    current_provider_prompt = prompt
    current_diff_baseline = diff_baseline
    initial_provider_turn_id = ""
    manifest_watch_task: asyncio.Task[None] | None = None
    goal_time_budget_task: asyncio.Task[None] | None = None
    goal_continuation_result: dict[str, Any] | None = None
    pending_goal_steer_handoff: dict[str, Any] | None = None
    goal_steer_recovery_fenced = False
    logical_state_lock = asyncio.Lock()
    steer_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    notification_task: asyncio.Task[tuple[int, dict[str, Any]]] | None = None
    synthetic_notification_sequence = 0
    handled_notification_sequence = 0
    last_activity = time.monotonic()
    cancelled_exit = False
    child_notification_handler: Any = None
    child_continuation_stop = asyncio.Event()
    child_continuation_stop_task: asyncio.Task[bool] | None = None
    child_completion_pending: dict[str, bool] = {}
    received_parent_turn_ids: set[str] = set()
    completed_native_turn_ids: set[str] = set()
    child_continuation_generation = 0
    native_answer_turn_id = ""
    waiting_for_child_continuation = False
    waiting_parent_turn_id = ""
    child_continuation_changes = asyncio.Event()
    child_continuation_change_task: asyncio.Task[bool] | None = None
    child_continuation_attempts: set[str] = set()
    active_child_ids: set[str] = set()
    child_acceptance_parent_id = ""

    def install_child_continuation_observer() -> None:
        """Capture native descendant activity in receive order, without IO.

        Card projection is asynchronous and serialized per child, not across
        the entire family. A child can therefore finish before the ordinary
        consumer processes the preceding parent completion. Keep that exact
        completion's pending flag, rather than consulting a later card cache.
        """
        nonlocal child_notification_handler, child_continuation_generation
        add_handler = getattr(manager, "add_notification_handler", None)
        if standalone_provider_context or not callable(add_handler):
            return
        generation = getattr(manager, "generation", None)
        if type(generation) is not int or generation < 1:
            return
        child_continuation_generation = generation
        owner = RUNS.active.get(session_id)
        known_children: set[str] = set()
        active_children = active_child_ids
        child_turn_ids: dict[str, str] = {}
        child_activity_items: set[tuple[str, str]] = set()
        # Force Send preserves running children while replacing their parent
        # logical turn. Only a live native-generation proof can seed the new
        # supervisor; old persisted cards never manufacture pending work.
        with CODEX_SUBAGENT_INDEX_LOCK:
            for child, state in CODEX_SUBAGENT_STATE.items():
                if (
                    child != provider_id
                    and CODEX_SUBAGENT_SESSION_INDEX.get(child) == session_id
                    and CODEX_SUBAGENT_LIVE_GENERATIONS.get(child) == generation
                    and CODEX_SUBAGENT_LIVE_MANAGERS.get(child) is manager
                    and state.get("subagent_status") in {"starting", "running"}
                ):
                    known_children.add(child)
                    active_children.add(child)

        def observe_native(notification: dict[str, Any]) -> None:
            current = RUNS.active.get(session_id)
            session = STORE.sessions.get(session_id)
            if not (
                current is owner and current
                and current.get("run_id") == current_run_id
                and current.get("provider_thread_id") == provider_id
                and (RUNS.current_turns.get(session_id) or {}).get("run_id") == current_run_id
                and session_id in RUNS.busy
                and session is not None and not session.get("archived")
                and not current.get("stop_requested")
                and current_run_id not in RUNS.stopped_runs
                and getattr(manager, "generation", None) == generation
            ):
                return
            method = notification.get("method")
            params = notification.get("params")
            if not isinstance(params, dict):
                return
            thread = params.get("threadId")
            if method == "thread/started" and isinstance(params.get("thread"), dict):
                started = params["thread"]
                child = started.get("id")
                _nickname, _path, parent = codex_subagent_thread_identity(started)
                if isinstance(child, str) and child and child != provider_id and (
                    parent == provider_id or parent in known_children
                ):
                    known_children.add(child)
                    if codex_child_status_from_thread(started.get("status")) in {None, "running"}:
                        active_children.add(child)
                return
            if thread != provider_id and thread not in known_children:
                return
            turn_value = params.get("turn")
            turn_id = params.get("turnId") or (
                turn_value.get("id") if isinstance(turn_value, dict) else None
            )
            if thread == provider_id and method == "turn/completed" and isinstance(turn_id, str):
                child_completion_pending.setdefault(turn_id, bool(active_children))
            elif method == "turn/started" and isinstance(turn_id, str) and turn_id:
                if thread == provider_id:
                    if turn is not None and (
                        turn_id in getattr(turn, "_completed_turn_ids", set())
                        or (turn.turn_id and turn.turn_id != turn_id)
                    ):
                        return
                    received_parent_turn_ids.add(turn_id)
                    # The retained native handle has already rebound at the
                    # router. Make Stop/Force Send see that exact new turn too.
                    current["provider_turn_id"] = turn_id
                    current["provider_turn_ready"] = True
                    current["codex_child_continuation_waiting"] = False
                    current["native_interrupt_sent"] = False
                else:
                    child_turn_ids[thread] = turn_id
                    active_children.add(thread)
            elif thread != provider_id and method in {"turn/completed", "thread/closed", "thread/status/changed"}:
                if method == "turn/completed" and child_turn_ids.get(thread) not in {None, turn_id}:
                    return
                if method == "thread/status/changed":
                    status = codex_child_status_from_thread(params.get("status"))
                    if status == "running":
                        active_children.add(thread)
                    elif status:
                        active_children.discard(thread)
                else:
                    active_children.discard(thread)
            if method not in {"item/started", "item/completed"}:
                return
            item = params.get("item")
            if not isinstance(item, dict):
                return
            if item.get("type") == "subAgentActivity":
                child = item.get("agentThreadId")
                if isinstance(child, str) and child and child != provider_id:
                    key = (child, str(item.get("id") or ""))
                    if key in child_activity_items:
                        return
                    child_activity_items.add(key)
                    known_children.add(child)
                    if item.get("kind") == "interrupted":
                        active_children.discard(child)
                    elif item.get("kind") == "started":
                        active_children.add(child)
                return
            if item.get("type") not in {"collabAgentToolCall", "collabToolCall"}:
                return
            operation = re.sub(r"[^a-z0-9]", "", str(item.get("tool") or "").lower())
            states = codex_collaboration_states(item)
            for child in dict.fromkeys([*codex_collaboration_thread_ids(item), *states]):
                if child == provider_id:
                    continue
                was_known = child in known_children
                known_children.add(child)
                state = states.get(child) or {}
                status = normalize_subagent_status(state.get("status")) if state.get("status") is not None else None
                if status and status not in {"starting", "running"}:
                    active_children.discard(child)
                elif operation in {"closeagent", "interruptagent"} and method == "item/completed":
                    active_children.discard(child)
                elif operation in {"spawnagent", "resumeagent", "sendinput", "sendmessage", "followuptask"}:
                    key = (child, str(item.get("id") or ""))
                    if key not in child_activity_items:
                        child_activity_items.add(key)
                        # A very fast new child may finish before spawn's
                        # completed tool packet. Do not revive that child.
                        if (
                            operation == "spawnagent" and (not was_known or child in active_children)
                        ) or (
                            operation != "spawnagent" and status in {"starting", "running"}
                        ):
                            active_children.add(child)

        def observe(notification: dict[str, Any]) -> None:
            active_before = len(active_children)
            parent_before = len(received_parent_turn_ids)
            observe_native(notification)
            params = notification.get("params") or {}
            if (
                active_before != len(active_children)
                or parent_before != len(received_parent_turn_ids)
                or (notification.get("method") == "turn/started" and params.get("threadId") == provider_id)
            ):
                child_continuation_changes.set()

        child_notification_handler = observe
        owner["codex_child_continuation_stop"] = child_continuation_stop
        add_handler(observe)

    async def retain_ordinary_child_continuation(notification: dict[str, Any]) -> bool:
        nonlocal turn_completed, waiting_for_child_continuation, waiting_parent_turn_id
        params = notification.get("params") or {}
        value = params.get("turn") or {}
        completed_id = str(params.get("turnId") or value.get("id") or "")
        pending_children = child_completion_pending.pop(completed_id, False)
        if (
            child_notification_handler is None or terminal_status != "completed"
            or delivery_unknown or goal_steer_recovery_fenced
        ):
            return False
        # Goal activation and its provider-tool projection must win before
        # deciding whether this remains an ordinary, non-goal supervisor.
        await manager.wait_for_notification_handler(project_codex_notification, provider_id)
        async with RUNS.active_lock:
            active = RUNS.active.get(session_id)
            live_goal = (STORE.sessions.get(session_id) or {}).get("codex_goal")
            if not (
                active and active.get("run_id") == current_run_id
                and active.get("codex_app_server_turn") is turn
                and active.get("provider_thread_id") == provider_id
                and (RUNS.current_turns.get(session_id) or {}).get("run_id") == current_run_id
                and session_id in RUNS.busy
                and not active.get("stop_requested")
                and current_run_id not in RUNS.stopped_runs
                and getattr(manager, "generation", None) == child_continuation_generation
                and not (isinstance(live_goal, dict) and live_goal.get("status") == "active")
                and (pending_children or received_parent_turn_ids - completed_native_turn_ids)
            ):
                return False
            waiting_for_child_continuation = bool(getattr(turn, "_completed", True))
            waiting_parent_turn_id = completed_id
            active["codex_child_continuation_waiting"] = waiting_for_child_continuation
            if waiting_for_child_continuation:
                active["provider_turn_ready"] = False
                active["provider_turn_id"] = None
            # Keep the original logical run, provider authority, thread pin,
            # Stop owner, and subscription. A child terminal notification does
            # not finish this wait: only Codex's next parent turn can do that.
            turn_completed = False
            child_continuation_changes.set()
        await flush_pending_unknown(final=True)
        return True

    async def continue_after_owned_children() -> None:
        """Consume native pending child notifications once, with no user text."""
        nonlocal waiting_for_child_continuation, last_activity
        nonlocal child_acceptance_parent_id
        nonlocal provisional_thread_invalidated, delivery_unknown
        if (
            not waiting_for_child_continuation or active_child_ids
            or not waiting_parent_turn_id
            or waiting_parent_turn_id in child_continuation_attempts
        ):
            return

        def before_send() -> bool:
            active = RUNS.active.get(session_id)
            live_session = STORE.sessions.get(session_id)
            live_goal = (live_session or {}).get("codex_goal")
            return bool(
                active and active.get("run_id") == current_run_id
                and active.get("codex_app_server_turn") is turn
                and active.get("provider_thread_id") == provider_id
                and (RUNS.current_turns.get(session_id) or {}).get("run_id") == current_run_id
                and session_id in RUNS.busy
                and live_session is not None and not live_session.get("archived")
                and session_id not in DELETING_SESSIONS
                and session_id not in DELETED_SESSION_TOMBSTONES
                and not active.get("stop_requested")
                and current_run_id not in RUNS.stopped_runs
                and getattr(manager, "generation", None) == child_continuation_generation
                and active.get("codex_child_continuation_waiting") is True
                and turn.turn_id == waiting_parent_turn_id
                and not active_child_ids
                and not (isinstance(live_goal, dict) and live_goal.get("status") == "active")
            )

        if not before_send():
            return
        # Mark before awaiting the guarded write. An ambiguous transport result
        # must never cause a second empty native turn or replay the user prompt.
        attempted_parent_id = waiting_parent_turn_id
        child_continuation_attempts.add(attempted_parent_id)
        child_acceptance_parent_id = attempted_parent_id
        try:
            continued_turn_id = await turn.continue_after_subagents(
                before_send=before_send,
                client_user_message_id=current_run_id,
                responsesapi_client_metadata={
                    "hyadex_run_id": current_run_id,
                    "hyadex_run_proof": codex_provider_mcp_run_proof(
                        session_id, provider_id, current_run_id,
                    ),
                },
            )
        except asyncio.CancelledError as exc:
            if getattr(exc, "request_sent", None) is False:
                child_acceptance_parent_id = ""
            # The shielded finalizer retains the stream through this same
            # bounded acceptance window. Never replay a possibly sent request.
            raise
        except CodexAppServerError as exc:
            if not getattr(exc, "request_sent", False) or isinstance(exc, CodexAppServerRequestError):
                child_acceptance_parent_id = ""
                raise
            accepted = await reconcile_child_continuation_acceptance()
            if not accepted:
                if child_continuation_owner_current():
                    delivery_unknown = True
                    provisional_thread_invalidated = True
                    await manager.retire_generation(child_continuation_generation)
                child_acceptance_parent_id = ""
                raise
            child_acceptance_parent_id = ""
            continued_turn_id = turn.turn_id
        child_acceptance_parent_id = ""
        if continued_turn_id is None:
            # A new child, Stop, or spontaneous parent turn won before any
            # bytes were sent. Only that proven non-send may retry on a later
            # child drain; ambiguous delivery keeps the one-shot fence.
            child_continuation_attempts.discard(attempted_parent_id)
            return
        if continued_turn_id:
            waiting_for_child_continuation = False
            last_activity = time.monotonic()
            await bind_active_turn_and_reconcile_stop()

    def child_continuation_owner_current() -> bool:
        active = RUNS.active.get(session_id)
        return bool(
            active and active.get("run_id") == current_run_id
            and active.get("codex_app_server_turn") is turn
            and active.get("provider_thread_id") == provider_id
            and (RUNS.current_turns.get(session_id) or {}).get("run_id") == current_run_id
            and session_id in RUNS.busy
            and getattr(manager, "generation", None) == child_continuation_generation
            and getattr(turn, "transport_generation", None) == child_continuation_generation
        )

    async def reconcile_child_continuation_acceptance() -> bool:
        """Bound a lost empty-input acknowledgement without replay or polling."""
        def new_native_turn() -> bool:
            return bool(
                child_continuation_owner_current()
                and turn.turn_id and turn.turn_id != child_acceptance_parent_id
            )

        if new_native_turn():
            return True
        if not child_continuation_owner_current():
            return False
        deadline = time.monotonic() + max(0.01, float(CODEX_APP_SERVER_AMBIGUOUS_ACCEPT_SECONDS))
        # Empty-input turns contain no clientUserMessageId/userMessage in
        # native list_turns. Never guess the newest turn from that endpoint.
        # Only this retained, exact-generation native handle proves acceptance.
        while child_continuation_owner_current():
            if new_native_turn():
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            child_continuation_changes.clear()
            try:
                await asyncio.wait_for(child_continuation_changes.wait(), timeout=remaining)
            except asyncio.TimeoutError:
                break
        return new_native_turn()

    async def detach_native_steer_admission() -> None:
        """Stop Force Send admission into this runner before queue cleanup."""

        async with RUNS.active_lock:
            active = RUNS.active.get(session_id)
            if (
                active
                and str(active.get("run_id") or "") == current_run_id
                and (
                    active.get("native_steer_queue") is steer_queue
                    or active.get("codex_goal_steer_queue") is steer_queue
                )
            ):
                active["provider_turn_ready"] = False
                active["native_steer_queue"] = None
                active["codex_goal_steer_queue"] = None

    async def reconcile_cancelled_runner_exit() -> None:
        """Release this run and its exact provider-thread pin after cancellation."""

        nonlocal thread_pinned
        try:
            # This identity-fenced recovery also publishes the missing terminal
            # event and promotes one queued successor. The outer task supervisor
            # may invoke it again; its second call is deliberately a no-op.
            await reconcile_provider_task_exit(
                session_id,
                current_run_id,
                BACKEND_CODEX,
            )
        finally:
            try:
                if thread_pinned and provider_id:
                    if provisional_thread_invalidated:
                        await unpin_codex_app_server_thread(
                            manager,
                            provider_id,
                            invalidate_loaded_thread=True,
                        )
                    else:
                        await unpin_codex_app_server_thread(manager, provider_id)
            finally:
                thread_pinned = False

    text_parts: list[str] = []
    seen_text: set[str] = set()
    seen_reasoning: set[str] = set()
    emitted_final_item_ids: set[str] = set()
    emitted_reasoning_item_ids: set[str | tuple[str, str]] = set()
    reasoning_stream_run_ids = {current_run_id}
    assistant_deltas: dict[str, list[str]] = {}
    reasoning_summary_deltas: dict[str, list[str]] = {}
    plan_deltas: dict[str, list[str]] = {}
    tool_output_deltas: dict[str, deque[str]] = {}
    tool_output_lengths: dict[str, int] = {}
    tool_output_truncated: set[str] = set()
    tool_calls: dict[str, dict[str, Any]] = {}
    started_tools: set[str] = set()
    finished_tools: set[str] = set()
    changed_paths: set[str] = set()
    seen_artifacts: set[str] = set()
    pending_unknown_message = ""
    pending_unknown_item_id = ""

    def append_tool_output_delta(item_id: str, value: Any) -> None:
        delta = str(value or "")
        if not delta:
            return
        chunks = tool_output_deltas.setdefault(item_id, deque())
        chunks.append(delta)
        total = tool_output_lengths.get(item_id, 0) + len(delta)
        while total > CODEX_APP_SERVER_TOOL_OUTPUT_MAX_CHARS and chunks:
            excess = total - CODEX_APP_SERVER_TOOL_OUTPUT_MAX_CHARS
            first = chunks[0]
            if len(first) <= excess:
                total -= len(chunks.popleft())
            else:
                chunks[0] = first[excess:]
                total -= excess
            tool_output_truncated.add(item_id)
        tool_output_lengths[item_id] = total

    def consume_tool_output_delta(item_id: str) -> str:
        chunks = tool_output_deltas.pop(item_id, deque())
        tool_output_lengths.pop(item_id, None)
        text = "".join(chunks)
        if item_id in tool_output_truncated:
            tool_output_truncated.discard(item_id)
            return "[Earlier tool output truncated by HyadexServer]\n" + text
        return text

    def current_metadata() -> dict[str, Any]:
        return {
            **run_event_metadata(current_run_id),
            **({"provider_initial_turn_id": initial_provider_turn_id,
                "provider_thread_id": provider_id} if initial_provider_turn_id else {}),
        }

    async def emit_final_text(value: Any, *, item_id: str = "") -> None:
        text = clean_assistant_text(str(value or ""))
        if not text:
            return
        if (
            item_id
            and item_id in emitted_final_item_ids
        ) or text in seen_text:
            return
        seen_text.add(text)
        text_parts.append(text)
        await append_event(session_id, "assistant_text", {
            "run_id": current_run_id,
            "text": text,
            **({"item_id": item_id} if item_id else {}),
            **current_metadata(),
        })
        if item_id:
            emitted_final_item_ids.add(item_id)

    async def emit_reasoning(
        value: Any,
        *,
        phase: str = "",
        item_id: str = "",
        reasoning_after_seq: int | None = None,
    ) -> None:
        text = str(value or "").strip()
        if not text:
            return
        reasoning_identity = ("reasoning", item_id) if phase == "reasoning" else item_id
        if item_id:
            if reasoning_identity in emitted_reasoning_item_ids:
                return
        elif text in seen_reasoning:
            return
        seen_reasoning.add(text)
        payload = {
            "run_id": current_run_id,
            "text": text,
            **current_metadata(),
        }
        if phase:
            payload["phase"] = phase
        if item_id:
            payload["item_id"] = item_id
        if reasoning_after_seq is not None:
            payload["reasoning_after_seq"] = reasoning_after_seq
        if phase in {"summary", "reasoning"} and item_id:
            await persist_reasoning_summary(session_id, payload, emitted_reasoning_item_ids)
        else:
            await append_event(session_id, "reasoning_summary", payload)
            if item_id:
                emitted_reasoning_item_ids.add(reasoning_identity)

    async def flush_pending_unknown(*, final: bool) -> None:
        nonlocal pending_unknown_message, pending_unknown_item_id
        text = pending_unknown_message
        item_id = pending_unknown_item_id
        pending_unknown_message = ""
        pending_unknown_item_id = ""
        if not text:
            return
        if final:
            await emit_final_text(text, item_id=item_id)
        else:
            await emit_reasoning(
                text,
                phase="commentary",
                item_id=item_id,
            )

    async def emit_completed_reasoning(item: dict[str, Any], item_id: str) -> None:
        # One native completion owns both channels. Finish their authoritative
        # writes even if cancellation arrives between the two app events.
        async def commit() -> None:
            summary_stream = reasoning_summary_stream_item(session_id, current_run_id, item_id)
            plaintext_stream = reasoning_summary_stream_item(session_id, current_run_id, item_id, "reasoning")
            buffered = reasoning_summary_deltas.pop(item_id, [])
            summary = codex_app_server_reasoning_summary(item) or summary_stream.get("text") or "".join(buffered)
            plaintext = codex_app_server_reasoning_plaintext(item) or plaintext_stream.get("text")
            for phase, text, streamed in (("summary", summary, summary_stream), ("reasoning", plaintext, plaintext_stream)):
                await emit_reasoning(text, phase=phase, item_id=item_id,
                    reasoning_after_seq=streamed.get("after_seq"))
                emitted_reasoning_item_ids.add(("reasoning", item_id) if phase == "reasoning" else item_id)
                await clear_reasoning_summary_stream(session_id, current_run_id, item_id, phase)
        task = asyncio.create_task(commit())
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            await join_task_despite_caller_cancellation(task)
            raise

    async def emit_tool_started(tool: dict[str, Any]) -> None:
        tool_id = str(tool.get("id") or "")
        if not tool_id or tool_id in started_tools:
            return
        await flush_pending_unknown(final=False)
        started_tools.add(tool_id)
        tool_calls[tool_id] = tool
        await append_event(session_id, "tool_started", {
            "run_id": current_run_id,
            "tool": tool,
            **current_metadata(),
        })

    async def emit_tool_finished(item: dict[str, Any]) -> None:
        tool = codex_app_server_tool(item)
        if not tool:
            return
        tool_id = str(tool["id"])
        if tool_id in finished_tools:
            return
        await emit_tool_started(tool)
        finished_tools.add(tool_id)
        output, exit_code, is_error = codex_app_server_tool_output(
            item,
            consume_tool_output_delta(tool_id),
        )
        await append_event(session_id, "tool_finished", {
            "run_id": current_run_id,
            "tool_id": tool_id,
            "tool": tool_calls.get(tool_id) or tool,
            "output": output,
            "exit_code": exit_code,
            "is_error": is_error,
            **current_metadata(),
        })

    async def stop_manifest_watcher() -> None:
        nonlocal manifest_watch_task
        task = manifest_watch_task
        manifest_watch_task = None
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    async def enforce_goal_time_budget() -> None:
        def native_time_used(goal_value: dict[str, Any]) -> float:
            value = goal_value.get("timeUsedSeconds")
            return (
                float(value)
                if isinstance(value, (int, float)) and not isinstance(value, bool)
                else 0.0
            )

        latest = STORE.sessions.get(session_id) or sess
        goal = latest.get("codex_goal")
        if not isinstance(goal, dict):
            return
        goal_identity = str(goal.get("id") or goal.get("objective") or "")
        base_used = native_time_used(goal)
        base_started_at = time.monotonic()
        paused = False
        while True:
            latest = STORE.sessions.get(session_id) or {}
            goal = latest.get("codex_goal")
            limit = latest.get("codex_goal_time_budget_seconds")
            if (
                not isinstance(goal, dict)
                or isinstance(limit, bool)
                or not isinstance(limit, (int, float))
                or limit <= 0
            ):
                return
            status = str(goal.get("status") or "")
            if status == "paused":
                paused = True
                await asyncio.sleep(0.5)
                continue
            if status != "active":
                return
            current_identity = str(
                goal.get("id") or goal.get("objective") or ""
            )
            reported_used = native_time_used(goal)
            if paused or current_identity != goal_identity:
                goal_identity = current_identity
                base_used = reported_used
                base_started_at = time.monotonic()
                paused = False
            projected_used = max(
                reported_used,
                base_used + max(0.0, time.monotonic() - base_started_at),
            )
            remaining = float(limit) - projected_used
            if remaining <= 0:
                # Goal controls use the same lifecycle lock. Revalidate after
                # acquiring it so a concurrent clear, pause, objective edit,
                # or raised limit can never be overwritten by this watcher.
                async with session_lifecycle_lock(session_id):
                    current = STORE.sessions.get(session_id) or {}
                    current_goal = current.get("codex_goal")
                    current_limit = current.get(
                        "codex_goal_time_budget_seconds"
                    )
                    if (
                        not isinstance(current_goal, dict)
                        or isinstance(current_limit, bool)
                        or not isinstance(current_limit, (int, float))
                        or current_limit <= 0
                        or str(current_goal.get("status") or "") != "active"
                        or current.get("codex_goal_time_budget_exhausted")
                        is True
                    ):
                        return
                    current_identity = str(
                        current_goal.get("id")
                        or current_goal.get("objective")
                        or ""
                    )
                    if (
                        current_identity != goal_identity
                        or float(current_limit) != float(limit)
                    ):
                        goal_identity = current_identity
                        base_used = native_time_used(current_goal)
                        base_started_at = time.monotonic()
                        paused = False
                        continue
                    await apply_codex_goal_time_budget_limit(
                        session_id,
                        current_run_id,
                        manager,
                        provider_id,
                        turn,
                        current_limit,
                    )
                    return
            # Re-evaluate at least twice per second so edits to the time limit,
            # goal pause/resume, and native elapsed-time updates take effect.
            await asyncio.sleep(min(0.5, max(0.01, remaining)))

    async def finish_logical_outputs(
        logical_run_id: str,
        baseline: dict[str, Any],
        paths: set[str],
        artifacts: set[str],
        logical_manifest_path: Path,
    ) -> None:
        await collect_manifest(
            session_id,
            logical_run_id,
            logical_manifest_path,
            seen_artifacts=artifacts,
            final=True,
        )
        await publish_turn_code_diff(
            session_id,
            logical_run_id,
            BACKEND_CODEX,
            cwd,
            baseline,
            paths,
        )

    def clear_logical_buffers() -> None:
        nonlocal pending_unknown_message, pending_unknown_item_id
        nonlocal terminal_error, terminal_status, error_emitted
        # Native turn/steer keeps one provider turn. Clear only unfinished
        # delta state at the logical UI boundary; turn-wide completed item IDs
        # deliberately survive so a repeated notification cannot duplicate a
        # pre-steer reasoning/commentary item under the new logical run.
        text_parts.clear()
        seen_text.clear()
        seen_reasoning.clear()
        assistant_deltas.clear()
        reasoning_summary_deltas.clear()
        plan_deltas.clear()
        tool_output_deltas.clear()
        tool_output_lengths.clear()
        tool_output_truncated.clear()
        tool_calls.clear()
        started_tools.clear()
        finished_tools.clear()
        changed_paths.clear()
        seen_artifacts.clear()
        pending_unknown_message = ""
        pending_unknown_item_id = ""
        terminal_error = None
        terminal_status = "failed"
        error_emitted = False

    async def next_sequenced_notification() -> tuple[int, dict[str, Any]]:
        nonlocal synthetic_notification_sequence
        if turn is None:
            raise CodexAppServerError("Codex app-server turn is unavailable")
        sequenced_reader = getattr(
            turn,
            "next_notification_with_sequence",
            None,
        )
        if callable(sequenced_reader):
            sequence, notification = await sequenced_reader()
            return int(sequence), dict(notification)
        # Compatibility for tests or third-party manager wrappers created
        # before receive-order watermarks were added.
        notification = await turn.next_notification()
        synthetic_notification_sequence += 1
        return synthetic_notification_sequence, dict(notification)

    async def drain_notifications_through(
        watermark: int, *, handoff_at_completion: bool = False,
    ) -> None:
        """Persist exactly the provider notifications preceding steer ack."""
        nonlocal notification_task, handled_notification_sequence
        nonlocal last_activity
        while handled_notification_sequence < watermark:
            if notification_task is None:
                notification_task = asyncio.create_task(
                    next_sequenced_notification()
                )
            sequence, notification = await notification_task
            notification_task = None
            if sequence <= handled_notification_sequence:
                continue
            if sequence > watermark:
                raise CodexAppServerProtocolError(
                    "turn/steer notification watermark skipped routed output",
                    request_sent=True,
                    safe_to_retry=False,
                )
            handled_notification_sequence = sequence
            last_activity = time.monotonic()
            await handle_notification(notification)
            if handoff_at_completion and turn_completed:
                # The subscription is thread-wide. A native continuation may
                # already follow this completion before the steer ACK. Leave
                # those packets to the goal consumer, which owns their turn
                # identity/readiness and commits the pending ACK after them.
                break

    async def switch_logical_run(
        selected: dict[str, Any],
        remaining: int,
    ) -> dict[str, Any]:
        nonlocal current_run_id, current_provider_prompt
        nonlocal current_diff_baseline, manifest_watch_task, last_activity
        nonlocal delivery_unknown, initial_provider_turn_id
        if turn is None or turn_completed:
            raise NativeSteerHandoffError(
                "the active Codex turn has already completed",
                safe_to_requeue=True,
            )

        candidate_run_id = f"run_{uuid.uuid4().hex[:16]}"
        candidate_authority_path: Path | None = None
        steer_rpc_attempted = False
        try:
            request_prompt = build_user_provider_prompt(
                session_id,
                str(selected.get("prompt") or ""),
                list(selected.get("file_ids") or []),
            )
            candidate_diff_baseline = await capture_git_baseline(
                session_id,
                candidate_run_id,
                cwd,
            )
        except Exception as exc:
            raise NativeSteerHandoffError(
                concise_error_message(exc),
                safe_to_requeue=True,
            ) from exc

        transition_ready = asyncio.Event()
        transition_nonce = secrets.token_hex(16)
        transition_reserved = False

        async def release_transition_boundary() -> None:
            nonlocal transition_reserved
            if not transition_reserved:
                return
            transition_reserved = False
            transition_ready.set()
            async with RUNS.active_lock:
                active = RUNS.active.get(session_id)
                if (
                    active
                    and active.get("logical_transition_ready") is transition_ready
                ):
                    active.pop("logical_transition_ready", None)
                    active.pop("logical_transition_candidate_run_id", None)
                    active.pop("logical_transition_authority_open", None)
                    active.pop("logical_transition_predecessor_run_id", None)
                    active.pop("logical_transition_authority_nonce", None)

        async with RUNS.active_lock:
            active = RUNS.active.get(session_id)
            if not active or active.get("stop_requested"):
                raise NativeSteerHandoffError(
                    "the active Codex turn is already stopping",
                    safe_to_requeue=True,
                )
            active["logical_transition_ready"] = transition_ready
            active["logical_transition_candidate_run_id"] = candidate_run_id
            active["logical_transition_predecessor_run_id"] = current_run_id
            active["logical_transition_authority_nonce"] = transition_nonce
            transition_reserved = True

        try:
            try:
                await fence_native_steer_delivery(
                    session_id,
                    selected,
                    backend=BACKEND_CODEX,
                )
            except BaseException as exc:
                await release_transition_boundary()
                if isinstance(exc, asyncio.CancelledError):
                    raise
                raise NativeSteerHandoffError(
                    (
                        "Codex steering was not sent because its durable "
                        f"delivery fence failed: {concise_error_message(exc)}"
                    ),
                    safe_to_requeue=True,
                ) from exc
            async with RUNS.active_lock:
                active = RUNS.active.get(session_id)
                delivery_still_allowed = bool(
                    active
                    and not active.get("stop_requested")
                    and active.get("logical_transition_ready")
                    is transition_ready
                    and str(
                        active.get("logical_transition_candidate_run_id") or ""
                    ) == candidate_run_id
                )
            if not delivery_still_allowed:
                await release_transition_boundary()
                raise NativeSteerHandoffError(
                    "the active Codex turn stopped before steering delivery",
                    safe_to_requeue=True,
                )
            try:
                request_prompt, candidate_authority_path, _candidate_runtime_env = (
                    await issue_native_steer_provider_authority(
                        session_id,
                        candidate_run_id,
                        selected,
                        request_prompt,
                        transition_nonce,
                        backend=BACKEND_CODEX,
                    )
                )
            except BaseException as exc:
                await release_transition_boundary()
                if isinstance(exc, asyncio.CancelledError):
                    raise
                if isinstance(exc, NativeSteerHandoffError):
                    raise
                raise NativeSteerHandoffError(
                    (
                        "Codex steering was not sent because replacement "
                        f"authority issuance failed: {concise_error_message(exc)}"
                    ),
                    safe_to_requeue=True,
                ) from exc
            async with RUNS.active_lock:
                active = RUNS.active.get(session_id)
                delivery_still_allowed = bool(
                    active
                    and not active.get("stop_requested")
                    and active.get("logical_transition_ready")
                    is transition_ready
                    and str(
                        active.get("logical_transition_candidate_run_id") or ""
                    ) == candidate_run_id
                )
                if delivery_still_allowed:
                    active["logical_transition_authority_open"] = True
            if not delivery_still_allowed:
                await revoke_provider_authority(candidate_run_id)
                candidate_authority_path = None
                await release_transition_boundary()
                raise NativeSteerHandoffError(
                    "the active Codex turn stopped before steering delivery",
                    safe_to_requeue=True,
                )
            steer_with_watermark = getattr(
                turn,
                "steer_with_notification_watermark",
                None,
            )
            steer_rpc_attempted = True
            if callable(steer_with_watermark):
                _turn_id, notification_watermark = await steer_with_watermark(
                    [{
                        "type": "text",
                        "text": request_prompt,
                        "text_elements": [],
                    }],
                    client_user_message_id=candidate_run_id,
                )
            else:
                _turn_id = await turn.steer(
                    [{
                        "type": "text",
                        "text": request_prompt,
                        "text_elements": [],
                    }],
                    client_user_message_id=candidate_run_id,
                )
                notification_watermark = handled_notification_sequence
        except asyncio.CancelledError:
            # Once the durable delivery fence exists, cancellation at the RPC
            # boundary cannot prove that Codex did not accept the steer. Retire
            # projection of this turn and let the command waiter receive the
            # conservative non-replay outcome below.
            delivery_unknown = steer_rpc_attempted
            with suppress(Exception):
                await turn.interrupt()
            if candidate_authority_path is not None:
                await revoke_provider_authority(candidate_run_id)
                candidate_authority_path = None
            if steer_rpc_attempted:
                await revoke_provider_authority(current_run_id)
            await release_transition_boundary()
            raise
        except NativeSteerHandoffError:
            if candidate_authority_path is not None:
                await revoke_provider_authority(candidate_run_id)
                candidate_authority_path = None
            await release_transition_boundary()
            raise
        except CodexAppServerRequestError as exc:
            if candidate_authority_path is not None:
                await revoke_provider_authority(candidate_run_id)
                candidate_authority_path = None
            await release_transition_boundary()
            raise NativeSteerHandoffError(
                concise_error_message(exc),
                safe_to_requeue=True,
            ) from exc
        except Exception as exc:
            # Delivery may have succeeded. Never put this message back in the
            # queue, and interrupt the provider turn so uncertain output cannot
            # be attributed to the preceding logical request.
            delivery_unknown = True
            with suppress(Exception):
                await turn.interrupt()
            if candidate_authority_path is not None:
                await revoke_provider_authority(candidate_run_id)
                candidate_authority_path = None
            await revoke_provider_authority(current_run_id)
            with suppress(Exception):
                await append_event(session_id, "error", {
                    "run_id": current_run_id,
                    "backend": BACKEND_CODEX,
                    "transport": CODEX_TRANSPORT_APP_SERVER,
                    "message": (
                        "Force Send delivery could not be confirmed. The message "
                        "was not replayed; retry it manually if it does not appear."
                    ),
                    "delivery_unknown": True,
                })
            await release_transition_boundary()
            raise NativeSteerHandoffError(
                concise_error_message(exc),
                safe_to_requeue=False,
                delivery_uncertain=True,
            ) from exc

        try:
            await drain_notifications_through(int(notification_watermark))
        except asyncio.CancelledError:
            delivery_unknown = True
            with suppress(Exception):
                await turn.interrupt()
            if candidate_authority_path is not None:
                await revoke_provider_authority(candidate_run_id)
                candidate_authority_path = None
            await revoke_provider_authority(current_run_id)
            await release_transition_boundary()
            raise
        except Exception as exc:
            delivery_unknown = True
            with suppress(Exception):
                await turn.interrupt()
            if candidate_authority_path is not None:
                await revoke_provider_authority(candidate_run_id)
                candidate_authority_path = None
            await revoke_provider_authority(current_run_id)
            with suppress(Exception):
                await append_event(session_id, "error", {
                    "run_id": current_run_id,
                    "backend": BACKEND_CODEX,
                    "transport": CODEX_TRANSPORT_APP_SERVER,
                    "message": (
                        "Force Send was accepted, but the provider output boundary "
                        "could not be reconciled. The message was not replayed."
                    ),
                    "delivery_unknown": True,
                })
            await release_transition_boundary()
            raise NativeSteerHandoffError(
                concise_error_message(exc),
                safe_to_requeue=False,
                delivery_uncertain=True,
            ) from exc

        if turn_completed:
            delivery_unknown = True
            if candidate_authority_path is not None:
                await revoke_provider_authority(candidate_run_id)
                candidate_authority_path = None
            await revoke_provider_authority(current_run_id)
            with suppress(Exception):
                await append_event(session_id, "error", {
                    "run_id": current_run_id,
                    "backend": BACKEND_CODEX,
                    "transport": CODEX_TRANSPORT_APP_SERVER,
                    "message": (
                        "Force Send was acknowledged as the provider turn "
                        "completed. Delivery is uncertain, so it was not replayed."
                    ),
                    "delivery_unknown": True,
                })
            await release_transition_boundary()
            raise NativeSteerHandoffError(
                "the provider turn completed at the Force Send boundary",
                safe_to_requeue=False,
                delivery_uncertain=True,
            )

        previous_run_id = current_run_id

        async def retire_accepted_transition() -> None:
            """Atomically retire both authorities before opening the fence."""

            nonlocal candidate_authority_path, delivery_unknown
            delivery_unknown = True

            async def cleanup() -> None:
                nonlocal candidate_authority_path
                await revoke_provider_authority(candidate_run_id)
                await revoke_provider_authority(previous_run_id)
                candidate_authority_path = None
                with suppress(Exception):
                    await turn.interrupt()
                await release_transition_boundary()

            completion = asyncio.create_task(cleanup())
            try:
                await asyncio.shield(completion)
            except asyncio.CancelledError:
                await join_task_despite_caller_cancellation(completion)

        try:
            previous_metadata = current_metadata()
            with suppress(Exception):
                await flush_pending_unknown(final=False)
            previous_watcher = manifest_watch_task
            manifest_watch_task = None
            if previous_watcher is not None:
                previous_watcher.cancel()
                await asyncio.gather(previous_watcher, return_exceptions=True)
            previous_manifest_path = manifest_path.with_name(
                f".{previous_run_id}.json"
            )
            with suppress(OSError):
                previous_manifest_path.unlink()
            with suppress(OSError):
                manifest_path.replace(previous_manifest_path)
            previous_diff_baseline = current_diff_baseline
            previous_changed_paths = set(changed_paths)
            previous_seen_artifacts = set(seen_artifacts)

            metadata = queued_turn_run_metadata(selected)
            metadata["steer_interrupted_run_id"] = previous_run_id
            display_prompt = str(
                selected.get("display_prompt")
                if selected.get("display_prompt") is not None
                else selected.get("prompt") or ""
            )
            display_file_ids = list(
                selected.get("display_file_ids")
                if selected.get("display_file_ids") is not None
                else selected.get("file_ids") or []
            )
            lineage = turn_steering_lineage(RUNS.current_turns.get(session_id))
            steering_message = {
                "prompt": str(selected.get("prompt") or ""),
                "file_ids": list(selected.get("file_ids") or []),
            }
            if not lineage or steering_message != lineage[-1]:
                lineage.append(steering_message)

            async with RUNS.active_lock:
                active = RUNS.active.get(session_id)
                if not active:
                    raise RuntimeError(
                        "the active turn disappeared after Force Send was accepted"
                    )
                current = RUNS.current_turns.get(session_id)
                if current is None:
                    raise RuntimeError(
                        "the current turn disappeared after Force Send was accepted"
                    )
                stopped_during_handoff = bool(active.get("stop_requested"))
                current_run_id = candidate_run_id
                # Force Send accepts a new logical input on the current
                # native turn; it must not inherit the predecessor's input ID.
                initial_provider_turn_id = str(_turn_id or "")
                reasoning_stream_run_ids.add(candidate_run_id)
                current_provider_prompt = request_prompt
                current_diff_baseline = candidate_diff_baseline
                clear_logical_buffers()
                RUNS.run_metadata[candidate_run_id] = metadata
                active["run_id"] = candidate_run_id
                active["started_at"] = time.time()
                active["started_at_iso"] = now_iso()
                if stopped_during_handoff:
                    RUNS.stopped_runs.add(candidate_run_id)
                current.update({
                    "run_id": candidate_run_id,
                    "prompt": str(selected.get("prompt") or ""),
                    "display_prompt": display_prompt,
                    "file_ids": list(selected.get("file_ids") or []),
                    "queued_id": selected.get("queued_id"),
                    "skill_selection": selected.get("skill_selection"),
                    "steering_lineage": lineage,
                })
            last_activity = time.monotonic()

            with suppress(OSError):
                manifest_path.unlink()
            manifest_watch_task = asyncio.create_task(
                watch_manifest_artifacts(
                    session_id,
                    candidate_run_id,
                    manifest_path,
                    seen_artifacts,
                )
            )
            event_specs = [
                (
                    "turn_stopped",
                    {
                        "run_id": previous_run_id,
                        "backend": BACKEND_CODEX,
                        "transport": CODEX_TRANSPORT_APP_SERVER,
                        "native_steer": True,
                        "superseded_by_run_id": candidate_run_id,
                        **previous_metadata,
                    },
                ),
                (
                    "turn_queue_run_now",
                    {
                        "queued_id": selected.get("queued_id"),
                        "backend": BACKEND_CODEX,
                        "prompt": display_prompt,
                        "request_prompt": str(selected.get("prompt") or ""),
                        "display_prompt": display_prompt,
                        "file_ids": list(selected.get("file_ids") or []),
                        "display_file_ids": display_file_ids,
                        "interrupted_run_id": previous_run_id,
                        "replays_interrupted_message": False,
                        "native_steer": True,
                        "message": "Steering message sent to the active Codex turn.",
                        "remaining": remaining,
                        "superseded_queued_ids": [],
                    },
                ),
                (
                    "turn_started",
                    {
                        "run_id": candidate_run_id,
                        "backend": BACKEND_CODEX,
                        "prompt": display_prompt,
                        "file_ids": display_file_ids,
                        "queued_id": selected.get("queued_id"),
                        "native_steer": True,
                        **metadata,
                    },
                ),
            ]
            await append_durable_event_batch(session_id, event_specs)
            await finish_reasoning_summary_stream(
                session_id, previous_run_id, emitted_reasoning_item_ids)
            await revoke_provider_authority(previous_run_id)
        except BaseException as exc:
            await retire_accepted_transition()
            if isinstance(exc, asyncio.CancelledError):
                raise
            logger.exception(
                "could not finish native steer bookkeeping session=%s run=%s",
                session_id,
                candidate_run_id,
            )
            with suppress(Exception):
                await append_event(session_id, "error", {
                    "run_id": candidate_run_id,
                    "backend": BACKEND_CODEX,
                    "transport": CODEX_TRANSPORT_APP_SERVER,
                    "message": (
                        "Force Send was accepted, but its durable lifecycle "
                        "could not be committed. The message was not replayed."
                    ),
                    "delivery_unknown": True,
                })
            raise NativeSteerHandoffError(
                concise_error_message(exc),
                safe_to_requeue=False,
                delivery_uncertain=True,
            ) from exc
        await release_transition_boundary()

        RUNS.run_metadata.pop(previous_run_id, None)
        RUNS.stopped_runs.discard(previous_run_id)
        try:
            await finish_logical_outputs(
                previous_run_id,
                previous_diff_baseline,
                previous_changed_paths,
                previous_seen_artifacts,
                previous_manifest_path,
            )
        except Exception:
            logger.exception(
                "could not finalize superseded Codex run session=%s run=%s",
                session_id,
                previous_run_id,
            )
        # Bookkeeping and artifact finalization can be slow on remote disks.
        # The steered request gets its full idle window after the handoff is
        # actually ready to resume provider notification processing.
        last_activity = time.monotonic()
        return {
            "ok": True,
            "queued_id": selected.get("queued_id"),
            "run_id": candidate_run_id,
            "interrupted": True,
            "native_steer": True,
            "replays_interrupted_message": False,
            "superseded_queued_ids": [],
        }

    async def bind_active_turn_and_reconcile_stop() -> None:
        nonlocal initial_provider_turn_id
        if turn is None or not turn.turn_id:
            return
        should_interrupt = False
        async with RUNS.active_lock:
            active = RUNS.active.get(session_id)
            active_owned = bool(
                active
                and str(active.get("run_id") or "") == current_run_id
                and active.get("codex_app_server_turn") is turn
                and active.get("provider_thread_id") == provider_id
                and session_id in RUNS.busy
                and str(
                    (RUNS.current_turns.get(session_id) or {}).get("run_id") or ""
                ) == current_run_id
                and (
                    not child_continuation_generation
                    or getattr(manager, "generation", None) == child_continuation_generation
                )
            )
            if active_owned:
                if not initial_provider_turn_id:
                    initial_provider_turn_id = str(getattr(turn, "initial_turn_id", "") or turn.turn_id)
                active["provider_turn_ready"] = True
                active["provider_turn_id"] = turn.turn_id
                if (
                    active.get("stop_requested")
                    and not active.get("native_interrupt_sent")
                ):
                    active["native_interrupt_sent"] = True
                    should_interrupt = True
        if should_interrupt:
            try:
                await turn.interrupt()
            except Exception:
                async with RUNS.active_lock:
                    active = RUNS.active.get(session_id)
                    if (
                        active
                        and str(active.get("run_id") or "") == current_run_id
                        and active.get("provider_turn_id") == turn.turn_id
                    ):
                        active["native_interrupt_sent"] = False
                logger.warning(
                    "Codex app-server late interrupt failed session=%s turn=%s",
                    session_id,
                    turn.turn_id,
                )

    def stop_requested_for_current_run() -> bool:
        if current_run_id in RUNS.stopped_runs:
            return True
        active = RUNS.active.get(session_id)
        return bool(
            active
            and str(active.get("run_id") or "") == current_run_id
            and active.get("stop_requested")
        )

    async def handle_notification(notification: dict[str, Any]) -> bool:
        nonlocal terminal_status, terminal_error, turn_completed
        nonlocal pending_unknown_message, pending_unknown_item_id
        nonlocal ambiguous_turn_start, error_emitted
        nonlocal native_answer_turn_id, waiting_for_child_continuation
        nonlocal compaction_failed, compaction_execution_seen
        method = str(notification.get("method") or "")
        params = (
            notification.get("params")
            if isinstance(notification.get("params"), dict)
            else {}
        )
        item = params.get("item") if isinstance(params.get("item"), dict) else {}
        item_id = str(params.get("itemId") or item.get("id") or "")
        if method.startswith(("item/agentMessage/", "item/reasoning/", "item/plan/",
                              "item/commandExecution/", "item/fileChange/")):
            compaction_execution_seen = True

        native_turn = params.get("turn") if isinstance(params.get("turn"), dict) else {}
        native_turn_id = str(params.get("turnId") or native_turn.get("id") or "")
        if method in {"turn/started", "turn/completed"} and native_turn_id in completed_native_turn_ids:
            # A retained stream may repeat A's terminal after B has started.
            # It cannot finish or rebind the live B supervisor.
            return False
        if method == "turn/started" and native_turn_id:
            waiting_for_child_continuation = False
            if native_answer_turn_id != native_turn_id:
                pending_compaction_items.clear()
            if native_answer_turn_id and native_answer_turn_id != native_turn_id:
                # Preserve previous answers as their original timeline events;
                # only the latest native answer belongs in terminal result_text.
                await flush_pending_unknown(final=True)
                text_parts.clear()
                seen_text.clear()
            native_answer_turn_id = native_turn_id

        if turn is not None and turn.turn_id:
            ambiguous_turn_start = False
            await bind_active_turn_and_reconcile_stop()

        stopped_output = stop_requested_for_current_run()
        if stopped_output:
            # Phase-less agent messages can be final output. Once the user
            # stops a run, never promote one of these buffered messages into
            # durable commentary merely because a later trace item completes.
            pending_unknown_message = ""
            pending_unknown_item_id = ""

        if method != "turn/completed" and delivery_unknown:
            return False

        if stopped_output and method not in {
            # These deltas are not persisted by themselves. They only provide
            # bounded fallback text if the corresponding completed,
            # user-visible trace item omits its aggregate payload.
            "item/agentMessage/delta",
            "item/reasoning/summaryTextDelta",
            "item/reasoning/summaryPartAdded",
            "item/reasoning/textDelta",
            "item/plan/delta",
            "item/commandExecution/outputDelta",
            "item/fileChange/outputDelta",
            # Preserve the durable lifecycle of tools and completed trace
            # items emitted while app-server is acknowledging interruption.
            "item/started",
            "item/completed",
            "turn/completed",
        }:
            return False

        if method == "item/agentMessage/delta" and item_id:
            assistant_deltas.setdefault(item_id, []).append(
                str(params.get("delta") or "")
            )
            return False
        if method == "item/reasoning/textDelta" and item_id:
            if ("reasoning", item_id) not in emitted_reasoning_item_ids:
                await update_reasoning_summary_stream(session_id, current_run_id, item_id, params, current_metadata(), "reasoning")
            return False
        if method in {"item/reasoning/summaryTextDelta", "item/reasoning/summaryPartAdded"} and item_id:
            if item_id in emitted_reasoning_item_ids:
                return False
            reasoning_summary_deltas.setdefault(item_id, []).append(
                str(params.get("delta") or "")
            )
            await update_reasoning_summary_stream(session_id, current_run_id, item_id, params, current_metadata())
            return False
        if method == "item/plan/delta" and item_id:
            plan_deltas.setdefault(item_id, []).append(
                str(params.get("delta") or "")
            )
            return False
        if method in {
            "item/commandExecution/outputDelta",
            "item/fileChange/outputDelta",
        } and item_id:
            append_tool_output_delta(item_id, params.get("delta"))
            return False
        if method == "item/started" and item:
            if item.get("type") == "contextCompaction" and item_id:
                pending_compaction_items.add(item_id)
            elif item.get("type") != "userMessage":
                compaction_execution_seen = True
            tool = codex_app_server_tool(item)
            if tool:
                await emit_tool_started(tool)
            changed_paths.update(codex_app_server_changed_paths(item))
            return False
        if method == "item/completed" and item:
            if item.get("type") == "contextCompaction":
                pending_compaction_items.discard(item_id)
            elif item.get("type") != "userMessage":
                compaction_execution_seen = True
            item_type = str(item.get("type") or "")
            if item_type == "agentMessage":
                buffered = assistant_deltas.pop(
                    str(item.get("id") or ""),
                    [],
                )
                text = clean_assistant_text(
                    str(
                        item.get("text")
                        or "".join(buffered)
                    )
                )
                phase = str(item.get("phase") or "")
                if phase == "commentary":
                    await flush_pending_unknown(final=False)
                    await emit_reasoning(
                        text,
                        phase="commentary",
                        item_id=item_id,
                    )
                elif phase == "final_answer":
                    if not stopped_output:
                        await flush_pending_unknown(final=False)
                        await emit_final_text(text, item_id=item_id)
                elif text and not stopped_output:
                    await flush_pending_unknown(final=False)
                    pending_unknown_message = text
                    pending_unknown_item_id = item_id
            elif item_type == "reasoning":
                await flush_pending_unknown(final=False)
                await emit_completed_reasoning(item, item_id)
            elif item_type == "plan":
                await flush_pending_unknown(final=False)
                buffered = plan_deltas.pop(
                    str(item.get("id") or ""),
                    [],
                )
                plan = codex_reasoning_text(item) or "".join(buffered)
                await emit_reasoning(plan, phase="plan", item_id=item_id)
            else:
                changed_paths.update(codex_app_server_changed_paths(item))
                await emit_tool_finished(item)
            return False
        if method == "error":
            error_value = params.get("error") or params.get("message") or params
            message = concise_error_message(error_value)
            if is_codex_app_server_retry_notice(params, message):
                return False
            terminal_error = message
            # Delay terminal error publication until turn/completed. A stale
            # resumed thread may be recovered transparently onto a fresh
            # bounded-memory thread.
            return False
        if method == "turn/completed":
            completed_turn = (
                params.get("turn")
                if isinstance(params.get("turn"), dict)
                else {}
            )
            terminal_status = str(completed_turn.get("status") or "failed")
            compaction_failed = terminal_status == "failed" and bool(pending_compaction_items)
            if completed_turn.get("error"):
                terminal_error = concise_error_message(completed_turn.get("error"))
            elif terminal_status == "completed":
                terminal_error = None
            turn_completed = True
            if native_turn_id:
                completed_native_turn_ids.add(native_turn_id)
            return True
        return False

    def matching_ambiguous_turn(
        turns: Any,
    ) -> dict[str, Any] | None:
        if not isinstance(turns, list):
            return None
        for candidate in reversed(turns):
            if not isinstance(candidate, dict):
                continue
            if not codex_turn_matches_client_user_message(
                candidate,
                current_run_id,
            ):
                continue
            started_at = candidate.get("startedAt")
            if not isinstance(started_at, (int, float)):
                continue
            if started_at + 2 < turn_start_epoch:
                break
            candidate_id = str(candidate.get("id") or "")
            candidate_status = str(candidate.get("status") or "")
            if candidate_id and candidate_status in {
                "inProgress",
                "completed",
                "interrupted",
                "failed",
            }:
                return candidate
        return None

    async def read_ambiguous_turn(
        deadline: float,
    ) -> dict[str, Any] | None:
        now = time.monotonic()
        if now >= deadline:
            return None
        reconcile_timeout = max(
            0.01,
            min(
                5.0,
                float(CODEX_APP_SERVER_TIMEOUT_SECONDS),
                max(0.0, deadline - now),
            ),
        )
        turns = await asyncio.wait_for(
            manager.list_turns(
                provider_id,
                limit=4,
                items_view="full",
                sort_direction="desc",
            ),
            timeout=reconcile_timeout,
        )
        return matching_ambiguous_turn(turns)

    async def reconcile_cancelled_provisional_acceptance() -> bool:
        """Keep a cancelled post-write handle routed until acceptance settles."""

        if turn is None or turn.turn_id:
            return bool(turn is not None and turn.turn_id)
        deadline = (
            time.monotonic()
            + max(0.01, float(CODEX_APP_SERVER_AMBIGUOUS_ACCEPT_SECONDS))
        )
        try:
            recovered_turn = await read_ambiguous_turn(deadline)
        except Exception as exc:
            logger.debug(
                "cancelled Codex turn reconciliation pending session=%s: %s",
                session_id,
                exc,
            )
        else:
            if recovered_turn is not None and not turn.turn_id:
                turn.adopt_turn_id(str(recovered_turn.get("id") or ""))

        # The low-latency proof is a routed notification.  Leave the exact
        # provisional subscription installed through the full bounded window;
        # _route_notification binds its turn id before enqueueing the event.
        while not turn.turn_id:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                await turn.next_notification(timeout=remaining)
            except asyncio.TimeoutError:
                break
            except (CodexAppServerError, CodexAppServerSubscriptionClosed):
                break
        return bool(turn.turn_id)

    async def reconcile_ambiguous_start() -> bool:
        """Resolve a lost turn/start acknowledgement without replaying input."""
        nonlocal ambiguous_turn_start, ambiguous_reconcile_at
        nonlocal ambiguous_reconcile_attempted
        nonlocal terminal_status, terminal_error, turn_completed, error_emitted
        nonlocal delivery_unknown, provisional_thread_invalidated
        if not ambiguous_turn_start or turn is None or turn.turn_id:
            ambiguous_turn_start = False if turn is not None and turn.turn_id else ambiguous_turn_start
            return False

        now = time.monotonic()
        deadline_reached = now >= ambiguous_accept_deadline
        if now < ambiguous_reconcile_at and not deadline_reached:
            return False
        should_read = not ambiguous_reconcile_attempted
        ambiguous_reconcile_attempted = True
        ambiguous_reconcile_at = ambiguous_accept_deadline
        recovered_turn: dict[str, Any] | None = None
        if should_read:
            try:
                reconcile_timeout = max(
                    0.01,
                    min(
                        5.0,
                        float(CODEX_APP_SERVER_TIMEOUT_SECONDS),
                        max(0.0, ambiguous_accept_deadline - now),
                    ),
                )
                turns = await asyncio.wait_for(
                    manager.list_turns(
                        provider_id,
                        limit=4,
                        items_view="full",
                        sort_direction="desc",
                    ),
                    timeout=reconcile_timeout,
                )
                recovered_turn = matching_ambiguous_turn(turns)
            except Exception as exc:
                logger.debug(
                    "ambiguous Codex turn reconciliation pending session=%s: %s",
                    session_id,
                    exc,
                )
        now = time.monotonic()

        if recovered_turn is not None:
            turn.adopt_turn_id(str(recovered_turn.get("id") or ""))
            ambiguous_turn_start = False
            await bind_active_turn_and_reconcile_stop()
            status = str(recovered_turn.get("status") or "")
            if status != "inProgress":
                for recovered_item in recovered_turn.get("items") or []:
                    if isinstance(recovered_item, dict):
                        await handle_notification({
                            "method": "item/completed",
                            "params": {
                                "threadId": provider_id,
                                "turnId": turn.turn_id,
                                "item": recovered_item,
                            },
                        })
                terminal_status = status or "failed"
                error_value = recovered_turn.get("error")
                if error_value:
                    terminal_error = concise_error_message(error_value)
                turn_completed = True
                return True
            return False

        if now < ambiguous_accept_deadline:
            return False
        terminal_status = "failed"
        terminal_error = (
            "Codex app-server did not confirm whether the turn was accepted. "
            "The message was not replayed; retry it manually."
        )
        delivery_unknown = True
        provisional_thread_invalidated = True
        await manager.retire_generation(turn_start_generation)
        await append_event(session_id, "error", {
            "run_id": current_run_id,
            "backend": BACKEND_CODEX,
            "transport": CODEX_TRANSPORT_APP_SERVER,
            "message": terminal_error,
            "delivery_unknown": True,
            **current_metadata(),
        })
        error_emitted = True
        return True

    try:
        await manager.start()
        bound, stop_requested = await bind_active_turn(
            session_id,
            current_run_id,
            {
                "proc": None,
                "run_id": current_run_id,
                "backend": BACKEND_CODEX,
                "transport": CODEX_TRANSPORT_APP_SERVER,
                "pid": None,
                "pgid": None,
                "cwd": cwd,
                "argv": public_cmd,
                "started_at": time.time(),
                "started_at_iso": now_iso(),
                "provider_turn_ready": False,
                "standalone_provider_context": bool(
                    standalone_provider_context
                ),
                # Publish the provider identity only after the exact per-run
                # environment has been loaded under its runtime lease.
                "provider_thread_id": None,
                "provider_turn_id": None,
                "provider_model": model,
                "provider_effort": effort,
                "provider_service_tier": service_tier,
                "provider_tools_ready": asyncio.Event(),
                "native_interrupt_sent": False,
                "codex_app_server_turn": None,
                "interactive_app_server": interactive_app_server,
                "codex_approval_policy": approval_policy,
                # turn/steer cannot replace the run-bound Responses metadata.
                # Authority-bearing Force Send therefore uses the existing
                # Stop -> queued fresh-start lifecycle; the thread remains
                # loaded. Preserve native steer for no-authority callers.
                "native_steer_queue": (
                    None
                    if runtime_env or provider_command is not None
                    else steer_queue
                ),
                # Goal steering retains this exact run, Responses metadata,
                # authority and tool processes. Advertise separately even when
                # ordinary logical-run replacement is unavailable.
                "codex_goal_steer_queue": (
                    steer_queue
                    if not standalone_provider_context
                    else None
                ),
                "stdout_tail": deque(maxlen=LIVE_STDOUT_MAX_LINES),
                "stdout_total_lines": 0,
                "stdout_updated_at": None,
            },
        )
        if not bound:
            return
        if stop_requested:
            terminal_status = "interrupted"
        else:
            provider_id = await acquire_codex_run_thread(
                manager,
                session_id,
                sess,
                cwd,
                standalone_provider_context=standalone_provider_context,
                expected_run_id=current_run_id,
            )
            thread_pinned = True
            if not await turn_slot_is_owned(session_id, current_run_id):
                return
            if not standalone_provider_context:
                persisted_provider = await persist_run_provider_session(
                    session_id,
                    current_run_id,
                    BACKEND_CODEX,
                    provider_id,
                )
                if not persisted_provider:
                    return
            async with RUNS.active_lock:
                active = RUNS.active.get(session_id)
                active_owned = bool(
                    active
                    and str(active.get("run_id") or "") == current_run_id
                    and session_id in RUNS.busy
                    and str(
                        (RUNS.current_turns.get(session_id) or {}).get("run_id") or ""
                    ) == current_run_id
                )
                stop_requested = bool(
                    active_owned and active.get("stop_requested")
                )
                if active_owned:
                    active["provider_thread_id"] = provider_id
                    active["provider_session_id"] = provider_id
            if not active_owned:
                return

            if stop_requested:
                RUNS.stopped_runs.add(current_run_id)
                terminal_status = "interrupted"
            else:
                overrides: dict[str, Any] = {
                    "cwd": cwd,
                    "summary": "detailed",
                    "clientUserMessageId": current_run_id,
                    "responsesapiClientMetadata": {
                        "hyadex_run_id": current_run_id,
                        "hyadex_run_proof": codex_provider_mcp_run_proof(
                            session_id,
                            provider_id,
                            current_run_id,
                        ),
                    },
                }
                permission_profile = (
                    str(
                        sess.get("codex_permission_profile")
                        or CODEX_DEFAULT_PERMISSION_PROFILE
                        or ""
                    ).strip()
                    if interactive_app_server
                    else ""
                )
                overrides["approvalPolicy"] = approval_policy
                if permission_profile:
                    # Permission profiles are an experimental app-server
                    # override and are mutually exclusive with sandbox policy,
                    # but approvalPolicy still controls when Codex asks.
                    overrides["permissions"] = permission_profile
                else:
                    sandbox_mode = (
                        str(
                            sess.get("codex_sandbox_mode")
                            or CODEX_DEFAULT_SANDBOX_MODE
                        )
                        if interactive_app_server
                        else CODEX_DEFAULT_SANDBOX_MODE
                    )
                    overrides["sandboxPolicy"] = {
                        "type": CODEX_SANDBOX_POLICY_TYPES.get(
                            sandbox_mode,
                            CODEX_SANDBOX_POLICY_TYPES[
                                CODEX_DEFAULT_SANDBOX_MODE
                            ],
                        )
                    }
                if interactive_app_server:
                    approvals_reviewer = str(
                        sess.get("codex_approvals_reviewer")
                        or CODEX_DEFAULT_APPROVALS_REVIEWER
                    )
                    overrides["approvalsReviewer"] = (
                        approvals_reviewer
                        if approvals_reviewer in CODEX_APPROVAL_REVIEWERS
                        else CODEX_DEFAULT_APPROVALS_REVIEWER
                    )
                if model:
                    overrides["model"] = model
                if effort:
                    overrides["effort"] = effort
                if service_tier:
                    overrides["serviceTier"] = codex_app_server_service_tier(service_tier)

                turn_start_attempted = True
                if compaction_continuation:
                    overrides.pop("clientUserMessageId", None)
                turn_start_epoch = time.time()
                install_child_continuation_observer()
                try:
                    turn = await manager.start_turn(
                        provider_id,
                        [] if compaction_continuation else codex_provider_command_turn_input(
                            prompt,
                            provider_command,
                        ),
                        overrides=overrides,
                        retain_thread_stream=not standalone_provider_context,
                    )
                except asyncio.CancelledError as exc:
                    pending_turn = getattr(exc, "pending_turn", None)
                    if pending_turn is not None:
                        # Preserve ownership across the cancellation boundary;
                        # the common finally block below must release the
                        # provisional client mapping before the thread unpins.
                        turn = pending_turn
                        turn_start_generation = int(
                            getattr(pending_turn, "transport_generation", 0)
                        )
                        cancelled_provisional_turn = True
                    raise
                except CodexAppServerError as exc:
                    pending_turn = getattr(exc, "pending_turn", None)
                    if pending_turn is None:
                        # Replay is safe only when transport delivery was
                        # provably impossible, or app-server explicitly
                        # rejected the request.  A generic ``safe_to_retry``
                        # flag must not override evidence that bytes were
                        # written because acceptance would then be ambiguous.
                        safe_pre_accept_failure = (
                            bool(getattr(exc, "safe_to_retry", False))
                            and (
                                isinstance(exc, CodexAppServerRequestError)
                                or not bool(getattr(exc, "request_sent", False))
                            )
                        )
                        raise
                    # Delivery is ambiguous. Observe the routed provisional
                    # handle and never replay the user input through exec.
                    turn = pending_turn
                    turn_start_generation = int(
                        getattr(pending_turn, "transport_generation", 0)
                    )
                    terminal_error = concise_error_message(exc)
                    ambiguous_turn_start = True
                    ambiguous_accept_deadline = (
                        time.monotonic()
                        + CODEX_APP_SERVER_AMBIGUOUS_ACCEPT_SECONDS
                    )
                    ambiguous_reconcile_at = time.monotonic()

                async with RUNS.active_lock:
                    active = RUNS.active.get(session_id)
                    active_owned = bool(
                        active
                        and str(active.get("run_id") or "") == current_run_id
                        and session_id in RUNS.busy
                        and str(
                            (RUNS.current_turns.get(session_id) or {}).get("run_id")
                            or ""
                        ) == current_run_id
                    )
                    if active_owned:
                        active["codex_app_server_turn"] = turn
                        if not native_answer_turn_id:
                            native_answer_turn_id = str(turn.turn_id or "")
                        active["provider_thread_id"] = provider_id
                        active["provider_session_id"] = provider_id
                        active["provider_turn_id"] = turn.turn_id or None
                        active["provider_turn_ready"] = bool(turn.turn_id)
                        tools_ready = active.get("provider_tools_ready")
                        if turn.turn_id and isinstance(tools_ready, asyncio.Event):
                            tools_ready.set()
                        stop_requested = bool(active.get("stop_requested"))
                if not active_owned:
                    if turn is not None and turn.turn_id:
                        with suppress(CodexAppServerError):
                            await turn.interrupt()
                    return
                await bind_active_turn_and_reconcile_stop()

                if codex_goal_time_budget_remaining(sess) is not None:
                    goal_time_budget_task = asyncio.create_task(
                        enforce_goal_time_budget()
                    )
                manifest_watch_task = asyncio.create_task(
                    watch_manifest_artifacts(
                        session_id,
                        current_run_id,
                        manifest_path,
                        seen_artifacts,
                    )
                )
                last_activity = time.monotonic()
                first_activity_seen = False
                notification_task = asyncio.create_task(
                    next_sequenced_notification()
                )
                steer_task = asyncio.create_task(steer_queue.get())
                child_continuation_stop_task = asyncio.create_task(child_continuation_stop.wait())
                child_continuation_change_task = asyncio.create_task(child_continuation_changes.wait())
                try:
                    while not turn_completed:
                        if await reconcile_ambiguous_start():
                            break
                        wait_timeout = None if waiting_for_child_continuation else 5.0
                        if (
                            ambiguous_turn_start
                            and turn is not None
                            and not turn.turn_id
                        ):
                            now = time.monotonic()
                            wait_timeout = max(
                                0.01,
                                min(
                                    wait_timeout or 5.0,
                                    max(0.0, ambiguous_accept_deadline - now),
                                    max(0.0, ambiguous_reconcile_at - now),
                                ),
                            )
                        done, _pending = await asyncio.wait(
                            {notification_task, steer_task, child_continuation_stop_task, child_continuation_change_task}
                            if waiting_for_child_continuation else {notification_task, steer_task},
                            timeout=wait_timeout,
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                        if child_continuation_stop_task in done:
                            if turn is not None and not getattr(turn, "_completed", False):
                                await bind_active_turn_and_reconcile_stop()
                                waiting_for_child_continuation = False
                                continue  # Observe the interrupted native B terminal before releasing.
                            terminal_status = "interrupted"
                            turn_completed = True
                            break
                        if not done:
                            idle = time.monotonic() - last_activity
                            if (
                                not first_activity_seen
                                and idle >= CODEX_APP_SERVER_FIRST_ACTIVITY_TIMEOUT_SECONDS
                            ):
                                # turn/start was accepted but nothing at all
                                # has come back since - most likely a
                                # notification for this turn was dropped by
                                # _route_notification finding no matching
                                # subscription (see its docstring). Fail fast
                                # instead of waiting out the much longer
                                # IDLE_KILL_SECONDS meant for gaps *between*
                                # activity on an already-productive turn.
                                terminal_error = (
                                    "Codex app-server produced no activity within "
                                    f"{CODEX_APP_SERVER_FIRST_ACTIVITY_TIMEOUT_SECONDS}s "
                                    "of accepting this turn."
                                )
                                first_activity_stalled = True
                                dropped = [
                                    entry for entry in manager.client.unmatched_notifications
                                    if entry.get("thread_id") == provider_id
                                ]
                                logger.warning(
                                    "codex app-server first-activity timeout session=%s run=%s thread=%s dropped_notifications=%s",
                                    session_id,
                                    current_run_id,
                                    provider_id,
                                    dropped,
                                )
                                if turn.turn_id:
                                    with suppress(CodexAppServerError):
                                        await turn.interrupt()
                                terminal_status = "failed"
                                break
                            if idle >= IDLE_WARN_SECONDS:
                                await append_event(session_id, "idle_warning", {
                                    "run_id": current_run_id,
                                    "idle_seconds": int(idle),
                                })
                            if idle >= IDLE_KILL_SECONDS:
                                terminal_error = "Codex app-server turn exceeded the idle timeout."
                                if turn.turn_id:
                                    with suppress(CodexAppServerError):
                                        await turn.interrupt()
                                terminal_status = "failed"
                                break
                            continue

                        # Drain provider output before a queued steering command
                        # when both became ready together.
                        if notification_task in done:
                            sequence, notification = notification_task.result()
                            async with RUNS.active_lock:
                                current_owner = RUNS.active.get(session_id)
                                still_owned = bool(
                                    current_owner
                                    and current_owner.get("run_id") == current_run_id
                                    and current_owner.get("codex_app_server_turn") is turn
                                    and (RUNS.current_turns.get(session_id) or {}).get("run_id") == current_run_id
                                    and session_id in RUNS.busy
                                    and (
                                        not child_continuation_generation
                                        or getattr(manager, "generation", None) == child_continuation_generation
                                    )
                                )
                            if not still_owned:
                                terminal_status = "interrupted"
                                turn_completed = True
                                break
                            handled_notification_sequence = max(
                                handled_notification_sequence,
                                sequence,
                            )
                            last_activity = time.monotonic()
                            first_activity_seen = True
                            async with logical_state_lock:
                                completed = await handle_notification(notification)
                            if completed:
                                if not await retain_ordinary_child_continuation(notification):
                                    break
                            notification_task = asyncio.create_task(
                                next_sequenced_notification()
                            )
                            if steer_task not in done:
                                continue

                        if child_continuation_change_task in done:
                            child_continuation_changes.clear()
                            await continue_after_owned_children()
                            child_continuation_change_task = asyncio.create_task(child_continuation_changes.wait())
                            if steer_task not in done:
                                continue

                        if steer_task in done:
                            command = steer_task.result()
                            mark_native_steer_accepted(command)
                            future = command.get("future")
                            selected_for_steer = command.get("selected") or {}
                            try:
                                async with logical_state_lock:
                                    live_goal = (STORE.sessions.get(session_id) or {}).get("codex_goal")
                                    # Goal creation can race with queue admission. Do not
                                    # rotate ownership after a goal became active merely
                                    # because this request was admitted a moment earlier.
                                    if (
                                        command.get("goal_identity") is None
                                        and isinstance(live_goal, dict)
                                        and live_goal.get("status") == "active"
                                    ):
                                        command["goal_identity"] = (
                                            str(live_goal.get("id") or ""),
                                            str(live_goal.get("objective") or ""),
                                        )
                                    if command.get("goal_identity") is not None:
                                        pending = await send_codex_goal_steer(
                                            session_id, current_run_id, manager, provider_id,
                                            "", turn._subscription, steer_queue, command,
                                        )
                                        # The original prompt no longer represents all
                                        # accepted user input. Never replay only that old
                                        # prompt through context-recovery after this point.
                                        goal_steer_recovery_fenced = True
                                        try:
                                            await drain_notifications_through(
                                                pending["watermark"], handoff_at_completion=True,
                                            )
                                            if handled_notification_sequence < pending["watermark"]:
                                                pending_goal_steer_handoff = pending
                                                break
                                            result = await commit_codex_goal_steer(
                                                session_id, current_run_id, provider_id, "", pending,
                                            )
                                        except BaseException as exc:
                                            # Provider acceptance is already proven. A failed
                                            # projection must never replay that input or stop
                                            # the goal merely to change logical ownership.
                                            if isinstance(exc, asyncio.CancelledError):
                                                raise
                                            raise NativeSteerHandoffError(
                                                concise_error_message(exc),
                                                safe_to_requeue=False, delivery_uncertain=True,
                                            ) from exc
                                        last_activity = time.monotonic()
                                    else:
                                        result = await switch_logical_run(
                                            selected_for_steer,
                                            int(command.get("remaining") or 0),
                                        )
                                if future is not None and not future.done():
                                    future.set_result(result)
                            except asyncio.CancelledError:
                                if future is not None and not future.done():
                                    fenced = bool(
                                        selected_for_steer.get(
                                            "_native_delivery_fenced"
                                        )
                                    )
                                    future.set_exception(
                                        NativeSteerHandoffError(
                                            "Codex steering was interrupted at the delivery boundary",
                                            safe_to_requeue=not fenced,
                                            delivery_uncertain=fenced,
                                        )
                                    )
                                raise
                            except Exception as exc:
                                if (
                                    command.get("goal_identity") is not None
                                    and getattr(exc, "delivery_uncertain", False)
                                ):
                                    goal_steer_recovery_fenced = True
                                if future is not None and not future.done():
                                    # Do not transfer a traceback that still
                                    # owns this live runner frame into another
                                    # task. Consumers such as unittest may
                                    # clear received traceback frames, which
                                    # can close this coroutine on Python 3.12.
                                    future.set_exception(exc.with_traceback(None))
                            if notification_task is None and not turn_completed:
                                notification_task = asyncio.create_task(
                                    next_sequenced_notification()
                                )
                            steer_task = asyncio.create_task(steer_queue.get())
                finally:
                    # Close native steering admission before inspecting or
                    # draining the queue. The ACTIVE lock orders this against
                    # Force Send's final queue-identity check: an earlier put is
                    # drained below, while a later put is rejected and requeued.
                    await detach_native_steer_admission()
                    if steer_task.done() and not steer_task.cancelled():
                        with suppress(Exception):
                            command = steer_task.result()
                            future = command.get("future")
                            if (
                                future is not None and not future.done()
                                and (
                                    pending_goal_steer_handoff is None
                                    or command is not pending_goal_steer_handoff["request"]
                                )
                            ):
                                future.set_exception(
                                    NativeSteerHandoffError(
                                        "the Codex turn completed before steering was applied",
                                        safe_to_requeue=True,
                                    )
                                )
                    cleanup_tasks = [
                        task
                        for task in (notification_task, steer_task, child_continuation_stop_task, child_continuation_change_task)
                        if task is not None
                    ]
                    for task in cleanup_tasks:
                        if not task.done():
                            task.cancel()
                    await asyncio.gather(
                        *cleanup_tasks,
                        return_exceptions=True,
                    )
                    while not steer_queue.empty():
                        command = steer_queue.get_nowait()
                        future = command.get("future")
                        if future is not None and not future.done():
                            future.set_exception(
                                NativeSteerHandoffError(
                                    "the Codex turn completed before steering was applied",
                                    safe_to_requeue=True,
                                )
                            )

                if (
                    turn_completed and terminal_status == "completed"
                    and not standalone_provider_context
                    and await retain_codex_goal_run_owner(
                        session_id, current_run_id, manager, provider_id,
                        subscription=turn._subscription,
                        handled_sequence=handled_notification_sequence,
                    )
                ):
                    # Complete the initial answer, but not the supervised run.
                    if child_notification_handler is not None:
                        manager.remove_notification_handler(child_notification_handler)
                        child_notification_handler = None
                    # Its thread-wide stream already contains any early native
                    # continuation, including goals created by provider tools.
                    await flush_pending_unknown(final=True)
                    if goal_time_budget_task is not None:
                        goal_time_budget_task.cancel()
                        await asyncio.gather(goal_time_budget_task, return_exceptions=True)
                        goal_time_budget_task = None
                    goal_continuation_result = await consume_codex_native_turn(
                        session_id, current_run_id, "goal_resume", manager,
                        provider_id, current_run_id, turn._subscription,
                        finalize_operation=False,
                        initial_sequence=handled_notification_sequence,
                        initial_pending_steer=pending_goal_steer_handoff,
                    )
                    terminal_status = goal_continuation_result["status"]
                    terminal_error = goal_continuation_result["error"]
                    # Never overwrite a later goal answer with the first reply
                    # in the terminal payload when the goal eventually ends.
                    text_parts[:] = [goal_continuation_result["result_text"]]

        stopped = (
            terminal_status in {"interrupted", "cancelled", "canceled"}
            or current_run_id in RUNS.stopped_runs
        )
    except asyncio.CancelledError:
        # The post-processing finalizer below is after this try statement and
        # therefore cannot run on cancellation. Remember the exceptional exit
        # so the common transport cleanup can be followed by an exact,
        # cancellation-shielded slot + pin reconciliation.
        cancelled_exit = True
        raise
    except Exception as exc:
        # Failures before the notification loop owns its cleanup must not leave
        # this runner's queue advertised while fallback/error work awaits.
        await detach_native_steer_admission()
        planned_transport_shutdown = (
            isinstance(exc, CodexAppServerDisconnected)
            and (
                bool(getattr(exc, "planned", False))
                or managed_server_restart_is_planned()
            )
        )
        stop_requested = (
            current_run_id in RUNS.stopped_runs
            or planned_transport_shutdown
        )
        if (
            safe_pre_accept_failure
            and thread_pinned
            and provider_id
        ):
            # A pre-accept failure proves the provider accepted no user input.
            # Retire the cached subscription even when exec replay is disabled
            # (for example, interactive_v1). This releases only this runner's
            # pin; concurrent owners remain fenced.
            await unpin_codex_app_server_thread(
                manager,
                provider_id,
                invalidate_loaded_thread=True,
            )
            thread_pinned = False
        can_fallback = (
            allow_exec_fallback
            and not codex_native_archive_rejection(exc, str(session_provider_id(sess) or ""))
            and provider_command is None
            and not stop_requested
            and (
                not turn_start_attempted
                or safe_pre_accept_failure
            )
        )
        if can_fallback:
            if turn is not None:
                await turn.close()
            cleared = await clear_active_process(
                session_id,
                expected_run_id=current_run_id,
            )
            if not cleared:
                return
            await append_event(session_id, "codex_transport_fallback", {
                "run_id": current_run_id,
                "from": CODEX_TRANSPORT_APP_SERVER,
                "to": CODEX_TRANSPORT_EXEC,
                "message": (
                    "Codex app-server was unavailable before the turn was "
                    f"accepted; using codex exec. {concise_error_message(exc)}"
                ),
            })
            if not standalone_provider_context:
                await mark_codex_exec_context_usage_unavailable(session_id)
            await run_codex_exec(
                session_id,
                current_run_id,
                current_provider_prompt,
                dict(STORE.sessions.get(session_id) or sess),
                manifest_path,
                diff_baseline=current_diff_baseline,
                standalone_provider_context=standalone_provider_context,
                provider_runtime_env=runtime_env,
            )
            return
        if (
            turn is not None and turn.turn_id and not turn_completed
            and (not child_continuation_attempts or (
                child_continuation_owner_current() and not getattr(turn, "_completed", False)
            ))
        ):
            with suppress(CodexAppServerError):
                await turn.interrupt()
        if not planned_transport_shutdown:
            terminal_error = terminal_error or concise_error_message(exc)
        stopped = stop_requested
        terminal_status = "interrupted" if stopped else "failed"
        if not stopped:
            await append_event(session_id, "error", {
                "run_id": current_run_id,
                "backend": BACKEND_CODEX,
                "message": terminal_error,
                "transport": CODEX_TRANSPORT_APP_SERVER,
                **current_metadata(),
            })
            error_emitted = True
            if codex_native_archive_rejection(exc, str(session_provider_id(sess) or "")):
                try:
                    await archive_rejected_codex_session(
                        session_id, str(session_provider_id(sess)), current_run_id,
                    )
                except Exception:
                    # Preserve the actual provider error if local retirement
                    # fails. Never pretend the native thread was unarchived.
                    logger.exception("Could not mirror native Codex archive session=%s", session_id)
    finally:
        async def settle_transport_finalizer() -> None:
            """Retire every owner before cancellation can escape the runner."""
            nonlocal provisional_thread_invalidated, child_acceptance_parent_id

            try:
                for summary_run_id in reasoning_stream_run_ids:
                    await finish_reasoning_summary_stream(
                        session_id, summary_run_id, emitted_reasoning_item_ids)
                if pending_goal_steer_handoff is not None:
                    future = pending_goal_steer_handoff["request"].get("future")
                    if future is not None and not future.done():
                        # Stop, failed completion or cancellation can prevent
                        # the native consumer handoff after provider acceptance.
                        # Settle its caller without replaying or pausing a goal.
                        future.set_exception(NativeSteerHandoffError(
                            "The goal continuation ended before the accepted follow-up could be projected; it was not replayed",
                            safe_to_requeue=False, delivery_uncertain=True,
                        ))
                # Cancellation can bypass the Exception handler. Keep this
                # idempotent identity-fenced detach as the final transport guard.
                await detach_native_steer_admission()
                if goal_time_budget_task is not None:
                    goal_time_budget_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await goal_time_budget_task
                await stop_manifest_watcher()
                if turn is not None:
                    if child_acceptance_parent_id:
                        # A cancelled empty continuation is exactly as unsafe
                        # to abandon as the first ambiguous turn/start. Keep
                        # its original subscription until a new native ID can
                        # be interrupted, or retire only that exact generation.
                        accepted = await reconcile_child_continuation_acceptance()
                        interrupted = bool(accepted and turn._completed)
                        if accepted and not interrupted and child_continuation_owner_current():
                            try:
                                await turn.interrupt()
                            except CodexAppServerError:
                                pass
                            else:
                                interrupted = True
                        if not interrupted and child_continuation_owner_current():
                            provisional_thread_invalidated = True
                            await manager.retire_generation(child_continuation_generation)
                        child_acceptance_parent_id = ""
                    if cancelled_provisional_turn:
                        interrupted = turn_completed
                        try:
                            bound = await reconcile_cancelled_provisional_acceptance()
                            if bound and not turn_completed:
                                # A routed notification or exact client id
                                # proved acceptance. Stop that exact native turn
                                # before releasing its subscription.
                                try:
                                    await turn.interrupt()
                                except CodexAppServerError:
                                    pass
                                else:
                                    interrupted = True
                            if not interrupted:
                                # No id (or no confirmed interrupt) means the
                                # provider may still be executing work that no
                                # runner can attribute. Invalidate the thread
                                # and retire only the process generation that
                                # received this request; a replacement is never
                                # targeted by this cleanup.
                                provisional_thread_invalidated = True
                                await manager.retire_generation(
                                    turn_start_generation
                                )
                        finally:
                            await turn.close()
                    else:
                        await turn.close()
                if provider_id:
                    # The low-latency subscription can finish before its async
                    # projection callbacks. Keep ACTIVE/run attribution alive
                    # through every notification already received for this thread.
                    await manager.wait_for_notification_handler(
                        project_codex_notification,
                        provider_id,
                    )
            finally:
                if child_notification_handler is not None:
                    manager.remove_notification_handler(child_notification_handler)
                if cancelled_exit:
                    await reconcile_cancelled_runner_exit()

        finalizer_task = asyncio.create_task(settle_transport_finalizer())
        try:
            await asyncio.shield(finalizer_task)
        except asyncio.CancelledError as cancellation:
            # The runner can be cancelled again while its first cancellation
            # is already unwinding. Keep the whole transport/slot/pin
            # transaction owned, not only its last release step.
            await join_task_despite_caller_cancellation(finalizer_task)
            raise cancellation

    produced_activity = bool(
        text_parts
        or seen_reasoning
        or emitted_reasoning_item_ids
        or started_tools
        or finished_tools
        or seen_artifacts
    )
    compaction_failed = terminal_status == "failed" and (
        compaction_failed or is_codex_compaction_failure(terminal_error or "")
    )
    async def prepare_compaction_retry() -> str | None:
        nonlocal thread_pinned
        if not (
            compaction_failed and allow_compaction_retry and not stopped
            and not standalone_provider_context and resumed_provider_id == provider_id
            and not produced_activity and not compaction_execution_seen
            and not delivery_unknown and not ambiguous_turn_start
            and not goal_steer_recovery_fenced and not child_continuation_attempts
            and goal_continuation_result is None and turn_completed
            and current_run_id == run_id and turn is not None
        ):
            return None
        # The ordered stream alone is insufficient: corroborate it with a
        # full, terminal native snapshot. Unsupported/failed reads fail closed.
        retry_mode = None
        try:
            turns = await asyncio.wait_for(
                manager.list_turns(provider_id, limit=1, items_view="full", sort_direction="desc"),
                timeout=5.0,
            )
            retry_mode = codex_compaction_retry_mode(turns, turn.turn_id, current_run_id)
        except Exception:
            pass
        if (retry_mode and await turn_slot_is_owned(session_id, current_run_id)
                and not stop_requested_for_current_run()):
            await append_event(session_id, "codex_compaction_retry", {
                "run_id": current_run_id, "backend": BACKEND_CODEX,
                "provider_thread_id": provider_id, "model": model,
                "message": (
                    f"Context compaction failed before a reply. Retrying once with {model} "
                    "in the same Codex conversation, without duplicating the message or tool execution."
                ),
            })
            if thread_pinned:
                await unpin_codex_app_server_thread(manager, provider_id)
                thread_pinned = False
            return retry_mode
        return None

    retry_mode = None
    try:
        retry_mode = await prepare_compaction_retry()
    except asyncio.CancelledError:
        # Recovery runs after the original stream has closed. Stop/restart
        # must still release the same run's slot and exact thread lease.
        cleanup = asyncio.create_task(reconcile_cancelled_runner_exit())
        await join_task_despite_caller_cancellation(cleanup)
        raise
    except Exception:
        pass
    if retry_mode:
        # The continuation owns terminal cleanup. Do not catch its failure and
        # finalize the original attempt a second time.
        await run_codex_app_server(
            session_id, current_run_id, current_provider_prompt, sess, manifest_path,
            allow_exec_fallback=False, interactive_app_server=interactive_app_server,
            allow_resume_rollover=False, allow_compaction_retry=False,
            compaction_continuation=retry_mode == "continue",
            diff_baseline=current_diff_baseline,
            standalone_provider_context=standalone_provider_context,
            provider_command=provider_command, provider_runtime_env=runtime_env,
        )
        return
    if stop_requested_for_current_run():
        stopped = True
        terminal_status = "interrupted"
        terminal_error = None
    if compaction_failed and terminal_error:
        terminal_error = codex_compaction_error_message(model, terminal_error)
    recover_resume = (
        not delivery_unknown
        and not compaction_failed
        and not goal_steer_recovery_fenced
        and not child_continuation_attempts
        and goal_continuation_result is None
        and should_recover_codex_resume(
            allow_rollover=allow_resume_rollover,
            resumed_provider_id=resumed_provider_id,
            stopped=stopped,
            stream_error=None,
            idle_killed=False,
            resume_stalled=first_activity_stalled,
            returncode=0 if terminal_status == "completed" else 1,
            produced_activity=produced_activity,
            terminal_error=terminal_error or "",
        )
    )
    if recover_resume and resumed_provider_id:
        recovery_reason = (
            terminal_error
            or "Codex app-server resumed the thread but completed without a reply."
        )
        rollover = await rollover_codex_provider_session(
            session_id,
            current_run_id,
            resumed_provider_id,
            recovery_reason,
            message=(
                "Codex could not continue the provider thread. Retrying this "
                "turn once on a fresh app-server thread with bounded memory."
            ),
            memory_seed_used=False,
        )
        if rollover:
            fresh_session, _memory = rollover
            if thread_pinned and provider_id:
                await unpin_codex_app_server_thread(manager, provider_id)
                thread_pinned = False
            with suppress(OSError):
                manifest_path.unlink()
            await run_codex_app_server(
                session_id,
                current_run_id,
                current_provider_prompt,
                fresh_session,
                manifest_path,
                allow_exec_fallback=allow_exec_fallback,
                interactive_app_server=interactive_app_server,
                allow_resume_rollover=False,
                diff_baseline=current_diff_baseline,
                standalone_provider_context=standalone_provider_context,
                provider_command=(
                    provider_command
                    if provider_command is not None
                    and provider_command_prompt_matches(
                        current_provider_prompt,
                        str(provider_command.public.get("invocation") or ""),
                    )
                    else None
                ),
                provider_runtime_env=runtime_env,
            )
            return

    terminal_finalization_started = False
    try:
        if terminal_status == "failed":
            terminal_error = terminal_error or "Codex app-server turn failed."
            if getattr(manager, "_hyadex_login_superseded", False) is not True:
                record_runtime_failure(BACKEND_CODEX, terminal_error)
            if not error_emitted:
                await append_event(session_id, "error", {
                    "run_id": current_run_id,
                    "backend": BACKEND_CODEX,
                    "message": terminal_error,
                    "transport": CODEX_TRANSPORT_APP_SERVER,
                    **current_metadata(),
                })
        elif not stopped and getattr(manager, "_hyadex_login_superseded", False) is not True:
            record_runtime_success(BACKEND_CODEX)

        await flush_pending_unknown(
            final=not stopped and terminal_status == "completed"
        )
        await collect_manifest(
            session_id,
            current_run_id,
            manifest_path,
            seen_artifacts=seen_artifacts,
            final=True,
        )
        await collect_recent_leftover_manifests(
            session_id,
            current_run_id,
            manifest_path,
            seen_artifacts=seen_artifacts,
        )
        await publish_turn_code_diff(
            session_id,
            current_run_id,
            BACKEND_CODEX,
            cwd,
            current_diff_baseline,
            changed_paths,
        )
        exit_code = (
            0
            if terminal_status == "completed"
            else None
            if stopped
            else 1
        )
        terminal_payload = {
            "run_id": current_run_id,
            "backend": BACKEND_CODEX,
            "transport": CODEX_TRANSPORT_APP_SERVER,
            "provider_thread_id": provider_id or None,
            "provider_turn_id": (
                goal_continuation_result.get("turn_id")
                if goal_continuation_result is not None
                else turn.turn_id if turn is not None else None
            ),
            "exit_code": exit_code,
            "result_text": clean_assistant_text("\n\n".join(text_parts).strip()),
            "stopped": stopped,
            **current_metadata(),
        }
        # Release this exact run before cross-chat terminal finalization. The
        # finalizer can synchronously admit a response turn into either chat;
        # keeping this completed run BUSY until that work returned made an
        # otherwise-idle chat look occupied and forced its response to queue.
        # Keep the durable terminal pipeline joined across caller cancellation
        # just like the subprocess runners do.
        terminal_task = asyncio.create_task(finalize_owned_turn_finished(
            session_id,
            current_run_id,
            stopped=stopped,
            payload=terminal_payload,
        ))
        terminal_finalization_started = True
        try:
            released = await asyncio.shield(terminal_task)
        except asyncio.CancelledError:
            released = await join_task_despite_caller_cancellation(
                terminal_task
            )
            raise
        if released and provider_id and not standalone_provider_context:
            # Native children spawned during this turn stay loaded in the
            # app-server until something unsubscribes them. Finalize them
            # after the parent's terminal event, detached and bounded, so a
            # large spawn tree can neither hold the turn slot nor stall the
            # chat if the app-server is slow to answer.
            schedule_codex_subagent_finalization(
                session_id,
                manager,
                run_id=current_run_id,
            )
    finally:
        fallback_released = False
        try:
            if not terminal_finalization_started:
                # Post-processing failed before the owned terminal pipeline
                # could start. Preserve the prior exact-owner recovery path;
                # the task supervisor will publish a missing terminal marker.
                RUNS.run_metadata.pop(current_run_id, None)
                fallback_released = await release_turn_slot(
                    session_id,
                    expected_run_id=current_run_id,
                )
        finally:
            try:
                if thread_pinned and provider_id:
                    if provisional_thread_invalidated:
                        await unpin_codex_app_server_thread(
                            manager,
                            provider_id,
                            invalidate_loaded_thread=True,
                        )
                    else:
                        await unpin_codex_app_server_thread(manager, provider_id)
            finally:
                if not terminal_finalization_started:
                    drain_queue = should_schedule_queue_after_finish(
                        session_id,
                        stopped,
                    )
                    RUNS.stopped_runs.discard(current_run_id)
                    # Unpinning is provider-cache hygiene after the chat slot
                    # has already been released. It must not strand an admitted
                    # successor if unpin raises or this runner is cancelled.
                    if fallback_released and drain_queue:
                        schedule_next_queued_turn(session_id)


async def run_codex(
    session_id: str,
    run_id: str,
    prompt: str,
    sess: dict[str, Any],
    manifest_path: Path,
    *,
    interactive_app_server: bool = False,
    standalone_provider_context: bool = False,
    provider_command: ProviderCommandRecord | None = None,
    provider_runtime_env: dict[str, str] | None = None,
) -> None:
    runtime_env = validate_provider_runtime_env(provider_runtime_env)
    if CODEX_TRANSPORT == CODEX_TRANSPORT_EXEC:
        if provider_command is not None:
            raise RuntimeError(
                "Codex provider skills require the app-server transport"
            )
        if not standalone_provider_context:
            await mark_codex_exec_context_usage_unavailable(session_id)
        await run_codex_exec(
            session_id,
            run_id,
            prompt,
            sess,
            manifest_path,
            provider_runtime_env=runtime_env,
            **(
                {"standalone_provider_context": True}
                if standalone_provider_context
                else {}
            ),
        )
        return
    await run_codex_app_server(
        session_id,
        run_id,
        prompt,
        sess,
        manifest_path,
        # Interactive approvals/questions cannot be represented by the exec
        # fallback. Never silently change security semantics after opt-in.
        allow_exec_fallback=(
            CODEX_TRANSPORT == CODEX_TRANSPORT_AUTO
            and not interactive_app_server
            and provider_command is None
        ),
        interactive_app_server=interactive_app_server,
        provider_runtime_env=runtime_env,
        **(
            {"provider_command": provider_command}
            if provider_command is not None
            else {}
        ),
        **(
            {"standalone_provider_context": True}
            if standalone_provider_context
            else {}
        ),
    )


async def start_turn(
    session_id: str,
    req: TurnRequest,
    *,
    queue_if_busy: bool = True,
    queued_id: str | None = None,
    display_file_ids: list[str] | None = None,
    steering_lineage: list[dict[str, Any]] | None = None,
    provider_context_mode: Literal["chat", "standalone"] = "chat",
    scheduled_job_revision: str | None = None,
    scheduled_job_manual_run: bool = False,
) -> dict[str, Any]:
    await wait_for_queue_recovery_admission(session_id)
    if queue_if_busy and provider_context_mode == "chat":
        await reconcile_idle_queue_session(
            session_id,
            schedule=True,
            reason="turn_admission",
        )
    # Capture the chat backend before this request can wait on lifecycle work.
    # If a concurrent backend PATCH wins the lock, the request must be retried
    # instead of applying a now-stale per-turn backend after the PATCH.
    admission_backend: str | None = None
    if provider_context_mode == "chat":
        admission_session = STORE.sessions.get(session_id)
        if admission_session is not None:
            admission_backend = str(
                admission_session.get("backend") or DEFAULT_BACKEND
            ).strip().lower()
    # Register the request task before BUSY_SESSIONS is reserved. Stop can then
    # distinguish a legitimate slow launch from an orphaned reservation and
    # leave a deferred stop marker for the runner to reconcile when it binds.
    request_task = asyncio.current_task()
    if request_task is not None:
        register_session_task(RUNS.turn_tasks, session_id, request_task)
    try:
        async with session_lifecycle_lock(session_id):
            ensure_session_not_deleting(session_id)
            return await _start_turn_locked(
                session_id,
                req,
                queue_if_busy=queue_if_busy,
                queued_id=queued_id,
                display_file_ids=display_file_ids,
                steering_lineage=steering_lineage,
                provider_context_mode=provider_context_mode,
                admission_backend=admission_backend,
                scheduled_job_revision=scheduled_job_revision,
                scheduled_job_manual_run=scheduled_job_manual_run,
            )
    finally:
        if request_task is not None:
            tasks = RUNS.turn_tasks.get(session_id)
            if tasks is not None:
                tasks.discard(request_task)
                if not tasks:
                    RUNS.turn_tasks.pop(session_id, None)


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
            task = run_codex(
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
            task = run_claude(
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


def managed_server_restart_is_planned() -> bool:
    return managed_server_restart_blocks_work()


def server_lifecycle_active_session_ids_locked() -> list[str]:
    """Return chat-scoped work that must drain before worker retirement."""

    return sorted(
        RUNS.busy
        | RUNS.maintenance
        | explicit_stop_session_ids()
    )


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


def interactive_client_capabilities(target: dict[str, Any]) -> list[str]:
    """Return the interactive client capability for a server-started turn.

    Group member turns have no attached client, so the server starts them with
    the capability of the interactive transport. Exec and print transports
    cannot service those turns and are rejected.
    """
    backend = str(target.get("backend") or DEFAULT_BACKEND).strip().lower()
    if backend == BACKEND_CODEX:
        if not interactive_backend_supported(backend):
            raise HTTPException(
                status_code=409,
                detail="Codex chats must use app-server transport for server-started turns",
            )
        return [CODEX_INTERACTIVE_CLIENT_CAPABILITY]
    if backend == BACKEND_CLAUDE:
        if not interactive_backend_supported(backend):
            raise HTTPException(
                status_code=409,
                detail="Claude chats must use Agent SDK transport for server-started turns",
            )
        return [CLAUDE_SDK_INTERACTIVE_CLIENT_CAPABILITY]
    raise HTTPException(
        status_code=409,
        detail=f"backend {backend!r} does not support server-started turns",
    )


async def start_turn_durably(session_id: str, req: TurnRequest) -> dict[str, Any]:
    return await start_turn(session_id, req, queue_if_busy=True)


async def start_claude_goal_command(session_id: str, argument: str) -> dict[str, Any]:
    session = require_claude_goal_session(session_id)
    snapshot, inventory = await discover_session_provider_commands(session_id, session)
    command = next((record for record in inventory.records
                    if record.public.get("invocation") == "/goal"), None)
    if snapshot["support"].get("available") is not True or command is None:
        raise HTTPException(status_code=409, detail="This Claude installation does not offer /goal. Update Claude Code to use Goals.")
    return await start_turn(session_id, TurnRequest(
        prompt=f"/goal {argument}",
        skill_selection=SkillSelection(id=command.public["id"], revision=inventory.revision),
        client_capabilities=[CLAUDE_SDK_INTERACTIVE_CLIENT_CAPABILITY],
    ), queue_if_busy=False)


async def set_claude_goal(session_id: str, req: ClaudeGoalRequest) -> dict[str, Any]:
    require_claude_goal_session(session_id)
    condition = req.condition.strip()
    if not condition or condition.lower() == "clear":
        raise HTTPException(status_code=400, detail="Enter a completion condition for Claude")
    previous = await refresh_claude_goal(session_id, notify=False)
    accepted = await start_claude_goal_command(session_id, condition)
    # Bind pending display state only after actual admission. A competing
    # request or an older run's cleanup cannot clear the winner's operation.
    async with RUNS.active_lock:
        if str((RUNS.active.get(session_id) or {}).get("run_id") or "") == accepted.get("run_id"):
            CLAUDE_GOAL_PENDING[session_id] = {
                "run_id": accepted["run_id"],
                "previous_set_at": (previous or {}).get("set_at"),
            }
    return await claude_runtime_snapshot(session_id)


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


def turn_receipt_path(session_id: str, request_id: str) -> Path:
    return events_path(session_id).parent / "turn-receipts" / f"{request_id}.json"


def write_turn_receipt(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix(".tmp")
    with temporary.open("w") as stream:
        json.dump(value, stream, separators=(",", ":"))
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)
    descriptor = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def recover_turn_receipt(session_id: str, value: dict) -> dict | None:
    path = events_path(session_id)
    stamp = history_file_stamp(path)
    before = value.get("journal")
    offset = before[2] if before else 0
    if stamp is None:
        if before is not None:
            raise ValueError("send receipt journal disappeared")
        return None
    if before and (stamp[:2] != tuple(before[:2]) or stamp[2] < offset):
        raise ValueError("send receipt journal was replaced")
    # An ambiguous old receipt must not cause an unbounded scan or a resend.
    if stamp[2] - offset > 32 * 1024 * 1024:
        raise ValueError("send receipt recovery requires a larger journal scan")
    with path.open("rb") as stream:
        stream.seek(offset)
        while stream.tell() < stamp[2]:
            line = stream.readline(stamp[2] - stream.tell())
            if not line.endswith(b"\n"):
                raise ValueError("send receipt journal has an incomplete record")
            if b'"client_request_id"' not in line:
                continue
            event = json.loads(line)
            if event.get("client_request_id") != value["request_id"]:
                continue
            if event.get("type") == "turn_started":
                return {"run_id": event["run_id"], "queued": False}
            if event.get("type") == "turn_queued":
                return {"queued_id": event["queued_id"], "queued": True,
                        "position": event.get("position", 1)}
    return None


async def execute_receipted_turn(session_id: str, req: TurnRequest, fingerprint: str) -> dict:
    path = turn_receipt_path(session_id, req.client_request_id)
    try:
        value = await asyncio.to_thread(lambda: json.loads(path.read_text()))
    except FileNotFoundError:
        value = {"request_id": req.client_request_id, "fingerprint": fingerprint,
                 "journal": history_file_stamp(events_path(session_id)), "status": "pending"}
        await asyncio.to_thread(write_turn_receipt, path, value)
    else:
        if value.get("fingerprint") != fingerprint:
            raise HTTPException(status_code=409, detail="request ID was already used for a different message")
        if value.get("status") == "accepted":
            return {**value["response"], "session": public_session(STORE.sessions[session_id])}
        try:
            recovered = await asyncio.to_thread(recover_turn_receipt, session_id, value)
        except (ValueError, OSError) as exc:
            raise HTTPException(status_code=409, detail="message acceptance is uncertain; retain this request ID") from exc
        if recovered is not None:
            recovered["session"] = public_session(STORE.sessions[session_id])
            value.update(status="accepted", response=recovered)
            await asyncio.to_thread(write_turn_receipt, path, value)
            return recovered
    response = await post_turn_without_receipt(session_id, req)
    value.update(status="accepted", response=response)
    await asyncio.to_thread(write_turn_receipt, path, value)
    return response


async def post_turn_with_receipt(session_id: str, req: TurnRequest) -> Any:
    if session_id not in STORE.sessions or session_id in DELETED_SESSION_TOMBSTONES:
        raise HTTPException(status_code=404, detail="session not found")
    fingerprint = hashlib.sha256(json.dumps(req.model_dump(mode="json"), sort_keys=True,
                                           separators=(",", ":")).encode()).hexdigest()
    key = (session_id, req.client_request_id)
    existing = TURN_RECEIPT_TASKS.get(key)
    if existing is not None and existing[0] != fingerprint:
        raise HTTPException(status_code=409, detail="request ID was already used for a different message")
    if existing is None:
        task = asyncio.create_task(execute_receipted_turn(session_id, req, fingerprint))
        TURN_RECEIPT_TASKS[key] = (fingerprint, task)
        def completed(done):
            TURN_RECEIPT_TASKS.pop(key, None)
            if not done.cancelled():
                done.exception()
        task.add_done_callback(completed)
    else:
        task = existing[1]
    try:
        return await asyncio.wait_for(asyncio.shield(task), timeout=10)
    except asyncio.TimeoutError:
        return JSONResponse(status_code=202, content={"request_id": req.client_request_id,
                            "acceptance": "pending", "retry_same_request": True})


async def post_turn_without_receipt(session_id: str, req: TurnRequest) -> dict[str, Any]:
    return await start_turn(session_id, req)
