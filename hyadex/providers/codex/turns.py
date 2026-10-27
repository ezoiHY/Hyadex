"""Running Codex turns: the app-server and exec runners, native turn consumption, goal and compaction controls."""
from __future__ import annotations

import asyncio
from collections import deque
from contextlib import suppress
import json
import logging.handlers
import os
from pathlib import Path
import re
import secrets
import time
from typing import Any
import uuid

from fastapi import HTTPException

from hyadex.core.config import (
    BACKEND_CODEX,
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
    CODEX_NONINTERACTIVE_APPROVAL_POLICY,
    CODEX_RESUME_ACTIVITY_TIMEOUT_SECONDS,
    CODEX_SANDBOX_POLICY_TYPES,
    CODEX_TRANSPORT,
    CODEX_TRANSPORT_APP_SERVER,
    CODEX_TRANSPORT_AUTO,
    CODEX_TRANSPORT_EXEC,
    DEFAULT_CWD,
    IDLE_KILL_SECONDS,
    IDLE_WARN_SECONDS,
    PROCESS_STREAM_LIMIT,
)
from hyadex.core.event_log import reasoning_summary_stream_snapshot
from hyadex.core.paths import events_path, existing_cwd, now_iso
from hyadex.core.processes import process_group_for_pid, terminate_process_tree
from hyadex.core.queue import (
    queued_turn_run_metadata,
    request_fields_set,
    should_schedule_queue_after_finish,
)
from hyadex.core.artifacts import (
    collect_manifest,
    collect_recent_leftover_manifests,
    watch_manifest_artifacts,
)
from hyadex.core.history_sync import append_durable_event_batch, fence_native_steer_delivery
from hyadex.core.steering import mark_native_steer_accepted, turn_steering_lineage
from hyadex.core.queue_admission import wait_for_queue_recovery_admission
from hyadex.core.runs import RUNS
from hyadex.core.server_logging import concise_error_message
from hyadex.core.sessions import (
    append_event,
    event_delivery_lock,
    remember_codex_native_compaction_terminal,
    revoke_provider_authority,
    session_lifecycle_lock,
    STORE,
)
from hyadex.core.session_settings import codex_goal_time_budget_remaining, codex_runtime_settings
from hyadex.core.restart_status import managed_server_restart_blocks_work
from hyadex.core.event_seq import last_event_seq_from_file
from hyadex.core.session_codex import (
    codex_output_text,
    CODEX_SUBAGENT_INDEX_LOCK,
    CODEX_SUBAGENT_LIVE_GENERATIONS,
    CODEX_SUBAGENT_LIVE_MANAGERS,
    CODEX_SUBAGENT_SESSION_INDEX,
    CODEX_SUBAGENT_STATE,
)
from hyadex.core.state import (
    DELETED_SESSION_TOMBSTONES,
    DELETING_SESSIONS,
    EVENT_SEQ_CACHE,
    LIVE_STDOUT_MAX_LINES,
    REASONING_SUMMARY_STREAM_LAST_SENT,
    REASONING_SUMMARY_STREAM_PENDING,
    REASONING_SUMMARY_STREAMS,
)
from hyadex.core.turns import (
    append_active_stdout,
    append_turn_finished_event,
    bind_active_turn,
    clear_active_process,
    finalize_owned_turn_finished,
    reconcile_provider_task_exit,
    release_turn_slot,
    schedule_next_queued_turn,
)
from hyadex.core.util import clean_assistant_text, compact_memory_text
from hyadex.core.wiring import WIRING
from hyadex.features.files import capture_git_baseline, publish_turn_code_diff, run_event_metadata
from hyadex.features.side_questions import codex_app_server_service_tier
from hyadex.features.usage import HUB, join_task_despite_caller_cancellation
from hyadex.providers.authority import agent_runner_env, NativeSteerHandoffError
from hyadex.providers.claude.history import session_provider_id
from hyadex.providers.claude.runtime import mark_provider_turn_ready, tool_changed_paths
from hyadex.providers.codex.app_server import (
    CodexAppServerDisconnected,
    CodexAppServerError,
    CodexAppServerManager,
    CodexAppServerProtocolError,
    CodexAppServerRequestError,
    CodexAppServerSubscriptionClosed,
)
from hyadex.providers.codex.commands import (
    acquire_codex_control_thread,
    codex_app_server_manager,
    codex_provider_command_turn_input,
)
from hyadex.providers.codex.exec_runner import run_codex_exec
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
from hyadex.providers.codex.native_turns import consume_codex_native_turn
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
from hyadex.providers.codex.reasoning_stream import (
    clear_reasoning_summary_stream,
    finish_reasoning_summary_stream,
    persist_reasoning_summary,
    reasoning_summary_stream_item,
    update_reasoning_summary_stream,
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
    turn_start_blocker,
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
    build_user_provider_prompt,
    issue_native_steer_provider_authority,
    persist_run_provider_session,
    provider_command_prompt_matches,
    record_runtime_failure,
    record_runtime_success,
    redacted_provider_argv,
    standalone_provider_session,
    turn_slot_is_owned,
)
from hyadex.providers.runtime import validate_provider_runtime_env
from hyadex.providers.subagents import (
    codex_child_status_from_thread,
    codex_collaboration_states,
    codex_collaboration_thread_ids,
    codex_subagent_thread_identity,
    normalize_subagent_status,
    schedule_codex_subagent_finalization,
)
from provider_commands import ProviderCommandRecord

logger = logging.getLogger("hyadex-server")


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


def managed_server_restart_is_planned() -> bool:
    return managed_server_restart_blocks_work()


# The turn engine starts Codex turns through WIRING (it sits below this module).
WIRING.run_codex = run_codex
