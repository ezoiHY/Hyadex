"""Turn lifecycle entry points and the active-run record: binding, output, finishing."""
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

from hyadex.providers.claude.background_reconciliation import (
    pending_task_reconciliations,
    reconciliation_envelope,
    SESSION_FIELD as CLAUDE_BACKGROUND_SESSION_FIELD,
)
from hyadex.core.artifacts import (
    collect_manifest,
    collect_recent_leftover_manifests,
    watch_manifest_artifacts,
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
from hyadex.core.event_log import client_safe_event, reasoning_summary_stream_snapshot
from hyadex.core.event_seq import last_event_seq_from_file, timeline_index_session_lock
from hyadex.core.history_sync import (
    append_durable_event,
    append_durable_event_batch,
    fence_native_steer_delivery,
)
from hyadex.core.paths import codex_manifest_path, events_path, existing_cwd, now_iso
from hyadex.core.processes import process_group_for_pid, terminate_process_tree
from hyadex.core.queue import (
    queued_turn_run_metadata,
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
from hyadex.core.turn_start import (
    _recover_queued_turns_for_sessions,
    _start_turn_locked,
    reconcile_idle_queue_session,
    release_turn_slot,
    schedule_next_queued_turn,
)
from hyadex.core.restart_status import managed_server_restart_blocks_work
from hyadex.core.runs import RUNS
from hyadex.core.server import SERVER
from hyadex.core.server_logging import concise_error_message
from hyadex.core.session_codex import (
    codex_output_text,
    CODEX_SUBAGENT_INDEX_LOCK,
    CODEX_SUBAGENT_LIVE_GENERATIONS,
    CODEX_SUBAGENT_LIVE_MANAGERS,
    CODEX_SUBAGENT_SESSION_INDEX,
    CODEX_SUBAGENT_STATE,
)
from hyadex.core.session_files import event_files_belong_to_session
from hyadex.core.session_settings import (
    codex_goal_time_budget_remaining,
    codex_runtime_settings,
    effective_claude_permission_mode,
    effective_provider_jobs_access,
    preview_session_runtime_update,
    retired_session_runtime_message,
)
from hyadex.core.lifecycle_admission import server_lifecycle_admission_blocker
from hyadex.core.sessions import (
    append_event,
    CLAUDE_STOP_FENCE_SESSIONS,
    ensure_session_not_deleting,
    event_delivery_lock,
    event_output_text,
    mark_provider_context_usage_unavailable,
    public_session,
    refresh_native_session_title,
    register_session_task,
    remember_codex_native_compaction_terminal,
    revoke_provider_authority,
    revoke_terminal_run_authority,
    schedule_generated_session_title,
    session_lifecycle_lock,
    STORE,
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
from hyadex.core.steering import (
    force_send_task_owner,
    mark_native_steer_accepted,
    turn_steering_lineage,
)
from hyadex.core.timeline_index import (
    _build_timeline_index_locked,
    evict_timeline_index_cache_locked,
    fork_internal_run_ids,
    is_fork_internal_event,
    scheduled_job_occurrence_key,
    timeline_index_cached_entry,
)
from hyadex.core.turn_receipts import start_turn
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
from hyadex.features.job_context import VALID_JOB_CONTEXT_MODES
from hyadex.providers.authority import (
    agent_runner_env,
    issue_provider_authority,
    NativeSteerHandoffError,
    provider_authority_runtime_env,
)
from hyadex.providers.claude.background import stop_idle_claude_background_subagents_bounded
from hyadex.providers.claude.background_state import (
    acknowledge_claude_background_reconciliation,
    persist_claude_background_task_receipts,
)
from hyadex.providers.claude.catalog import build_claude_cmd
from hyadex.providers.claude.context_usage import (
    claude_provider_id_for_session,
    sample_claude_context_usage,
)
from hyadex.providers.claude.goals import (
    CLAUDE_GOAL_PENDING,
    ClaudeGoalRequest,
    refresh_claude_goal,
    require_claude_goal_session,
)
from hyadex.providers.claude.history import session_provider_id
from hyadex.providers.claude.interactions import (
    cancel_claude_interactions,
    claude_run_has_pending_interaction,
)
from hyadex.providers.claude.projection import (
    claude_sdk_result_details,
    mark_provider_turn_ready,
    project_claude_sdk_message,
    tool_changed_paths,
)
from hyadex.providers.claude.runtime import (
    claude_empty_turn_failure_message,
    claude_result_error,
    claude_runtime_snapshot,
    claude_sdk_manager,
    CLAUDE_STOP_FENCE_RETRY_DELAYS_SECONDS,
    CLAUDE_STOP_FENCE_RETRY_TASKS,
    claude_supports_no_session_persistence,
    interrupt_claude_sdk_run_bounded,
    is_expected_claude_interruption_result,
    mark_claude_sdk_turn_ready_after_ack,
    resolve_claude_resume_provider,
    stop_cleanup_in_progress,
    TurnRequest,
)
from hyadex.providers.claude.sdk import (
    ClaudeSDKQueryError,
    ClaudeSDKSupervisorClosed,
    ClaudeSDKUnavailable,
)
from hyadex.providers.claude.sdk_manager import ClaudeSDKSupervisorManager
from hyadex.providers.claude.subagents import (
    claude_sdk_field,
    claude_sdk_type,
    discard_unused_claude_stop_fence_attempt_lock,
    evict_claude_sdk_chat,
    normalize_subagent_status,
)
from hyadex.providers.codex.app_server import (
    CodexAppServerDisconnected,
    CodexAppServerError,
    CodexAppServerProtocolError,
    CodexAppServerRequestError,
    CodexAppServerSubscriptionClosed,
)
from hyadex.providers.codex.app_server_manager import CodexAppServerManager
from hyadex.providers.codex.commands import (
    acquire_codex_control_thread,
    build_claude_sdk_options,
    codex_app_server_manager,
    codex_provider_command_turn_input,
    discover_session_provider_commands,
    prepare_codex_login_turn,
    resolve_provider_command_selection,
)
from hyadex.providers.codex.goals import (
    apply_codex_goal_time_budget_limit,
    build_codex_cmd,
    CodexGoalRequest,
    commit_codex_goal_steer,
    quarantine_codex_goal_thread,
    rollover_codex_provider_session,
    send_codex_goal_steer,
    stop_codex_goal_resume,
    validate_codex_goal_resume_budget,
)
from hyadex.providers.codex.history import find_codex_history
from hyadex.providers.codex.interactions import effective_codex_approval_policy
from hyadex.providers.codex.managers import (
    acquire_codex_run_thread,
    claim_codex_control_terminal_publication,
    codex_control_http_error,
    CODEX_CONTROL_TERMINAL_FENCES,
    register_codex_native_action,
    release_codex_control_slot,
    release_codex_interactive_control_lease,
    unpin_codex_app_server_thread,
)
from hyadex.providers.codex.notifications import (
    codex_app_server_reasoning_plaintext,
    codex_app_server_reasoning_summary,
    codex_app_server_tool,
    codex_app_server_tool_output,
    codex_call_tool,
    codex_exec_agent_message,
    codex_reasoning_text,
    CodexExecMessageBuffer,
    current_codex_token_usage_snapshot,
    project_codex_notification,
    record_codex_token_usage,
    retain_codex_goal_run_owner,
)
from hyadex.providers.codex.runtime import (
    archive_rejected_codex_session,
    codex_app_server_changed_paths,
    codex_exec_raw_event_text,
    codex_native_archive_rejection,
    codex_output_exit_code,
    codex_result_error,
    codex_turn_matches_client_user_message,
    is_codex_app_server_retry_notice,
    is_silent_codex_completion,
    mark_codex_exec_context_usage_unavailable,
    migrate_retired_codex_selection,
    turn_start_blocker,
)
from hyadex.providers.codex.subagents import (
    codex_child_status_from_thread,
    codex_collaboration_states,
    codex_collaboration_thread_ids,
    codex_subagent_thread_identity,
    schedule_codex_subagent_finalization,
)
from hyadex.providers.codex.threads import (
    codex_compaction_error_message,
    codex_compaction_retry_mode,
    is_codex_compaction_failure,
    is_codex_resume_failure,
    should_recover_codex_resume,
)
from hyadex.providers.codex.tools import codex_provider_mcp_run_proof
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
from hyadex.providers.commands import ProviderCommandRecord

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


def server_lifecycle_active_session_ids_locked() -> list[str]:
    """Return chat-scoped work that must drain before worker retirement."""

    return sorted(
        RUNS.busy
        | RUNS.maintenance
        | explicit_stop_session_ids()
    )


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


