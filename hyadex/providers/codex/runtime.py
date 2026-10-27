"""Codex provider runtime: app-server managers, threads, goals, notifications and forks."""
from __future__ import annotations

import asyncio
from collections import deque, OrderedDict
from contextlib import suppress
import hashlib
import hmac
import inspect
import json
import logging.handlers
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import time
from typing import Any, Literal
import uuid
import weakref

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

import codex_auth
from hyadex.core.config import (
    BACKEND_CLAUDE,
    BACKEND_CODEX,
    CLAUDE_SDK_INTERACTIVE_CLIENT_CAPABILITY,
    CLAUDE_TRANSPORT,
    CLAUDE_TRANSPORT_PRINT,
    CODEX_APP_SERVER_JSONL_LIMIT_BYTES,
    CODEX_APP_SERVER_LIFECYCLE_TIMEOUT_SECONDS,
    CODEX_APP_SERVER_MAX_LOADED_THREADS,
    CODEX_APP_SERVER_NOTIFICATION_QUEUE_LIMIT,
    CODEX_APP_SERVER_TIMEOUT_SECONDS,
    CODEX_APPROVAL_POLICIES,
    CODEX_APPROVAL_REVIEWERS,
    CODEX_BIN,
    CODEX_CONTEXT_WINDOW_RESERVE_TOKENS,
    CODEX_DEFAULT_APPROVAL_POLICY,
    CODEX_DEFAULT_APPROVALS_REVIEWER,
    CODEX_DEFAULT_EFFORT,
    CODEX_DEFAULT_MODEL,
    CODEX_DEFAULT_PERMISSION_PROFILE,
    CODEX_DEFAULT_SANDBOX_MODE,
    CODEX_EXEC_RAW_EVENT_MAX_CHARS,
    CODEX_GOAL_CONTROL_TIMEOUT_SECONDS,
    CODEX_GOAL_STATUSES,
    CODEX_GOALS_DEFAULT_ENABLED,
    CODEX_GOALS_ENABLED,
    CODEX_INTERACTION_METHODS,
    CODEX_INTERACTIVE_CLIENT_CAPABILITY,
    CODEX_NONINTERACTIVE_APPROVAL_POLICY,
    CODEX_PROVIDER_MCP_HEADER_NAME,
    CODEX_PROVIDER_MCP_HEADER_SECRET,
    CODEX_PROVIDER_MCP_NAME,
    CODEX_PROVIDER_MCP_PATH,
    CODEX_PROVIDER_MCP_PROOF_KEY,
    CODEX_SANDBOX_MODES,
    CODEX_SANDBOX_POLICY_TYPES,
    CODEX_SESSION_CLEANUP_TIMEOUT_SECONDS,
    CODEX_TRANSPORT,
    CODEX_TRANSPORT_APP_SERVER,
    CODEX_TRANSPORT_EXEC,
    DEFAULT_BACKEND,
    DEFAULT_CWD,
    FORK_INTERNAL_PURPOSES,
    MAX_ACTIVE_AGENT_RUNS,
    MAX_CODEX_APPROVAL_ITEM_CACHE,
    MAX_CODEX_GOAL_CHARS,
    MAX_CODEX_PENDING_INTERACTIONS,
    MAX_FORK_MEMORY_CHARS,
    MIN_START_AVAILABLE_MEM_MB,
    PROCESS_STREAM_LIMIT,
    SERVER_INSTANCE_ID,
    SERVER_ROOT,
    SERVER_VERSION,
    STATE_DIR,
)
from hyadex.core.event_log import project_provider_history_event_for_egress
from hyadex.core.timeline_index import is_native_goal_steer_event
from hyadex.core.host import host_pressure_snapshot
from hyadex.core.paths import codex_manifest_path, existing_cwd, now_iso
from hyadex.core.processes import terminate_process_tree
from hyadex.core.queue import request_fields_set
from hyadex.core.history_sync import append_durable_event_batch, fence_native_steer_delivery
from hyadex.core.steering import codex_goal_steer_selection_is_plain
from hyadex.core.runs import RUNS
from hyadex.core.server import SERVER
from hyadex.core.server_logging import concise_error_message
from hyadex.core.sessions import (
    append_event,
    ensure_session_not_deleting,
    mark_provider_context_usage_unavailable,
    native_session_available,
    PROVIDER_AUTHORITIES,
    PROVIDER_AUTHORITY_LOCK,
    register_session_task,
    remember_codex_native_compaction_terminal,
    session_lifecycle_lock,
    session_prompt_addendum,
    session_system_prompt,
    STORE,
    write_abandoned_fork_thread_ids,
)
from hyadex.core.lifecycle_admission import server_lifecycle_admission_blocker
from hyadex.core.session_settings import (
    clamp_codex_runtime_effort,
    codex_available_model,
    codex_goal_time_budget_is_exhausted,
    codex_runtime_settings,
    codex_user_config_defaults,
    codex_user_config_path,
    effective_claude_permission_mode,
    load_codex_user_config,
    RUNTIME_DIAGNOSTICS,
    RUNTIME_DIAGNOSTICS_LOCK,
    UpdateSessionRequest,
)
from hyadex.core.session_files import iter_session_events
from hyadex.core.session_codex import (
    ABANDONED_FORK_PROVIDER_THREADS,
    codex_app_server_env,
    codex_app_server_managers,
    codex_compaction_count,
    CODEX_RETIRED_APP_SERVER_MANAGERS,
    CODEX_SESSION_APP_SERVER_MANAGERS,
    CODEX_SUBAGENT_INDEX_LOCK,
    CODEX_SUBAGENT_LIVE_GENERATIONS,
    CODEX_SUBAGENT_LIVE_MANAGERS,
    CODEX_SUBAGENT_SESSION_INDEX,
    CODEX_SUBAGENT_STATE,
    CODEX_THREAD_SESSION_INDEX,
    existing_codex_app_server_manager,
    native_codex_compaction_id,
    native_codex_compaction_terminal_aliases,
    provider_helper_server_origin,
    session_codex_thread_id,
)
from hyadex.core.state import DELETED_SESSION_TOMBSTONES, DELETING_SESSIONS
from hyadex.core.util import (
    clean_assistant_text,
    compact_memory_text,
    low_available_memory_message,
    runner_env,
    SkillSelection,
    TransientAdmissionWait,
)
from hyadex.features.side_questions import (
    codex_app_server_service_tier,
    CODEX_GOALS_RECONFIGURING,
    flatten_codex_config_overrides,
    require_side_question_admission,
)
from hyadex.features.terminal import _update_session
from hyadex.features.titles import active_generated_title_work_labels
from hyadex.features.usage import (
    bounded_codex_interaction_value,
    broadcast_provider_runtime_changed,
    broadcast_provider_usage_changed,
    clear_provider_context_usage_locked,
    context_usage_runtime_fields,
    HUB,
    join_task_despite_caller_cancellation,
    next_provider_context_usage_generation,
    provider_context_usage_signal,
    PROVIDER_USAGE,
)
from hyadex.providers.authority import (
    agent_runner_env,
    NativeSteerHandoffError,
    provider_capability_is_attached_to_live_run,
    provider_tool_active_matches,
    provider_tool_authority_path,
    provider_tool_call_is_read_only,
    ProviderToolError,
    redact_provider_tool_output,
    resolve_provider_tool_arguments,
    terminal_session_name,
    validate_provider_tool_input,
)
from hyadex.providers.claude.history import (
    native_session_title,
    session_provider_id,
    text_from_content,
)
from hyadex.providers.claude.runtime import (
    claude_event_file_fingerprints,
    claude_sdk_configuration_key,
    claude_sdk_manager,
    ensure_provider_manager_factory_admission,
    resolve_claude_resume_provider,
)
from hyadex.providers.claude.interactions import (
    CLAUDE_INTERACTION_HANDLER_TASKS,
    handle_claude_tool_permission,
)
from hyadex.providers.claude.catalog import claude_sdk_cli_path, runtime_command
from hyadex.providers.claude.background_state import (
    claude_background_work_labels_from_events,
    loaded_claude_background_session_state,
    PROVIDER_WORK_LABEL_LIMIT,
)
from hyadex.providers.claude.sdk import (
    claude_background_tracking_hooks,
    CLAUDE_NON_DURABLE_SCHEDULER_TOOLS,
    CLAUDE_PROVIDER_MCP_SERVER_NAME,
    CLAUDE_PROVIDER_MCP_TOOL_NAME,
    ClaudeSDKConfigurationConflict,
    ClaudeSDKGenerationChanged,
    ClaudeSDKRunActive,
    ClaudeSDKSupervisorError,
    create_claude_agent_options,
    create_claude_sdk_mcp_server,
)
from hyadex.providers.codex.app_server import (
    CodexAppServerError,
    CodexAppServerProtocolError,
    CodexAppServerRequestError,
    decline_server_request,
)
from hyadex.providers.codex.app_server_manager import CodexAppServerManager
from hyadex.providers.codex.commands import SIDE_QUESTIONS
from hyadex.providers.codex.config import (
    codex_effective_thread_config,
    codex_session_config_overrides,
    sanitize_codex_thread_config,
)
from hyadex.providers.codex.forks import (
    build_fork_memory,
    codex_raw_developer_message,
    CodexForkCleanupError,
    forget_abandoned_fork_provider_thread,
    persist_abandoned_fork_provider_thread,
)
from hyadex.providers.codex.goals import (
    CODEX_GOAL_SYNC_GENERATIONS,
    codex_native_goal_owner_matches,
    CODEX_QUARANTINED_GOAL_THREADS,
    persist_reconciled_codex_goal,
    quarantine_codex_goal_thread,
    reconcile_codex_thread_goal,
    reset_codex_ephemeral_runtime_metadata,
)
from hyadex.providers.codex.history import (
    codex_hygiene_int_setting,
    CODEX_ROLLOUT_OVERSIZED_BYTES,
    codex_rollout_stats,
    CODEX_ROLLOUT_WARN_BYTES,
)
from hyadex.providers.codex.interactions import (
    cache_codex_approval_item,
    CODEX_INTERACTION_HANDLER_TASKS,
    CODEX_PENDING_INTERACTIONS,
    CODEX_PENDING_INTERACTIONS_LOCK,
    effective_codex_approval_policy,
    handle_codex_server_request,
    public_codex_interaction,
)
from hyadex.providers.codex.managers import (
    acquire_codex_interactive_control_lease,
    bind_codex_control_reservation,
    cancel_codex_native_actions,
    CODEX_APP_SERVER_EVICTING_THREADS,
    CODEX_APP_SERVER_MANAGER_LOCK,
    CODEX_APP_SERVER_THREAD_PIN_COUNTS,
    codex_binary_identity,
    CODEX_BINARY_PROBE_LOCK,
    codex_control_http_error,
    CODEX_INTERACTIVE_CONTROL_THREAD_COUNTS,
    CODEX_INTERACTIVE_CONTROL_THREADS,
    CODEX_LOGIN_PROBE_LOCK,
    codex_manager_has_callers,
    codex_manager_work_belongs_to_session,
    CODEX_NATIVE_ACTION_TASKS,
    ensure_codex_app_server_thread,
    evict_codex_app_server_thread,
    existing_codex_app_server_manager_for_thread,
    expire_codex_login_diagnostic,
    pin_codex_app_server_thread,
    prepare_codex_app_server_process,
    register_codex_app_server_child,
    register_codex_native_action,
    release_codex_control_slot,
    release_codex_interactive_control_lease,
    touch_codex_app_server_thread,
    unpin_codex_app_server_thread,
    unregister_codex_app_server_child,
)
from hyadex.providers.codex.notifications import (
    codex_manager_owns_notification,
    project_codex_notification,
    project_codex_usage_notification,
)
from hyadex.providers.codex.threads import (
    CODEX_APP_SERVER_INVALIDATED_THREADS,
    CODEX_APP_SERVER_PINNED_THREADS,
    CODEX_APP_SERVER_THREAD_LRU,
    CODEX_APP_SERVER_THREAD_LRU_LOCK,
    codex_native_compaction_was_terminal,
    codex_session_id_for_thread,
    codex_thread_has_local_run_owner,
    codex_thread_hygiene,
    codex_thread_hygiene_public,
    codex_thread_instruction_hash,
    codex_thread_instructions,
    codex_thread_status_message,
    invalidated_codex_thread_error,
    schedule_codex_thread_hygiene_check,
)
from hyadex.providers.codex.tools import (
    codex_thread_params,
    execute_provider_tool,
    execute_provider_tool_once,
    start_standalone_codex_app_server_thread,
)
from hyadex.providers.instructions import (
    CODEX_PROMPT_PRELUDE,
    CODEX_THREAD_POLICY_VERSION,
    PROVIDER_TOOL_DESCRIPTION,
    PROVIDER_TOOL_INPUT_SCHEMA,
    PROVIDER_TOOL_MAX_CONCURRENT_PER_CLAUDE_CHAT,
    PROVIDER_TOOL_MAX_OUTPUT_BYTES,
    PROVIDER_TOOL_REPLAY,
    PROVIDER_TOOL_REPLAY_LIMIT,
    PROVIDER_TOOL_REPLAY_LOCK,
    PROVIDER_TOOL_REPLAY_TOMBSTONE_LIMIT,
    PROVIDER_TOOL_REPLAY_TOMBSTONES,
    PROVIDER_TOOL_TIMEOUT_SECONDS,
)
from hyadex.providers.registry import (
    build_user_provider_prompt,
    persist_run_provider_session,
    prepare_provider_background_work_snapshot,
    PROVIDER_COMMAND_EMPTY_FALLBACK_SECRET,
    provider_command_prompt_matches,
    provider_command_selector_secret,
    provider_command_snapshot,
    provider_command_support,
    ProviderCommandSelectionInvalid,
    ProviderCommandSelectionUnavailable,
    register_provider_child,
    request_client_is_provider_helper_local,
    runtime_diagnostic_payload,
    safe_runtime_version,
    store_runtime_diagnostic,
    unregister_provider_child,
)
from hyadex.providers.runtime import (
    is_scrubbed_provider_runtime_env_name,
    PROVIDER_SECRET_ENV_NAMES,
    validate_provider_runtime_env,
)
from hyadex.providers.state import PROVIDERS
from hyadex.providers.codex.subagents import (
    codex_child_status_from_thread,
    codex_collaboration_thread_ids,
    CODEX_GOALS_CONFIG_LOCK,
    codex_session_has_live_subagents,
    codex_subagent_has_live_owner,
    codex_subagent_native_turn_scopes,
    codex_subagent_thread_identity,
    emit_codex_subagent_state,
    project_codex_subagent_item,
    record_codex_subagent_limit_application,
)
from hyadex.providers.claude.subagents import normalize_subagent_status
from provider_commands import (
    claude_provider_command_inventory,
    codex_provider_command_inventory,
    empty_provider_command_inventory,
    MAX_PROVIDER_COMMANDS,
    ProviderCommandInventory,
    ProviderCommandRecord,
)
import side_questions

logger = logging.getLogger("hyadex-server")


def queued_codex_runtime_matches_active(
    session_id: str,
    selected: dict[str, Any],
    active: dict[str, Any],
) -> bool:
    sess = dict(STORE.sessions.get(session_id) or {})
    if selected.get("model") is not None:
        sess["model"] = selected.get("model")
    if selected.get("effort") is not None:
        sess["effort"] = selected.get("effort")
    model, effort, service_tier = codex_runtime_settings(sess)
    return (
        model == str(active.get("provider_model") or "")
        and effort == str(active.get("provider_effort") or "")
        and service_tier == str(active.get("provider_service_tier") or "")
    )


def is_silent_codex_completion(
    *,
    stopped: bool,
    stream_error: str | None,
    idle_killed: bool,
    returncode: int | None,
    produced_activity: bool,
) -> bool:
    return (
        not stopped
        and not stream_error
        and not idle_killed
        and returncode in (0, None)
        and not produced_activity
    )


async def turn_start_blocker(*, ignore_session_id: str | None = None) -> str | None:
    async with RUNS.active_lock:
        lifecycle_blocker = server_lifecycle_admission_blocker()
        if lifecycle_blocker:
            return lifecycle_blocker
        active_count = len(RUNS.busy - ({ignore_session_id} if ignore_session_id else set()))

    if MAX_ACTIVE_AGENT_RUNS > 0 and active_count >= MAX_ACTIVE_AGENT_RUNS:
        return f"server already has {active_count} active agent run(s)"

    pressure = host_pressure_snapshot()
    available_mem_mb = pressure.get("available_mem_mb")
    if (
        isinstance(available_mem_mb, int)
        and MIN_START_AVAILABLE_MEM_MB > 0
        and available_mem_mb < MIN_START_AVAILABLE_MEM_MB
    ):
        return low_available_memory_message(
            available_mem_mb, MIN_START_AVAILABLE_MEM_MB, action="start an agent turn",
        )

    return None


async def mark_codex_exec_context_usage_unavailable(session_id: str) -> None:
    """Invalidate native context telemetry before any codex exec turn."""

    await mark_provider_context_usage_unavailable(session_id, BACKEND_CODEX)


async def migrate_retired_codex_selection(
    session_id: str, sess: dict[str, Any], *, standalone: bool = False,
    previous_model: str | None = None,
) -> dict[str, Any]:
    """Migrate future work under the existing admission lock; retain history.

    No native transcript edits, forks, auth changes, or retries. Native
    compaction/recovery remains Codex-owned and can still fail independently.
    """
    if sess.get("backend") != BACKEND_CODEX:
        return sess
    configured_model, _effort, _tier = codex_user_config_defaults()
    previous = str(previous_model or sess.get("model") or configured_model or CODEX_DEFAULT_MODEL).strip()
    replacement = codex_available_model(previous)
    if previous == replacement:
        return sess
    patch = {"model": replacement, "effort": clamp_codex_runtime_effort(replacement, sess.get("effort") or _effort or CODEX_DEFAULT_EFFORT)}
    if standalone:
        migrated = {**sess, **patch}
    elif all(sess.get(key) == value for key, value in patch.items()):
        migrated = sess
    else:
        migrated = await STORE.update(session_id, patch)
    await append_event(session_id, "runtime_model_migrated", {
        "backend": BACKEND_CODEX, "previous_model": previous, "model": replacement,
        "standalone": standalone,
        "message": (f"{previous} is no longer available with ChatGPT sign-in. "
                    f"This {'scheduled run' if standalone else 'chat'} will use {replacement}. "
                    "Conversation history is unchanged."),
    })
    return migrated


def is_codex_reconnect_notice(message: str) -> bool:
    return bool(re.match(r"^Reconnecting\.\.\.\s+\d+/\d+\b", str(message or "").strip(), re.IGNORECASE))


def is_codex_app_server_retry_notice(params: dict[str, Any], message: str) -> bool:
    """Retry progress is not a terminal error; an explicit refusal wins."""
    will_retry = params.get("willRetry")
    return will_retry is True or (
        will_retry is not False and is_codex_reconnect_notice(message)
    )


def codex_result_error(event: dict[str, Any]) -> str | None:
    event_type = str(event.get("type") or "")
    if event_type == "error":
        message = concise_error_message(event.get("message") or event.get("error") or event)
        return None if is_codex_reconnect_notice(message) else message
    if event_type == "turn.failed":
        return concise_error_message(event.get("error") or event.get("message") or event)
    if event_type == "event_msg":
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        payload_type = str(payload.get("type") or "")
        if payload_type in {"error", "turn_failed"}:
            return concise_error_message(payload.get("message") or payload.get("error") or payload)
    return None


def codex_exec_raw_event_text(line: str, *, handled: bool) -> str:
    """Keep only bounded, unrecognized Codex exec packets for diagnostics."""
    if handled:
        return ""
    if len(line) <= CODEX_EXEC_RAW_EVENT_MAX_CHARS:
        return line
    omitted = len(line) - CODEX_EXEC_RAW_EVENT_MAX_CHARS
    return (
        line[:CODEX_EXEC_RAW_EVENT_MAX_CHARS]
        + f"\n[Hyadex omitted {omitted} characters from this raw event]"
    )


def codex_output_exit_code(text: str) -> int | None:
    for pattern in (r"Process exited with code (-?\d+)", r"Exit code:\s*(-?\d+)"):
        match = re.search(pattern, text)
        if match:
            with suppress(Exception):
                return int(match.group(1))
    return None


def codex_turn_matches_client_user_message(
    turn: dict[str, Any],
    client_user_message_id: str,
) -> bool:
    """Identify an app-server turn by the id assigned to its user item."""
    expected = str(client_user_message_id or "")
    if not expected:
        return False
    if str(turn.get("clientUserMessageId") or "") == expected:
        return True
    items = turn.get("items")
    if not isinstance(items, list):
        return False
    return any(
        isinstance(item, dict)
        and str(item.get("type") or "") == "userMessage"
        and (
            str(item.get("clientId") or "") == expected
            or str(item.get("id") or "") == expected
            or str(item.get("clientUserMessageId") or "") == expected
        )
        for item in items
    )


def codex_app_server_changed_paths(item: dict[str, Any]) -> set[str]:
    paths: set[str] = set()
    if str(item.get("type") or "") != "fileChange":
        return paths
    for change in item.get("changes") or []:
        if isinstance(change, dict):
            path = str(change.get("path") or change.get("filePath") or "").strip()
            if path:
                paths.add(path)
    return paths


def codex_native_archive_rejection(exc: BaseException, thread_id: str) -> bool:
    """Only the owning native resume RPC can report this state, not chat text."""
    return bool(
        thread_id and isinstance(exc, CodexAppServerRequestError)
        and exc.method == "thread/resume" and isinstance(exc.error, dict)
        and exc.error.get("message") == (
            f"session {thread_id} is archived. Run `codex unarchive {thread_id}` to unarchive it first."
        )
    )


async def archive_rejected_codex_session(session_id: str, thread_id: str, run_id: str) -> None:
    """Mirror a confirmed native archive through the normal local archive path.

    This never archives/unarchives/deletes the native thread. It retires local
    routes and jobs exactly like the user's Archive action, preserving history.
    Identity is stable while this run owns the chat; ignore stale/deleted runs.
    """
    current = STORE.sessions.get(session_id)
    active = RUNS.active.get(session_id) or {}
    if (not current or current.get("archived") or session_id in DELETING_SESSIONS
            or current.get("backend") != BACKEND_CODEX
            or str(session_provider_id(current) or "") != thread_id
            or active.get("run_id") != run_id):
        return
    await _update_session(session_id, UpdateSessionRequest(archived=True),
                          native_archive_owner=(thread_id, run_id))


def active_codex_work_labels() -> list[str]:
    labels: set[str] = set()
    for session_id in RUNS.busy:
        current = RUNS.current_turns.get(session_id) or {}
        active = RUNS.active.get(session_id) or {}
        session = STORE.sessions.get(session_id) or {}
        backend = str(
            active.get("backend")
            or current.get("backend")
            or session.get("backend")
            or DEFAULT_BACKEND
        ).strip().lower()
        if backend == BACKEND_CODEX:
            labels.add(f"active chat {session_id}")
    for session_id in RUNS.maintenance:
        session = STORE.sessions.get(session_id) or {}
        if str(session.get("backend") or DEFAULT_BACKEND).lower() == BACKEND_CODEX:
            labels.add(f"Codex maintenance for {session_id}")
    for (session_id, operation_id), task in CODEX_NATIVE_ACTION_TASKS.items():
        if not task.done():
            labels.add(f"Codex operation {operation_id} in {session_id}")
    for pending in CODEX_PENDING_INTERACTIONS.values():
        session_id = str(pending.get("session_id") or "unknown")
        labels.add(f"Codex prompt in {session_id}")
    with CODEX_SUBAGENT_INDEX_LOCK:
        codex_subagent_states = [
            (thread_id, dict(state))
            for thread_id, state in CODEX_SUBAGENT_STATE.items()
        ]
    for thread_id, state in codex_subagent_states:
        if codex_subagent_has_live_owner(thread_id, state):
            labels.add(f"Codex subagent {thread_id}")
    return sorted(labels)


def active_provider_background_work_labels() -> list[str]:
    """Return bounded provider-owned work that survives its parent turn.

    Lifecycle admission already checks active and queued parent turns. Codex
    can additionally own native operations and live child threads, while a
    loaded Claude SDK supervisor can retain background subagents after its
    parent ResultMessage. Only loaded Claude supervisors are considered: event
    history alone must never turn into a permanent lifecycle blocker after the
    provider process has been evicted or the server has restarted.
    """

    labels = sorted(set(
        active_codex_work_labels() + active_generated_title_work_labels()
        + SIDE_QUESTIONS.active_work_labels()
    ))[:PROVIDER_WORK_LABEL_LIMIT]
    remaining = PROVIDER_WORK_LABEL_LIMIT - len(labels)
    manager = PROVIDERS.claude_sdk_manager
    if remaining <= 0 or manager is None:
        return labels

    for session_id, session in tuple(STORE.sessions.items()):
        if remaining <= 0:
            break
        if (
            str(session.get("backend") or DEFAULT_BACKEND).strip().lower()
            != BACKEND_CLAUDE
            or session_id in RUNS.busy
            or session_id in RUNS.maintenance
        ):
            continue
        try:
            loaded = manager.is_loaded(session_id)
        except Exception:
            # Failure to inspect a live supervisor is not proof that it is safe
            # to terminate. Fail closed and require an explicit retry/update.
            labels.append(f"Claude provider state unknown in {session_id}")
            remaining -= 1
            continue
        if not loaded:
            continue
        claude_labels = claude_background_work_labels_from_events((session_id,))
        if claude_labels:
            labels.extend(claude_labels[:remaining])
            remaining -= 1
    return labels


def provider_background_work_labels_from_snapshot(
    snapshot: dict[str, Any],
) -> list[str]:
    """Validate a pre-lock fold using bounded process state and stat calls."""

    labels = active_codex_work_labels()
    codex_snapshot = snapshot.get("codex")
    if isinstance(codex_snapshot, dict):
        current_scopes = codex_subagent_native_turn_scopes()
        inspected_scopes = tuple(codex_snapshot.get("scopes") or ())
        codex_snapshot_is_current = (
            codex_snapshot.get("consistent") is True
            and len(current_scopes) == len(inspected_scopes)
            and all(manager is inspected.get("manager")
                    and generation == inspected.get("generation")
                    and candidates == tuple(inspected.get("candidates") or ())
                    for (manager, generation, candidates), inspected
                    in zip(current_scopes, inspected_scopes))
        )
        if codex_snapshot_is_current:
            terminal_labels = {
                f"Codex subagent {thread_id}"
                for thread_id in codex_snapshot.get("terminal_thread_ids") or ()
            }
            labels = [label for label in labels if label not in terminal_labels]
    labels = sorted(set(
        labels + active_generated_title_work_labels() + SIDE_QUESTIONS.active_work_labels()
    ))[:PROVIDER_WORK_LABEL_LIMIT]
    remaining = PROVIDER_WORK_LABEL_LIMIT - len(labels)
    if remaining <= 0:
        return labels

    manager = PROVIDERS.claude_sdk_manager
    session_ids, unknown_labels = loaded_claude_background_session_state(manager)
    fingerprints = claude_event_file_fingerprints(session_ids)
    snapshot_is_current = (
        snapshot.get("consistent") is True
        and manager is snapshot.get("manager")
        and session_ids == tuple(snapshot.get("session_ids") or ())
        and unknown_labels == tuple(snapshot.get("unknown_labels") or ())
        and fingerprints == tuple(snapshot.get("fingerprints") or ())
    )
    if not snapshot_is_current:
        snapshot["stale"] = True
        labels.append("Claude provider state changed during inspection")
        return labels[:PROVIDER_WORK_LABEL_LIMIT]

    labels.extend(unknown_labels[:remaining])
    remaining = PROVIDER_WORK_LABEL_LIMIT - len(labels)
    if remaining > 0:
        labels.extend(
            list(snapshot.get("claude_labels") or ())[:remaining]
        )
    return labels


async def active_provider_background_work_labels_async() -> list[str]:
    """Return bounded provider blockers without scanning on the event loop."""

    snapshot = await prepare_provider_background_work_snapshot()
    return provider_background_work_labels_from_snapshot(snapshot)


async def codex_runtime_snapshot(session_id: str) -> dict[str, Any]:
    session = STORE.sessions.get(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="session not found")
    is_codex = str(session.get("backend") or DEFAULT_BACKEND) == BACKEND_CODEX
    available = is_codex and CODEX_TRANSPORT != CODEX_TRANSPORT_EXEC
    thread_id = str(session_provider_id(session) or "")
    manager = existing_codex_app_server_manager(session)
    thread_loaded = bool(
        thread_id and manager is not None and manager.is_thread_loaded(thread_id)
    )
    async with CODEX_PENDING_INTERACTIONS_LOCK:
        pending = [
            public_codex_interaction(item)
            for item in CODEX_PENDING_INTERACTIONS.values()
            if item.get("session_id") == session_id
            and not item.get("responded")
        ]
    cached_status = session.get("codex_thread_status")
    async with RUNS.active_lock:
        active = session_id in RUNS.busy
    if active:
        status = (
            cached_status
            if isinstance(cached_status, dict)
            and cached_status.get("type") == "active"
            else {"type": "active", "activeFlags": []}
        )
    elif thread_loaded:
        status = (
            cached_status
            if isinstance(cached_status, dict)
            and cached_status.get("type") == "systemError"
            else {"type": "idle"}
        )
    else:
        status = {"type": "notLoaded"}
    thread_hygiene = codex_thread_hygiene_public(
        await codex_thread_hygiene(session)
    )
    return {
        "available": available,
        "transport": CODEX_TRANSPORT,
        "goals_enabled": CODEX_GOALS_ENABLED,
        "interactive_capability": CODEX_INTERACTIVE_CLIENT_CAPABILITY,
        "persisted_thread": bool(thread_id),
        "thread_loaded": thread_loaded,
        "status": status,
        "goal": session.get("codex_goal"),
        "time_budget_seconds": session.get("codex_goal_time_budget_seconds"),
        "time_budget_exhausted": codex_goal_time_budget_is_exhausted(session),
        "token_usage": session.get("codex_token_usage"),
        "token_usage_snapshot": session.get("codex_token_usage_snapshot"),
        **context_usage_runtime_fields(session, backend=BACKEND_CODEX),
        "pending_interactions": pending,
        "config_overrides": codex_session_config_overrides(session),
        "thread_hygiene": thread_hygiene,
        "rotated_threads": [
            dict(item)
            for item in (session.get("codex_rotated_threads") or [])
            if isinstance(item, dict)
        ],
        "policy": {
            "approval_policy": (
                session.get("codex_approval_policy")
                if session.get("codex_approval_policy")
                in CODEX_APPROVAL_POLICIES
                else CODEX_DEFAULT_APPROVAL_POLICY
            ),
            "sandbox_mode": (
                session.get("codex_sandbox_mode")
                if session.get("codex_sandbox_mode") in CODEX_SANDBOX_MODES
                else CODEX_DEFAULT_SANDBOX_MODE
            ),
            "permission_profile": (
                str(
                    session.get("codex_permission_profile")
                    or CODEX_DEFAULT_PERMISSION_PROFILE
                    or ""
                ).strip()
                or CODEX_DEFAULT_PERMISSION_PROFILE
            ),
            "approvals_reviewer": (
                session.get("codex_approvals_reviewer")
                if session.get("codex_approvals_reviewer")
                in CODEX_APPROVAL_REVIEWERS
                else CODEX_DEFAULT_APPROVALS_REVIEWER
            ),
        },
    }


