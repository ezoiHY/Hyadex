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
from hyadex.core.event_log import (
    host_pressure_snapshot,
    is_native_goal_steer_event,
    project_provider_history_event_for_egress,
)
from hyadex.core.paths import codex_manifest_path, existing_cwd, now_iso
from hyadex.core.processes import terminate_process_tree
from hyadex.core.queue import (
    append_durable_event_batch,
    codex_goal_steer_selection_is_plain,
    fence_native_steer_delivery,
    request_fields_set,
)
from hyadex.core.runs import RUNS
from hyadex.core.server import SERVER
from hyadex.core.server_logging import concise_error_message
from hyadex.core.sessions import (
    ABANDONED_FORK_PROVIDER_THREADS,
    append_event,
    clamp_codex_runtime_effort,
    codex_app_server_env,
    codex_app_server_managers,
    codex_available_model,
    codex_compaction_count,
    codex_goal_time_budget_is_exhausted,
    CODEX_RETIRED_APP_SERVER_MANAGERS,
    codex_runtime_settings,
    CODEX_SESSION_APP_SERVER_MANAGERS,
    CODEX_SUBAGENT_INDEX_LOCK,
    CODEX_SUBAGENT_LIVE_GENERATIONS,
    CODEX_SUBAGENT_LIVE_MANAGERS,
    CODEX_SUBAGENT_SESSION_INDEX,
    CODEX_SUBAGENT_STATE,
    CODEX_THREAD_SESSION_INDEX,
    codex_user_config_defaults,
    codex_user_config_path,
    effective_claude_permission_mode,
    ensure_session_not_deleting,
    existing_codex_app_server_manager,
    iter_session_events,
    load_codex_user_config,
    mark_provider_context_usage_unavailable,
    native_codex_compaction_id,
    native_codex_compaction_terminal_aliases,
    native_session_available,
    PROVIDER_AUTHORITIES,
    PROVIDER_AUTHORITY_LOCK,
    provider_helper_server_origin,
    register_session_task,
    remember_codex_native_compaction_terminal,
    RUNTIME_DIAGNOSTICS,
    RUNTIME_DIAGNOSTICS_LOCK,
    server_lifecycle_admission_blocker,
    session_codex_thread_id,
    session_lifecycle_lock,
    session_prompt_addendum,
    session_system_prompt,
    STORE,
    UpdateSessionRequest,
    write_abandoned_fork_thread_ids,
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
    claude_background_work_labels_from_events,
    claude_event_file_fingerprints,
    CLAUDE_INTERACTION_HANDLER_TASKS,
    claude_sdk_cli_path,
    claude_sdk_configuration_key,
    claude_sdk_manager,
    ensure_provider_manager_factory_admission,
    handle_claude_tool_permission,
    loaded_claude_background_session_state,
    PROVIDER_WORK_LABEL_LIMIT,
    resolve_claude_resume_provider,
    runtime_command,
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
    CodexAppServerManager,
    CodexAppServerProtocolError,
    CodexAppServerRequestError,
    decline_server_request,
)
from hyadex.providers.codex.config import (
    codex_effective_thread_config,
    codex_session_config_overrides,
    sanitize_codex_thread_config,
)
from hyadex.providers.codex.history import (
    codex_hygiene_int_setting,
    CODEX_ROLLOUT_OVERSIZED_BYTES,
    codex_rollout_stats,
    CODEX_ROLLOUT_WARN_BYTES,
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
from hyadex.providers.subagents import (
    codex_child_status_from_thread,
    codex_collaboration_thread_ids,
    CODEX_GOALS_CONFIG_LOCK,
    codex_session_has_live_subagents,
    codex_subagent_has_live_owner,
    codex_subagent_native_turn_scopes,
    codex_subagent_thread_identity,
    emit_codex_subagent_state,
    normalize_subagent_status,
    project_codex_subagent_item,
    record_codex_subagent_limit_application,
)
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


def request_client_is_codex_provider_mcp_local(request: Request) -> bool:
    """Compatibility-specific name for the shared provider-local predicate."""

    return request_client_is_provider_helper_local(request)


def effective_codex_approval_policy(sess: dict[str, Any]) -> str:
    policy = str(sess.get("codex_approval_policy") or CODEX_DEFAULT_APPROVAL_POLICY)
    return policy if policy in CODEX_APPROVAL_POLICIES else CODEX_DEFAULT_APPROVAL_POLICY


def active_codex_approval_policy(session_id: str, thread_id: str) -> str:
    """Use the current native turn's policy, not next-turn settings edits."""
    active = RUNS.active.get(session_id) or {}
    captured = str(active.get("codex_approval_policy") or "")
    if (
        str(active.get("provider_thread_id") or "") == thread_id
        and captured in CODEX_APPROVAL_POLICIES
    ):
        return captured
    return effective_codex_approval_policy(STORE.sessions.get(session_id) or {})


class CodexGoalsAdminRequest(BaseModel):
    enabled: bool


class CodexInteractionResponseRequest(BaseModel):
    response: dict[str, Any]


class CodexGoalRequest(BaseModel):
    objective: str | None = Field(default=None, max_length=MAX_CODEX_GOAL_CHARS)
    status: Literal[
        "active",
        "paused",
        "blocked",
        "usageLimited",
        "budgetLimited",
        "complete",
    ] | None = None
    token_budget: int | None = Field(default=None, ge=1)
    time_budget_seconds: int | None = Field(default=None, ge=1)


CODEX_APP_SERVER_MANAGER_LOCK = asyncio.Lock()
CODEX_BINARY_PROBE_LOCK = asyncio.Lock()
CODEX_LOGIN_PROBE_LOCK = asyncio.Lock()
CODEX_APP_SERVER_THREAD_LRU: OrderedDict[str, float] = OrderedDict()
CODEX_APP_SERVER_PINNED_THREADS: set[str] = set()
CODEX_APP_SERVER_THREAD_PIN_COUNTS: dict[str, int] = {}
CODEX_APP_SERVER_EVICTING_THREADS: dict[str, asyncio.Event] = {}
CODEX_APP_SERVER_INVALIDATED_THREADS: set[str] = set()
CODEX_APP_SERVER_THREAD_LRU_LOCK = asyncio.Lock()
# A persisted thread goal belongs to the provider thread, while Hyadex's
# session cache survives provider-process restarts. Remember which app-server
# generation has been reconciled so a resumed thread is checked exactly once
# per generation instead of trusting a stale local goal indefinitely.
CODEX_GOAL_SYNC_GENERATIONS: dict[str, tuple[int, int, str]] = {}
# Threads detached after a failed Stop remain fenced for the lifetime of the
# provider process. Any late native goal continuation is interrupted instead
# of being adopted as a new Hyadex run.
CODEX_QUARANTINED_GOAL_THREADS: dict[str, str] = {}
CODEX_COMPACTION_WARN_COUNT = codex_hygiene_int_setting(
    "CODEX_COMPACTION_WARN_COUNT", 100
)
CODEX_HYGIENE_TASKS: dict[str, set[asyncio.Task[Any]]] = {}
CODEX_PENDING_INTERACTIONS: dict[str, dict[str, Any]] = {}
CODEX_PENDING_INTERACTIONS_LOCK = asyncio.Lock()
CODEX_APPROVAL_ITEM_CACHE: OrderedDict[
    tuple[str, str],
    dict[str, Any],
] = OrderedDict()
CODEX_INTERACTIVE_CONTROL_THREADS: set[str] = set()
CODEX_INTERACTIVE_CONTROL_THREAD_COUNTS: dict[str, int] = {}
CODEX_NATIVE_ACTION_TASKS: dict[tuple[str, str], asyncio.Task[Any]] = {}
# Exact reservation owner for a native-control terminal row that has released
# BUSY but still blocks new admission until publication cleanup completes.
CODEX_CONTROL_TERMINAL_FENCES: dict[str, str] = {}
CODEX_INTERACTION_HANDLER_TASKS: dict[str, set[asyncio.Task[Any]]] = {}
ABANDONED_FORK_PROVIDER_THREADS_LOCK = asyncio.Lock()


def session_references_codex_thread(
    session: dict[str, Any],
    thread_id: str,
) -> bool:
    """Return whether a chat owns a Codex identity, active or parked."""

    codex_thread_id = str(session.get("codex_thread_id") or "").strip()
    active_session_id = (
        str(session.get("session_id") or "").strip()
        if str(session.get("backend") or DEFAULT_BACKEND).strip().lower()
        == BACKEND_CODEX
        else ""
    )
    return thread_id in {codex_thread_id, active_session_id}


def acquire_codex_interactive_control_lease(thread_id: str) -> None:
    count = CODEX_INTERACTIVE_CONTROL_THREAD_COUNTS.get(thread_id, 0) + 1
    CODEX_INTERACTIVE_CONTROL_THREAD_COUNTS[thread_id] = count
    CODEX_INTERACTIVE_CONTROL_THREADS.add(thread_id)


def release_codex_interactive_control_lease(thread_id: str) -> None:
    count = CODEX_INTERACTIVE_CONTROL_THREAD_COUNTS.get(thread_id, 0)
    if count <= 1:
        CODEX_INTERACTIVE_CONTROL_THREAD_COUNTS.pop(thread_id, None)
        CODEX_INTERACTIVE_CONTROL_THREADS.discard(thread_id)
        return
    CODEX_INTERACTIVE_CONTROL_THREAD_COUNTS[thread_id] = count - 1


async def reset_codex_ephemeral_runtime_metadata() -> None:
    """Clear process-owned Codex state after transport shutdown or restart."""
    CODEX_GOAL_SYNC_GENERATIONS.clear()
    CODEX_QUARANTINED_GOAL_THREADS.clear()
    with CODEX_SUBAGENT_INDEX_LOCK:
        CODEX_SUBAGENT_LIVE_GENERATIONS.clear()
        CODEX_SUBAGENT_LIVE_MANAGERS.clear()
    changed = False
    async with STORE.transaction():
        for session in STORE.sessions.values():
            if str(session.get("backend") or DEFAULT_BACKEND) != BACKEND_CODEX:
                continue
            reset_values = {
                "codex_thread_status": {"type": "notLoaded"},
                "codex_pending_interaction_count": 0,
                "codex_needs_user_action": False,
            }
            for key, value in reset_values.items():
                if session.get(key) != value:
                    session[key] = value
                    changed = True
            cached_goal = session.get("codex_goal")
            if (
                isinstance(cached_goal, dict)
                and str(cached_goal.get("status") or "") == "active"
            ):
                session["codex_goal"] = {
                    **cached_goal,
                    "status": "paused",
                }
                session["_codex_goal_reconciliation_required"] = {
                    "thread_id": str(session_provider_id(session) or "") or None,
                    "recorded_at": now_iso(),
                    "reason": "Codex app-server stopped without a live run owner.",
                }
                changed = True
            if "_codex_token_usage_terminal" in session:
                session.pop("_codex_token_usage_terminal", None)
                changed = True
        if changed:
            await STORE.save()


def codex_native_goal_owner_matches(session_id: str, run_id: str, thread_id: str, turn_id: str) -> bool:
    """Whether a native Codex goal-resume turn still owns this run.

    Native goal continuation runs without an app-server active-turn record, so
    provider tool calls are matched against the reserved native operation.
    """
    active = RUNS.active.get(session_id) or {}
    current = RUNS.current_turns.get(session_id) or {}
    reservation = str(active.get("codex_control_reservation_id") or "")
    goal = (STORE.sessions.get(session_id) or {}).get("codex_goal") or {}
    return bool(
        thread_id and turn_id and reservation
        and active.get("codex_native_operation") is True
        and active.get("codex_native_operation_kind") == "goal_resume"
        and str(current.get("codex_control_reservation_id") or "") == reservation
        and str(active.get("provider_thread_id") or "") == thread_id
        and str(active.get("provider_turn_id") or "") == turn_id
        and active.get("provider_turn_ready") is True
        and not active.get("codex_goal_handoff_closed")
        and goal.get("status") == "active"
        and provider_capability_is_attached_to_live_run(session_id, run_id)
    )


async def provider_tool_capability_snapshot(
    session_id: str,
    run_id: str,
    *,
    backend: str,
    provider_thread_id: str = "",
    provider_turn_id: str = "",
    claude_owner_token: str = "",
) -> tuple[Path, dict[str, str]]:
    """Resolve exactly one capability between two exact ACTIVE checks."""

    async with RUNS.active_lock:
        matches, ready = provider_tool_active_matches(
            session_id,
            run_id,
            backend=backend,
            provider_thread_id=provider_thread_id,
            provider_turn_id=provider_turn_id,
            claude_owner_token=claude_owner_token,
        )
    if not matches and ready is not None and not ready.is_set():
        try:
            await asyncio.wait_for(ready.wait(), timeout=10.0)
        except asyncio.TimeoutError as exc:
            raise ProviderToolError("provider tool turn is not ready") from exc
    if backend == BACKEND_CLAUDE:
        manager = PROVIDERS.claude_sdk_manager
        if manager is None or not manager.owns_active_run(
            session_id,
            claude_owner_token,
            run_id,
        ):
            raise ProviderToolError("provider tool owner is stale")
    elif backend == BACKEND_CODEX:
        manager = existing_codex_app_server_manager(STORE.sessions.get(session_id))
        active_turn = manager.active_turn(provider_thread_id) if manager else None
        if (
            not (active_turn is not None
                 and str(active_turn.thread_id or "") == provider_thread_id
                 and str(active_turn.turn_id or "") == provider_turn_id)
            and not (manager is not None and manager.ready
                     and codex_native_goal_owner_matches(session_id, run_id, provider_thread_id, provider_turn_id))
        ):
            raise ProviderToolError("provider tool turn is stale")
    else:
        raise ProviderToolError("provider tool backend is unsupported")

    async with RUNS.active_lock:
        matches, _ready = provider_tool_active_matches(
            session_id,
            run_id,
            backend=backend,
            provider_thread_id=provider_thread_id,
            provider_turn_id=provider_turn_id,
            claude_owner_token=claude_owner_token,
        )
    if not matches:
        raise ProviderToolError("provider tool run is no longer active")

    async with PROVIDER_AUTHORITY_LOCK:
        candidates = [
            capability
            for capability in PROVIDER_AUTHORITIES.values()
            if str(capability.get("source_session_id") or "") == session_id
            and str(capability.get("source_run_id") or "") == run_id
            and not str(capability.get("native_transition_nonce") or "")
        ]
        if len(candidates) != 1 or not provider_capability_is_attached_to_live_run(
            session_id,
            run_id,
        ):
            raise ProviderToolError("provider authority is not active")
        capability = candidates[0]
        authority_path = provider_tool_authority_path(capability, run_id)
        runtime_env = validate_provider_runtime_env(
            capability.get("provider_runtime_env")
        )

    async with RUNS.active_lock:
        matches, _ready = provider_tool_active_matches(
            session_id,
            run_id,
            backend=backend,
            provider_thread_id=provider_thread_id,
            provider_turn_id=provider_turn_id,
            claude_owner_token=claude_owner_token,
        )
    if not matches:
        raise ProviderToolError("provider tool run ended during authorization")
    return authority_path, runtime_env


async def execute_provider_tool(
    session_id: str,
    run_id: str,
    value: Any,
    *,
    backend: str,
    provider_thread_id: str = "",
    provider_turn_id: str = "",
    claude_owner_token: str = "",
) -> tuple[str, bool]:
    """Run one allow-listed helper subprocess with server-owned authority."""

    helper, arguments, stdin = validate_provider_tool_input(value)
    authority_path, runtime_env = await provider_tool_capability_snapshot(
        session_id,
        run_id,
        backend=backend,
        provider_thread_id=provider_thread_id,
        provider_turn_id=provider_turn_id,
        claude_owner_token=claude_owner_token,
    )
    resolved = resolve_provider_tool_arguments(helper, arguments, runtime_env)
    scripts = {
        "jobs": SERVER_ROOT / "hyadex_jobs.py",
        "publish": SERVER_ROOT / "hyadex_publish.py",
        "emergency": SERVER_ROOT / "hyadex_emergency.py",
        "notify": SERVER_ROOT / "hyadex_notify.py",

    }
    command = [sys.executable, str(scripts[helper]), "--authority-file", str(authority_path)]
    if helper in {"jobs", "publish", "emergency", "notify"}:
        command.extend(["--chat-id", session_id])
    command.extend(resolved)
    proc: asyncio.subprocess.Process | None = None
    stream_tasks: list[asyncio.Task[Any]] = []
    try:
        proc = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(SERVER_ROOT),
            env=agent_runner_env(session_id, runtime_env),
            limit=PROVIDER_TOOL_MAX_OUTPUT_BYTES + 1,
            start_new_session=True,
        )
        output_size = {"value": 0}

        async def read_bounded(stream: asyncio.StreamReader | None) -> bytes:
            chunks: list[bytes] = []
            if stream is None:
                return b""
            while True:
                chunk = await stream.read(16 * 1024)
                if not chunk:
                    return b"".join(chunks)
                output_size["value"] += len(chunk)
                if output_size["value"] > PROVIDER_TOOL_MAX_OUTPUT_BYTES:
                    raise ProviderToolError(
                        "Hyadex helper output exceeded its safe limit"
                    )
                chunks.append(chunk)

        async def write_stdin() -> None:
            if proc is None or proc.stdin is None:
                return
            proc.stdin.write(stdin.encode("utf-8"))
            await proc.stdin.drain()
            proc.stdin.close()

        stdout_task = asyncio.create_task(read_bounded(proc.stdout))
        stderr_task = asyncio.create_task(read_bounded(proc.stderr))
        stdin_task = asyncio.create_task(write_stdin())
        wait_task = asyncio.create_task(proc.wait())
        stream_tasks = [stdout_task, stderr_task, stdin_task, wait_task]
        stdout, stderr, _written, _returncode = await asyncio.wait_for(
            asyncio.gather(*stream_tasks),
            timeout=PROVIDER_TOOL_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError as exc:
        if proc is not None:
            await terminate_process_tree(proc, grace=0.5)
        raise ProviderToolError("Hyadex helper timed out") from exc
    except asyncio.CancelledError:
        if proc is not None:
            await terminate_process_tree(proc, grace=0.5)
        raise
    except ProviderToolError:
        if proc is not None:
            await terminate_process_tree(proc, grace=0.5)
        raise
    except Exception as exc:
        if proc is not None:
            await terminate_process_tree(proc, grace=0.5)
        raise ProviderToolError("Hyadex helper could not start") from exc
    finally:
        for task in stream_tasks:
            if not task.done():
                task.cancel()
        if stream_tasks:
            await asyncio.gather(*stream_tasks, return_exceptions=True)
    selected = stdout if proc.returncode == 0 else stderr or stdout
    result = redact_provider_tool_output(selected.decode("utf-8", "replace"), authority_path).strip()
    if not result:
        result = "Hyadex helper completed." if proc.returncode == 0 else "Hyadex helper failed."
    return result, proc.returncode != 0


async def execute_provider_tool_once(
    session_id: str,
    run_id: str,
    value: Any,
    *,
    replay_key: str,
    backend: str,
    provider_thread_id: str = "",
    provider_turn_id: str = "",
    claude_owner_token: str = "",
) -> tuple[str, bool]:
    """Single-flight/replay one provider call."""

    helper, arguments, _stdin = validate_provider_tool_input(value)
    # A replay cache is idempotency state, never authority. Revalidate the
    # exact live owner and capability before even looking up an earlier call;
    # otherwise a stopped/revoked run could replay a cached success.
    await provider_tool_capability_snapshot(
        session_id,
        run_id,
        backend=backend,
        provider_thread_id=provider_thread_id,
        provider_turn_id=provider_turn_id,
        claude_owner_token=claude_owner_token,
    )
    key = (session_id, run_id, str(replay_key))
    input_digest = hashlib.sha256(
        json.dumps(
            value,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    leader = False
    async with PROVIDER_TOOL_REPLAY_LOCK:
        prior = PROVIDER_TOOL_REPLAY.get(key)
        if prior is not None and not hmac.compare_digest(prior[0], input_digest):
            raise ProviderToolError("provider tool call identity was reused with different input")
        tombstone_digest = PROVIDER_TOOL_REPLAY_TOMBSTONES.get(key)
        if tombstone_digest is not None:
            PROVIDER_TOOL_REPLAY_TOMBSTONES.move_to_end(key)
            if not hmac.compare_digest(tombstone_digest, input_digest):
                raise ProviderToolError(
                    "provider tool call identity was reused with different input"
                )
            raise ProviderToolError(
                "provider tool call was already completed; its result expired"
            )
        future = prior[1] if prior is not None else None
        if future is None:
            future = asyncio.get_running_loop().create_future()
            PROVIDER_TOOL_REPLAY[key] = (input_digest, future)
            leader = True
    if not leader:
        result = await asyncio.shield(future)
        # The original call may have outlived Stop/revocation while this
        # follower waited. Never surface its cached result to a stale owner.
        await provider_tool_capability_snapshot(
            session_id,
            run_id,
            backend=backend,
            provider_thread_id=provider_thread_id,
            provider_turn_id=provider_turn_id,
            claude_owner_token=claude_owner_token,
        )
        return result
    try:
        result = await execute_provider_tool(
            session_id,
            run_id,
            value,
            backend=backend,
            provider_thread_id=provider_thread_id,
            provider_turn_id=provider_turn_id,
            claude_owner_token=claude_owner_token,
        )
        if not future.done():
            future.set_result(result)
        return result
    except BaseException as exc:
        if not future.done():
            future.set_exception(exc)
            # This function is the first observer; consume the stored future's
            # exception so a failed non-replayed call never warns at shutdown.
            with suppress(BaseException):
                future.exception()
        raise
    finally:
        async with PROVIDER_TOOL_REPLAY_LOCK:
            PROVIDER_TOOL_REPLAY.move_to_end(key)
            while len(PROVIDER_TOOL_REPLAY) > PROVIDER_TOOL_REPLAY_LIMIT:
                evicted = False
                for old_key, (old_digest, old_future) in list(
                    PROVIDER_TOOL_REPLAY.items()
                ):
                    if not old_future.done():
                        continue
                    PROVIDER_TOOL_REPLAY.pop(old_key, None)
                    PROVIDER_TOOL_REPLAY_TOMBSTONES[old_key] = old_digest
                    PROVIDER_TOOL_REPLAY_TOMBSTONES.move_to_end(old_key)
                    evicted = True
                    break
                if not evicted:
                    # In-flight calls are concurrency state and cannot be
                    # evicted safely. Their count is bounded elsewhere by
                    # live provider/tool concurrency.
                    break
            while (
                len(PROVIDER_TOOL_REPLAY_TOMBSTONES)
                > PROVIDER_TOOL_REPLAY_TOMBSTONE_LIMIT
            ):
                PROVIDER_TOOL_REPLAY_TOMBSTONES.popitem(last=False)


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


async def recover_abandoned_codex_compactions_after_start(
    *,
    forced_restart_request_id: str | None = None,
) -> int:
    """Close compaction rows whose provider process vanished mid-operation.

    The bounded pending index lives in sessions.json, so startup does not scan
    large transcripts. A terminal marker uses the original lifecycle identity
    and therefore updates the existing row at its arrival position.
    """

    pending: list[tuple[str, dict[str, Any]]] = []
    for session_id, session in tuple(STORE.sessions.items()):
        value = session.get("_active_codex_compactions")
        if not isinstance(value, dict):
            continue
        for compaction in value.values():
            if isinstance(compaction, dict):
                pending.append((session_id, dict(compaction)))

    recovered = 0
    for session_id, compaction in pending:
        if session_id not in STORE.sessions:
            continue
        payload = {
            key: compaction.get(key)
            for key in (
                "compaction_id",
                "operation_id",
                "thread_id",
                "turn_id",
                "item_id",
                "run_id",
            )
            if compaction.get(key) is not None
        }
        payload.update({
            "status": "interrupted",
            "recovered_after_restart": True,
            "message": (
                "Context compaction was interrupted when HyadexServer restarted."
            ),
        })
        if forced_restart_request_id:
            payload.update({
                "forced_restart": True,
                "restart_request_id": forced_restart_request_id,
                "message": (
                    "Context compaction was forcibly interrupted when "
                    "HyadexServer restarted."
                ),
            })
        try:
            await append_event(
                session_id,
                "codex_compaction_completed",
                payload,
            )
        except Exception as exc:
            logger.warning(
                "could not close abandoned Codex compaction session=%s key=%s error=%s",
                session_id,
                compaction.get("lifecycle_key") or "unknown",
                concise_error_message(exc),
            )
        else:
            recovered += 1
    if recovered:
        logger.warning(
            "closed abandoned Codex compactions after restart count=%d",
            recovered,
        )
    return recovered


async def bind_codex_control_reservation(
    session_id: str,
    reservation_id: str,
    record: dict[str, Any],
) -> tuple[bool, bool]:
    """Bind only the exact run-less native-control reservation.

    Native control startup uses ``run_id=None`` until the provider operation
    is accepted, so normal run-id fencing cannot distinguish an old delayed
    startup from a newer reservation. The private reservation id supplies the
    missing compare-and-swap identity.
    """

    expected = str(reservation_id or "").strip()
    async with RUNS.active_lock:
        current = RUNS.current_turns.get(session_id)
        current_reservation = (
            str(current.get("codex_control_reservation_id") or "")
            if isinstance(current, dict)
            else ""
        )
        owns_reservation = bool(
            expected
            and session_id in RUNS.busy
            and isinstance(current, dict)
            and current.get("purpose") == "codex_native_control"
            and current_reservation == expected
            and session_id not in RUNS.active
        )
        if not owns_reservation:
            return False, False
        stop_requested = session_id in RUNS.stop_requests
        if stop_requested:
            return False, True
        record["codex_control_reservation_id"] = expected
        RUNS.active[session_id] = record
        return True, False


async def codex_session_has_active_run(session_id: str) -> bool:
    """Whether the chat currently holds a turn slot or an ACTIVE run record."""

    async with RUNS.active_lock:
        return session_id in RUNS.busy or bool(RUNS.active.get(session_id))


def codex_native_compaction_was_terminal(
    session_id: str,
    thread_id: str,
    turn_id: str | None,
    item_id: str | None,
) -> bool:
    """Return whether this provider compaction already reached a terminal row.

    A notification that carries an item id is matched only by its exact
    identity or its thread+item alias. The thread+turn alias remembered for an
    earlier compaction is deliberately ignored here, otherwise the second and
    later automatic compactions of one turn would be treated as replays and
    never mirrored into the timeline.
    """

    aliases = set(native_codex_compaction_terminal_aliases(
        thread_id,
        turn_id,
        item_id,
    ))
    if not aliases:
        return False
    session = STORE.sessions.get(session_id) or {}
    for field in (
        "_codex_compaction_native_terminal_keys",
        "_codex_compaction_terminal_keys",
    ):
        values = session.get(field)
        if isinstance(values, list) and aliases.intersection(map(str, values)):
            return True
    return False


def codex_goals_cli_args() -> list[str]:
    """Apply the server-wide goal policy to every one-shot Codex process."""

    return [] if CODEX_GOALS_ENABLED else ["--disable", "goals"]


async def codex_thread_hygiene(session: dict[str, Any]) -> dict[str, Any]:
    """Describe how bloated the chat's current Codex thread has become."""

    thread_id = str(session_provider_id(session) or "").strip()
    stats: dict[str, Any] = {
        "rollout_path": None,
        "rollout_bytes": None,
        "rollout_mtime": None,
    }
    if thread_id and str(session.get("backend") or DEFAULT_BACKEND) == BACKEND_CODEX:
        try:
            stats = await asyncio.to_thread(codex_rollout_stats, thread_id)
        except Exception as exc:
            logger.debug(
                "codex rollout stats unavailable thread=%s: %s",
                thread_id,
                concise_error_message(exc),
            )
    return {
        **stats,
        "compaction_count": codex_compaction_count(session),
        "thread_started_at": session.get("codex_thread_started_at"),
    }


def codex_thread_hygiene_warning(hygiene: dict[str, Any]) -> str | None:
    """Return None, "large", or "oversized" for one hygiene snapshot."""

    rollout_bytes = hygiene.get("rollout_bytes")
    size = (
        int(rollout_bytes)
        if isinstance(rollout_bytes, int) and not isinstance(rollout_bytes, bool)
        else 0
    )
    if size >= CODEX_ROLLOUT_OVERSIZED_BYTES:
        return "oversized"
    count = hygiene.get("compaction_count")
    compactions = (
        int(count) if isinstance(count, int) and not isinstance(count, bool) else 0
    )
    if size >= CODEX_ROLLOUT_WARN_BYTES or compactions >= CODEX_COMPACTION_WARN_COUNT:
        return "large"
    return None


def codex_thread_hygiene_public(hygiene: dict[str, Any]) -> dict[str, Any]:
    """Attach the warning level and thresholds for runtime consumers."""

    return {
        **hygiene,
        "warning": codex_thread_hygiene_warning(hygiene),
        "thresholds": {
            "rollout_warn_bytes": CODEX_ROLLOUT_WARN_BYTES,
            "rollout_oversized_bytes": CODEX_ROLLOUT_OVERSIZED_BYTES,
            "compaction_warn_count": CODEX_COMPACTION_WARN_COUNT,
        },
    }


CODEX_HYGIENE_WARNING_RANK = {None: 0, "large": 1, "oversized": 2}


def format_byte_size(value: int | None) -> str:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        return "unknown size"
    if value >= 1024 * 1024 * 1024:
        return f"{value / (1024 * 1024 * 1024):.1f} GB"
    if value >= 1024 * 1024:
        return f"{value / (1024 * 1024):.0f} MB"
    if value >= 1024:
        return f"{value / 1024:.0f} KB"
    return f"{value} bytes"


def codex_thread_hygiene_message(
    warning: str,
    hygiene: dict[str, Any],
) -> str:
    size_text = format_byte_size(hygiene.get("rollout_bytes"))
    count = hygiene.get("compaction_count")
    compactions = (
        int(count) if isinstance(count, int) and not isinstance(count, bool) else 0
    )
    detail = f"its on-disk rollout is {size_text}"
    if compactions:
        detail += f" after {compactions} context compaction(s)"
    if warning == "oversized":
        return (
            f"This Codex thread is oversized: {detail}. Resuming it is slow and "
            "the app-server may stall or time out. Use \"Start fresh thread\" "
            "to rotate to a new thread with a handoff summary."
        )
    return (
        f"This Codex thread is getting large: {detail}. Long threads resume "
        "slowly and compaction gets less effective. Consider \"Start fresh "
        "thread\" to rotate to a new thread with a handoff summary."
    )


async def warn_codex_thread_hygiene(
    session_id: str,
    thread_id: str,
    run_id: str | None,
) -> None:
    """Append one visible warning the first time a thread crosses a level."""

    session = STORE.sessions.get(session_id)
    if not session or str(session_provider_id(session) or "") != thread_id:
        return
    hygiene = await codex_thread_hygiene(session)
    warning = codex_thread_hygiene_warning(hygiene)
    if warning is None:
        return
    async with STORE.transaction():
        current = STORE.sessions.get(session_id)
        if not current or str(session_provider_id(current) or "") != thread_id:
            return
        warned = current.get("_codex_hygiene_warned")
        warned_level = (
            str(warned.get("level") or "") or None
            if isinstance(warned, dict)
            and str(warned.get("thread_id") or "") == thread_id
            else None
        )
        if (
            CODEX_HYGIENE_WARNING_RANK.get(warning, 0)
            <= CODEX_HYGIENE_WARNING_RANK.get(warned_level, 0)
        ):
            return
        current["_codex_hygiene_warned"] = {
            "thread_id": thread_id,
            "level": warning,
            "warned_at": now_iso(),
        }
        await STORE.save()
    await append_event(
        session_id,
        "codex_thread_hygiene",
        {
            "run_id": run_id or None,
            "backend": BACKEND_CODEX,
            "thread_id": thread_id,
            "warning": warning,
            "rollout_path": hygiene.get("rollout_path"),
            "rollout_bytes": hygiene.get("rollout_bytes"),
            "compaction_count": hygiene.get("compaction_count"),
            "suggested_action": "codex_rotate",
            "message": codex_thread_hygiene_message(warning, hygiene),
        },
    )


def schedule_codex_thread_hygiene_check(
    session_id: str,
    thread_id: str,
    run_id: str | None,
) -> None:
    """Fire-and-forget hygiene check so a turn start is never delayed by it."""

    if not thread_id:
        return

    async def runner() -> None:
        try:
            await warn_codex_thread_hygiene(session_id, thread_id, run_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.debug(
                "codex thread hygiene check failed session=%s: %s",
                session_id,
                concise_error_message(exc),
            )

    register_session_task(
        CODEX_HYGIENE_TASKS,
        session_id,
        asyncio.create_task(runner()),
    )


def build_fork_memory(
    parent: dict[str, Any],
    parent_id: str,
    *,
    reason: str | None = None,
    exclude_run_id: str | None = None,
) -> str:
    provider_id = session_provider_id(parent)
    header = [
        "[Hyadex memory fork]",
        "This is a fresh provider thread seeded from a compact memory dump because the original provider-level fork was unavailable.",
        "Use this memory as background context. Do not treat it as a new user request.",
        "",
        f"Parent Hyadex session: {parent_id}",
        f"Parent title: {parent.get('title') or 'Untitled'}",
        f"Backend: {parent.get('backend') or DEFAULT_BACKEND}",
        f"Working directory: {parent.get('cwd') or DEFAULT_CWD}",
    ]
    if provider_id:
        header.append(f"Original provider session/thread: {provider_id}")
    if reason:
        header.append(f"Fork fallback reason: {compact_memory_text(reason, 800)}")

    lines: list[str] = header + ["", "Recent rough conversation:"]
    # Select the most recent useful conversational events, not merely the last
    # raw events. A single tool-heavy turn can contain thousands of tool events
    # and previously displaced every user/assistant message from the fallback.
    internal_run_ids = {
        str(event.get("run_id"))
        for event in iter_session_events(parent_id)
        if event.get("purpose") in FORK_INTERNAL_PURPOSES
        and str(event.get("run_id") or "").strip()
    }
    events: deque[dict[str, Any]] = deque(maxlen=160)
    for event in iter_session_events(parent_id):
        event = project_provider_history_event_for_egress(event, parent_id)
        run_id = str(event.get("run_id") or "").strip()
        event_type = str(event.get("type") or "")
        if (
            (exclude_run_id and run_id == exclude_run_id)
            or event.get("purpose") in FORK_INTERNAL_PURPOSES
            or (run_id and run_id in internal_run_ids)
        ):
            continue
        if event_type in {"turn_started", "assistant_text", "turn_finished", "artifact_created"} or is_native_goal_steer_event(event):
            events.append(event)
        elif event_type == "reasoning_summary" and event.get("phase") == "commentary":
            events.append(event)
    assistant_runs = {
        event.get("run_id")
        for event in events
        if event.get("type") == "assistant_text" and str(event.get("text") or "").strip()
    }

    for event in events:
        event_type = event.get("type")
        if event_type == "turn_started" or is_native_goal_steer_event(event):
            text = compact_memory_text(event.get("prompt") or "")
            if text:
                lines.append(f"\nUser:\n{text}")
        elif event_type == "assistant_text":
            text = compact_memory_text(event.get("text") or "")
            if text:
                lines.append(f"\nAssistant:\n{text}")
        elif event_type == "turn_finished" and event.get("run_id") not in assistant_runs:
            text = compact_memory_text(event.get("result_text") or "")
            if text:
                lines.append(f"\nAssistant:\n{text}")
        elif event_type == "reasoning_summary":
            text = compact_memory_text(event.get("text") or "", 900)
            if text:
                lines.append(f"\nAssistant commentary:\n{text}")
        elif event_type == "artifact_created":
            artifact = event.get("artifact") if isinstance(event.get("artifact"), dict) else {}
            title = artifact.get("title") or artifact.get("filename") or artifact.get("id") or "artifact"
            note = compact_memory_text(artifact.get("text") or "", 600)
            artifact_line = f"\nArtifact: {title}"
            if artifact.get("content_type"):
                artifact_line += f" ({artifact.get('content_type')})"
            if note:
                artifact_line += f"\n{note}"
            lines.append(artifact_line)

    memory = "\n".join(lines).strip()
    if len(memory) > MAX_FORK_MEMORY_CHARS:
        trim_prefix = (
            "[Hyadex memory fork]\n[Older memory trimmed]\n"
        )
        tail_budget = max(0, MAX_FORK_MEMORY_CHARS - len(trim_prefix))
        memory = trim_prefix + memory[-tail_budget:].lstrip()
        memory = memory[:MAX_FORK_MEMORY_CHARS]
    return memory


def is_codex_compaction_failure(message: str) -> bool:
    lowered = str(message or "").lower()
    return "remote compact task" in lowered and "stream disconnected" in lowered


def codex_compaction_retry_mode(turns: Any, failed_turn_id: str, run_id: str) -> str | None:
    """Prove which input, if any, a same-thread compaction retry can use.

    An empty/summary snapshot is not evidence. Unknown items, a later turn,
    or any execution forbid recovery, even if no final reply was seen. When
    Codex persisted this exact user message before failing, continue with empty
    input: native turn/start supports that without adding another user item.
    """
    if not failed_turn_id or not isinstance(turns, list) or not turns:
        return None
    turn = turns[0]
    if not isinstance(turn, dict) or (
        turn.get("id") != failed_turn_id
        or turn.get("status") != "failed"
        or turn.get("itemsView") != "full"
    ):
        return None
    items = turn.get("items")
    if not isinstance(items, list) or not items or any(
        not isinstance(item, dict) or item.get("type") not in {"contextCompaction", "userMessage"}
        for item in items
    ):
        return None
    users = [item for item in items if item.get("type") == "userMessage"]
    if not users:
        return "resubmit"
    if len(users) == 1 and run_id and users[0].get("clientId") == run_id:
        return "continue"
    return None


def codex_compaction_error_message(model: str, native_error: str) -> str:
    # The native error may name the previous model even after native fallback.
    # Do not invent its hidden fallback error or infer retirement from a stream error.
    return (
        f"Codex context compaction failed with {model or 'the current model'} selected. "
        "The original conversation and history were kept. The provider error below "
        "may refer to an earlier model; it does not mean your selection was ignored. "
        "Any additional native fallback failure may not be exposed by Codex. "
        "Try Compact context in Codex Thread Controls, then send again. "
        f"Provider error: {native_error}"
    )


def is_codex_resume_failure(message: str) -> bool:
    lowered = str(message or "").lower()
    return any(
        marker in lowered
        for marker in (
            "no conversation found with session id",
            "conversation not found",
            "thread not found",
            "unknown thread",
            "thread does not exist",
            "thread is not loaded",
            "session not found",
            "failed to resume",
            "could not resume",
            "unable to resume",
        )
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


def should_recover_codex_resume(
    *,
    allow_rollover: bool,
    resumed_provider_id: str | None,
    stopped: bool,
    stream_error: str | None,
    idle_killed: bool,
    resume_stalled: bool,
    returncode: int | None,
    produced_activity: bool,
    terminal_error: str,
) -> bool:
    if (
        not allow_rollover
        or not resumed_provider_id
        or stopped
        or stream_error
        or idle_killed
        or produced_activity
        # Compaction is not evidence of a missing thread. Never silently
        # replace original context with a bounded-memory fork on this error.
        or is_codex_compaction_failure(terminal_error)
    ):
        return False
    if resume_stalled or returncode in (0, None):
        return True
    return is_codex_resume_failure(terminal_error)


async def rollover_codex_provider_session(
    session_id: str,
    run_id: str,
    provider_id: str,
    reason: str,
    *,
    message: str,
    memory_seed_used: bool = True,
) -> tuple[dict[str, Any], str] | None:
    current = STORE.sessions.get(session_id)
    if not current:
        return None
    snapshot = dict(current)
    memory = build_fork_memory(snapshot, session_id, reason=reason, exclude_run_id=run_id)
    quarantined_goal_thread = False
    usage_signal: dict[str, Any] | None = None
    async with STORE.transaction():
        current = STORE.sessions.get(session_id)
        if not current or session_provider_id(current) != provider_id:
            return None
        current_goal = current.get("codex_goal")
        if (
            isinstance(current_goal, dict)
            and str(current_goal.get("status") or "") == "active"
        ):
            # A provider rollover abandons the native thread. Keep the user's
            # objective, but never claim it is actively running without a
            # local owner; the replacement thread will receive it paused.
            current["codex_goal"] = {**current_goal, "status": "paused"}
            quarantined_goal_thread = True
        current["session_id"] = None
        current["codex_thread_id"] = None
        current.pop("codex_instruction_hash", None)
        current.pop("codex_instruction_version", None)
        current.pop("codex_token_usage", None)
        current.pop("codex_token_usage_snapshot", None)
        current.pop("_codex_token_usage_checkpoint", None)
        current.pop("_codex_token_usage_terminal", None)
        usage_signal = clear_provider_context_usage_locked(
            current,
            BACKEND_CODEX,
            provider_session_id=None,
            state="cleared",
        )
        current["memory_seed"] = memory
        current["memory_seed_used"] = memory_seed_used
        current["memory_forked"] = True
        current["memory_fork_reason"] = compact_memory_text(reason, 2000)
        current["updated_at"] = now_iso()
        await STORE.save()
        fresh_session = dict(current)
    if CODEX_THREAD_SESSION_INDEX.get(provider_id) == session_id:
        CODEX_THREAD_SESSION_INDEX.pop(provider_id, None)
    CODEX_GOAL_SYNC_GENERATIONS.pop(session_id, None)
    if quarantined_goal_thread:
        CODEX_QUARANTINED_GOAL_THREADS[provider_id] = session_id
    if usage_signal is not None:
        await broadcast_provider_runtime_changed(session_id, usage_signal)
    await append_event(session_id, "provider_rollover", {
        "run_id": run_id,
        "backend": BACKEND_CODEX,
        "old_provider_session_id": provider_id,
        "message": message,
        "reason": compact_memory_text(reason, 1200),
    })
    return fresh_session, memory


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


def codex_session_id_for_thread(thread_id: str) -> str | None:
    if not thread_id:
        return None
    with CODEX_SUBAGENT_INDEX_LOCK:
        child_session_id = CODEX_SUBAGENT_SESSION_INDEX.get(thread_id)
    if child_session_id:
        child_session = STORE.sessions.get(child_session_id)
        if (
            child_session is not None
            and child_session_id not in DELETING_SESSIONS
            and child_session_id not in DELETED_SESSION_TOMBSTONES
        ):
            if session_codex_thread_id(child_session) == thread_id:
                # Root ownership wins over a stale child receiver record.
                with CODEX_SUBAGENT_INDEX_LOCK:
                    CODEX_SUBAGENT_SESSION_INDEX.pop(thread_id, None)
                    CODEX_SUBAGENT_STATE.pop(thread_id, None)
                    CODEX_SUBAGENT_LIVE_GENERATIONS.pop(thread_id, None)
                    CODEX_SUBAGENT_LIVE_MANAGERS.pop(thread_id, None)
                CODEX_THREAD_SESSION_INDEX[thread_id] = child_session_id
            return child_session_id
        with CODEX_SUBAGENT_INDEX_LOCK:
            CODEX_SUBAGENT_SESSION_INDEX.pop(thread_id, None)
            CODEX_SUBAGENT_STATE.pop(thread_id, None)
            CODEX_SUBAGENT_LIVE_GENERATIONS.pop(thread_id, None)
            CODEX_SUBAGENT_LIVE_MANAGERS.pop(thread_id, None)
    indexed = CODEX_THREAD_SESSION_INDEX.get(thread_id)
    if indexed:
        session = STORE.sessions.get(indexed)
        if (
            session
            and indexed not in DELETING_SESSIONS
            and indexed not in DELETED_SESSION_TOMBSTONES
            and str(session_provider_id(session) or "") == thread_id
        ):
            return indexed
        CODEX_THREAD_SESSION_INDEX.pop(thread_id, None)
    for session_id, session in STORE.sessions.items():
        if (
            session_id in DELETING_SESSIONS
            or session_id in DELETED_SESSION_TOMBSTONES
        ):
            continue
        if str(session_provider_id(session) or "") == thread_id:
            CODEX_THREAD_SESSION_INDEX[thread_id] = session_id
            return session_id
    return None


def codex_request_is_interactive(session_id: str, thread_id: str) -> bool:
    if (
        session_id in DELETING_SESSIONS
        or session_id in DELETED_SESSION_TOMBSTONES
    ):
        return False
    if thread_id in CODEX_INTERACTIVE_CONTROL_THREADS:
        return True
    active = RUNS.active.get(session_id)
    return bool(
        active
        and str(active.get("provider_thread_id") or "") == thread_id
        and active.get("interactive_app_server") is True
    )


CODEX_TOKEN_USAGE_PERSIST_INTERVAL_SECONDS = 30.0


async def mark_codex_exec_context_usage_unavailable(session_id: str) -> None:
    """Invalidate native context telemetry before any codex exec turn."""

    await mark_provider_context_usage_unavailable(session_id, BACKEND_CODEX)


def codex_token_usage_snapshot(
    token_usage: Any,
    *,
    thread_id: str,
    turn_id: str | None,
    run_id: str | None,
    snapshot_at: str | None = None,
) -> dict[str, Any] | None:
    """Return one bounded, forward-compatible app-server usage snapshot.

    ``total`` is cumulative thread usage and can grow into the millions.  The
    live context occupancy reported to clients must instead use ``last``.
    """
    bounded = bounded_codex_interaction_value(token_usage, remaining=4_000)
    if not isinstance(bounded, dict):
        return None
    last = bounded.get("last") if isinstance(bounded.get("last"), dict) else {}
    total = (
        bounded.get("total")
        if isinstance(bounded.get("total"), dict)
        else {}
    )

    def token_count(value: Any) -> int | None:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        return max(0, int(value))

    raw_context_tokens = token_count(last.get("totalTokens"))
    raw_context_window = token_count(bounded.get("modelContextWindow"))
    effective_context_tokens = (
        max(0, raw_context_tokens - CODEX_CONTEXT_WINDOW_RESERVE_TOKENS)
        if raw_context_tokens is not None
        else None
    )
    effective_context_window = (
        max(1, raw_context_window - CODEX_CONTEXT_WINDOW_RESERVE_TOKENS)
        if raw_context_window
        else None
    )
    context_percent = (
        round(
            min(
                100.0,
                effective_context_tokens * 100 / effective_context_window,
            ),
            2,
        )
        if effective_context_tokens is not None and effective_context_window
        else None
    )
    snapshot: dict[str, Any] = {
        "thread_id": thread_id,
        "turn_id": turn_id,
        "run_id": run_id,
        "snapshot_at": snapshot_at or now_iso(),
        "token_usage": bounded,
        "input_tokens": token_count(last.get("inputTokens")),
        "cached_input_tokens": token_count(last.get("cachedInputTokens")),
        "cache_write_input_tokens": token_count(
            last.get("cacheWriteInputTokens")
        ),
        "output_tokens": token_count(last.get("outputTokens")),
        "reasoning_output_tokens": token_count(
            last.get("reasoningOutputTokens")
        ),
        "total_tokens": token_count(last.get("totalTokens")),
        "cumulative_total_tokens": token_count(total.get("totalTokens")),
        "raw_context_tokens": raw_context_tokens,
        # Preserve the legacy/raw contract. New clients use the explicit
        # reserve/effective fields below and must not subtract twice.
        "context_tokens": raw_context_tokens,
        "context_window": raw_context_window,
        "baseline_tokens": CODEX_CONTEXT_WINDOW_RESERVE_TOKENS,
        "effective_context_window": effective_context_window,
        "raw_context_window": raw_context_window,
        "context_percent": context_percent,
        "provider_session_id": thread_id,
        "context_usage_state": "available",
    }
    return {key: value for key, value in snapshot.items() if value is not None}


def current_codex_token_usage_snapshot(session_id: str) -> dict[str, Any] | None:
    session = STORE.sessions.get(session_id)
    value = session.get("codex_token_usage_snapshot") if session else None
    return dict(value) if isinstance(value, dict) else None


def terminal_codex_token_usage_run_id(
    session_id: str,
    thread_id: str,
    turn_id: str | None,
) -> str | None:
    session = STORE.sessions.get(session_id)
    terminal = session.get("_codex_token_usage_terminal") if session else None
    marker = terminal if isinstance(terminal, dict) else {}
    if (
        turn_id is not None
        and marker.get("thread_id") == thread_id
        and marker.get("turn_id") == turn_id
    ):
        return str(marker.get("run_id") or "") or None
    return None


async def record_codex_token_usage(
    session_id: str,
    snapshot: dict[str, Any],
    *,
    force_checkpoint: bool = False,
) -> bool:
    """Keep every sample in memory and durably checkpoint at a bounded rate.

    Token usage is runtime/control state, never timeline content.  Emitting one
    event per sample would leak unknown rows into older clients and make large
    installations rewrite/broadcast far too often.  Persist the first sample
    for a run/turn, then at most every interval, plus explicit terminal points.
    """
    now_epoch = time.time()
    should_checkpoint = force_checkpoint
    signal: dict[str, Any] | None = None
    async with STORE.transaction():
        session = STORE.sessions.get(session_id)
        if not session:
            return False
        snapshot_thread_id = str(snapshot.get("thread_id") or "").strip()
        current_thread_id = str(session_provider_id(session) or "").strip()
        if not snapshot_thread_id or snapshot_thread_id != current_thread_id:
            return False
        stored_snapshot = dict(snapshot)
        usage_generation = next_provider_context_usage_generation(session)
        stored_snapshot["usage_generation"] = usage_generation
        manager = existing_codex_app_server_manager(session)
        provider_generation = getattr(manager, "generation", None)
        if (
            isinstance(provider_generation, (int, float))
            and not isinstance(provider_generation, bool)
        ):
            stored_snapshot["provider_generation"] = max(
                0, int(provider_generation)
            )
        model = str(session.get("model") or "")[:240]
        if model:
            stored_snapshot["model"] = model
        session["codex_token_usage"] = dict(snapshot.get("token_usage") or {})
        session["codex_token_usage_snapshot"] = stored_snapshot
        session["context_usage_state"] = "available"
        session["context_usage_snapshot"] = stored_snapshot
        signal = provider_context_usage_signal(
            BACKEND_CODEX,
            state="available",
            generation=usage_generation,
            provider_session_id=snapshot_thread_id,
            snapshot=stored_snapshot,
        )
        previous = session.get("_codex_token_usage_checkpoint")
        previous_marker = previous if isinstance(previous, dict) else {}
        terminal = session.get("_codex_token_usage_terminal")
        terminal_marker = terminal if isinstance(terminal, dict) else {}
        terminal_match = bool(
            snapshot.get("turn_id")
            and terminal_marker.get("thread_id") == snapshot.get("thread_id")
            and terminal_marker.get("turn_id") == snapshot.get("turn_id")
        )
        if terminal_marker and not terminal_match:
            # A sample for a different turn proves the prior terminal marker
            # can no longer receive a late final usage update.
            session.pop("_codex_token_usage_terminal", None)
        previous_saved_at = previous_marker.get("saved_at_epoch")
        elapsed = (
            now_epoch - float(previous_saved_at)
            if isinstance(previous_saved_at, (int, float))
            and not isinstance(previous_saved_at, bool)
            else None
        )
        if (
            previous_marker.get("run_id") != snapshot.get("run_id")
            or previous_marker.get("turn_id") != snapshot.get("turn_id")
            or elapsed is None
            or elapsed < 0
            or elapsed >= CODEX_TOKEN_USAGE_PERSIST_INTERVAL_SECONDS
            or terminal_match
        ):
            should_checkpoint = True
        if should_checkpoint:
            session["_codex_token_usage_checkpoint"] = {
                "run_id": snapshot.get("run_id"),
                "turn_id": snapshot.get("turn_id"),
                "saved_at_epoch": now_epoch,
            }
            # The latest bounded checkpoint remains available after restart;
            # routine telemetry never changes chat ordering.
            await STORE.save()
    if signal is not None:
        await broadcast_provider_runtime_changed(session_id, signal)
    return should_checkpoint


async def codex_notification_run_id(
    session_id: str,
    thread_id: str,
    turn_id: str | None,
) -> str | None:
    async with RUNS.active_lock:
        active = RUNS.active.get(session_id)
        if not active:
            return None
        if (
            active.get("backend") != BACKEND_CODEX
            or active.get("transport") != CODEX_TRANSPORT_APP_SERVER
        ):
            return None
        active_thread_id = str(active.get("provider_thread_id") or "")
        if active_thread_id and active_thread_id != thread_id:
            return None
        active_turn_id = str(active.get("provider_turn_id") or "")
        if active_turn_id and turn_id and active_turn_id != turn_id:
            return None
        return str(active.get("run_id") or "") or None


def public_codex_interaction(pending: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": pending["id"],
        "session_id": pending["session_id"],
        "thread_id": pending["thread_id"],
        "turn_id": pending.get("turn_id"),
        "item_id": pending.get("item_id"),
        "method": pending["method"],
        "params": pending["params"],
        "created_at": pending["created_at"],
        "auto_resolution_ms": pending.get("auto_resolution_ms"),
    }


def cache_codex_approval_item(notification: dict[str, Any]) -> None:
    """Keep bounded item/started details so approval cards can show their scope."""
    method = str(notification.get("method") or "")
    params = (
        notification.get("params")
        if isinstance(notification.get("params"), dict)
        else {}
    )
    thread_id = str(params.get("threadId") or "")
    item = params.get("item") if isinstance(params.get("item"), dict) else {}
    item_id = str(params.get("itemId") or item.get("id") or "")
    if method == "item/started" and thread_id and item_id:
        bounded = bounded_codex_interaction_value(item)
        if isinstance(bounded, dict):
            key = (thread_id, item_id)
            CODEX_APPROVAL_ITEM_CACHE.pop(key, None)
            CODEX_APPROVAL_ITEM_CACHE[key] = bounded
            while len(CODEX_APPROVAL_ITEM_CACHE) > MAX_CODEX_APPROVAL_ITEM_CACHE:
                CODEX_APPROVAL_ITEM_CACHE.popitem(last=False)
        return
    if method == "item/completed" and thread_id and item_id:
        CODEX_APPROVAL_ITEM_CACHE.pop((thread_id, item_id), None)
        return
    if method in {"turn/completed", "thread/closed"} and thread_id:
        for key in tuple(CODEX_APPROVAL_ITEM_CACHE):
            if key[0] == thread_id:
                CODEX_APPROVAL_ITEM_CACHE.pop(key, None)


async def update_codex_pending_session_metadata(session_id: str) -> None:
    async with CODEX_PENDING_INTERACTIONS_LOCK:
        count = sum(
            1
            for pending in CODEX_PENDING_INTERACTIONS.values()
            if pending.get("session_id") == session_id
            and not pending.get("responded")
        )
    async with STORE.transaction():
        session = STORE.sessions.get(session_id)
        if not session:
            return
        changed = (
            int(session.get("codex_pending_interaction_count") or 0) != count
            or bool(session.get("codex_needs_user_action")) != bool(count)
        )
        session["codex_pending_interaction_count"] = count
        session["codex_needs_user_action"] = bool(count)
        if changed:
            session["updated_at"] = now_iso()
            await STORE.save()


def codex_permission_value_is_subset(granted: Any, requested: Any) -> bool:
    """Conservatively ensure a grant cannot exceed the requested permission."""
    if granted in (None, False, "", [], {}):
        return True
    if isinstance(granted, dict):
        return isinstance(requested, dict) and all(
            key in requested
            and codex_permission_value_is_subset(item, requested[key])
            for key, item in granted.items()
        )
    if isinstance(granted, list):
        if not isinstance(requested, list):
            return False
        return all(
            any(
                codex_permission_value_is_subset(item, candidate)
                and codex_permission_value_is_subset(candidate, item)
                for candidate in requested
            )
            for item in granted
        )
    return granted == requested


def validate_codex_interaction_response(
    pending: dict[str, Any],
    response: dict[str, Any],
) -> dict[str, Any]:
    method = str(pending.get("method") or "")
    params = pending.get("params") if isinstance(pending.get("params"), dict) else {}
    if method in {
        "item/commandExecution/requestApproval",
        "item/fileChange/requestApproval",
    }:
        if set(response) != {"decision"}:
            raise HTTPException(status_code=400, detail="approval response requires only decision")
        decision = response.get("decision")
        allowed = {"accept", "acceptForSession", "decline", "cancel"}
        if isinstance(decision, str):
            if decision not in allowed:
                raise HTTPException(status_code=400, detail="invalid approval decision")
            available = params.get("availableDecisions")
            if isinstance(available, list) and decision not in available:
                raise HTTPException(status_code=400, detail="approval decision is unavailable")
            return {"decision": decision}
        if method != "item/commandExecution/requestApproval" or not isinstance(decision, dict):
            raise HTTPException(status_code=400, detail="invalid approval decision")
        if set(decision) == {"acceptWithExecpolicyAmendment"}:
            body = decision["acceptWithExecpolicyAmendment"]
            amendment = (
                body.get("execpolicy_amendment")
                if isinstance(body, dict)
                and set(body) == {"execpolicy_amendment"}
                else None
            )
            proposed = params.get("proposedExecpolicyAmendment")
            if (
                not isinstance(amendment, list)
                or any(not isinstance(item, str) for item in amendment)
                or amendment != proposed
            ):
                raise HTTPException(status_code=400, detail="exec policy amendment must match the proposal")
        elif set(decision) == {"applyNetworkPolicyAmendment"}:
            body = decision["applyNetworkPolicyAmendment"]
            amendment = (
                body.get("network_policy_amendment")
                if isinstance(body, dict)
                and set(body) == {"network_policy_amendment"}
                else None
            )
            proposed = params.get("proposedNetworkPolicyAmendments")
            if (
                not isinstance(amendment, dict)
                or set(amendment) != {"action", "host"}
                or amendment.get("action") not in {"allow", "deny"}
                or not isinstance(amendment.get("host"), str)
                or not isinstance(proposed, list)
                or amendment not in proposed
            ):
                raise HTTPException(
                    status_code=400,
                    detail="network policy amendment must match a proposal",
                )
        else:
            raise HTTPException(status_code=400, detail="invalid command approval decision")
        available = params.get("availableDecisions")
        if not isinstance(available, list) or decision not in available:
            raise HTTPException(status_code=400, detail="approval decision is unavailable")
        return {"decision": decision}

    if method == "item/tool/requestUserInput":
        if set(response) != {"answers"} or not isinstance(response.get("answers"), dict):
            raise HTTPException(status_code=400, detail="question response requires an answers map")
        questions = {
            str(question.get("id") or ""): question
            for question in params.get("questions") or []
            if isinstance(question, dict) and question.get("id")
        }
        answers: dict[str, Any] = {}
        for question_id, answer in response["answers"].items():
            if question_id not in questions:
                raise HTTPException(status_code=400, detail=f"unknown question id: {question_id}")
            values = answer.get("answers") if isinstance(answer, dict) else None
            if (
                not isinstance(values, list)
                or len(values) > 20
                or any(not isinstance(value, str) or len(value) > 8_000 for value in values)
            ):
                raise HTTPException(status_code=400, detail=f"invalid answer for {question_id}")
            answers[question_id] = {"answers": list(values)}
        return {"answers": answers}

    if method == "item/permissions/requestApproval":
        allowed_keys = {"permissions", "scope", "strictAutoReview"}
        if not set(response).issubset(allowed_keys) or not isinstance(response.get("permissions"), dict):
            raise HTTPException(status_code=400, detail="invalid permission grant")
        scope = str(response.get("scope") or "turn")
        if scope not in {"turn", "session"}:
            raise HTTPException(status_code=400, detail="permission scope must be turn or session")
        requested = params.get("permissions")
        if not codex_permission_value_is_subset(response["permissions"], requested):
            raise HTTPException(status_code=400, detail="permission grant exceeds the requested subset")
        return {
            "permissions": response["permissions"],
            "scope": scope,
            "strictAutoReview": bool(response.get("strictAutoReview", False)),
        }

    if method == "mcpServer/elicitation/request":
        if not set(response).issubset({"action", "content"}):
            raise HTTPException(status_code=400, detail="invalid MCP elicitation response")
        action = str(response.get("action") or "")
        if action not in {"accept", "decline", "cancel"}:
            raise HTTPException(status_code=400, detail="invalid MCP elicitation action")
        content = response.get("content")
        if action == "accept" and content is None:
            raise HTTPException(status_code=400, detail="accepted MCP elicitation requires content")
        return {"action": action, "content": content if action == "accept" else None}

    if method in {"applyPatchApproval", "execCommandApproval"}:
        decision = str(response.get("decision") or "")
        if decision not in {"approved", "approved_for_session", "denied", "abort"}:
            raise HTTPException(status_code=400, detail="invalid legacy approval decision")
        return {"decision": decision}

    raise HTTPException(status_code=400, detail=f"unsupported Codex interaction: {method}")


_CODEX_CANCEL_INTERACTION_FUTURE = object()


def finish_codex_interaction_locked(
    pending: dict[str, Any],
    *,
    resolution: str,
    response: dict[str, Any] | object,
) -> bool:
    """Atomically claim one pending interaction and settle its waiter.

    Callers must hold ``CODEX_PENDING_INTERACTIONS_LOCK``. Keeping the state
    transition and Future settlement in the same critical section prevents a
    user response, timeout, and session cancellation from all believing they
    won the same prompt.
    """
    future = pending.get("future")
    if (
        pending.get("responded")
        or not isinstance(future, asyncio.Future)
        or future.done()
    ):
        return False
    pending["responded"] = True
    pending["resolution"] = resolution
    if response is _CODEX_CANCEL_INTERACTION_FUTURE:
        future.cancel()
    else:
        future.set_result(response)
    return True


async def resolve_codex_interaction(
    session_id: str,
    interaction_id: str,
    response: dict[str, Any],
) -> dict[str, Any]:
    async with CODEX_PENDING_INTERACTIONS_LOCK:
        pending = CODEX_PENDING_INTERACTIONS.get(interaction_id)
        if not pending or pending.get("session_id") != session_id:
            raise HTTPException(status_code=404, detail="pending Codex interaction not found")
        if pending.get("responded") or pending["future"].done():
            raise HTTPException(status_code=409, detail="Codex interaction is already resolved")
        validated = validate_codex_interaction_response(pending, response)
        if not finish_codex_interaction_locked(
            pending,
            resolution="answered",
            response=validated,
        ):
            raise HTTPException(status_code=409, detail="Codex interaction is already resolved")
        interaction = public_codex_interaction(pending)
    return interaction


async def cancel_codex_interactions(
    session_id: str | None = None,
    *,
    resolution: str = "dismissed",
) -> None:
    """Resolve pending prompts with least privilege so shutdown never wedges."""
    async with CODEX_PENDING_INTERACTIONS_LOCK:
        pending_items = [
            (interaction_id, pending)
            for interaction_id, pending in CODEX_PENDING_INTERACTIONS.items()
            if session_id is None or pending.get("session_id") == session_id
            if resolution != "turn_stopped" or not pending.get("side_chat")
        ]
    affected: set[str] = set()
    for interaction_id, pending in pending_items:
        try:
            response = await decline_server_request(
                pending.get("native_request_id"),
                str(pending.get("method") or ""),
                dict(pending.get("params") or {}),
            )
        except Exception:
            response = _CODEX_CANCEL_INTERACTION_FUTURE
        async with CODEX_PENDING_INTERACTIONS_LOCK:
            if CODEX_PENDING_INTERACTIONS.get(interaction_id) is not pending:
                continue
            won = finish_codex_interaction_locked(
                pending,
                resolution=resolution,
                response=response,
            )
        if won:
            affected.add(str(pending.get("session_id") or ""))
    for affected_session_id in affected:
        if affected_session_id:
            with suppress(Exception):
                await update_codex_pending_session_metadata(affected_session_id)


async def handle_codex_server_request(
    request_id: Any,
    method: str,
    params: dict[str, Any],
    *,
    side_session_id: str | None = None,
    side_owner_is_current: Any = None,
    side_approval_policy: str | None = None,
    source_manager: CodexAppServerManager | None = None,
) -> dict[str, Any]:
    """Bridge one app-server prompt to the owning Hyadex chat, fail closed."""
    thread_id = str(params.get("threadId") or "")
    session_id = side_session_id or codex_session_id_for_thread(thread_id)

    def request_is_owned() -> bool:
        if source_manager is not None and not codex_manager_owns_notification(source_manager, {"params": params}):
            return False
        if side_session_id is not None:
            # A side fork belongs to its private client, never to the main
            # thread registry. Recheck that exact owner under the lifecycle lock.
            return bool(callable(side_owner_is_current) and side_owner_is_current())
        return bool(codex_session_id_for_thread(thread_id) == session_id
                    and codex_request_is_interactive(session_id, thread_id))

    if (
        method not in CODEX_INTERACTION_METHODS
        or not session_id
        or not request_is_owned()
    ):
        return await decline_server_request(request_id, method, params)
    pending: dict[str, Any] | None = None
    async with session_lifecycle_lock(session_id):
        # Deletion takes the same lock before marking the chat. Rechecking the
        # route and installing both ownership records here makes it impossible
        # for deletion to miss a newly arriving provider request.
        if (
            session_id in DELETING_SESSIONS
            or session_id in DELETED_SESSION_TOMBSTONES
            or not request_is_owned()
        ):
            pending = None
        else:
            if method == "item/tool/requestUserInput":
                policy = (
                    side_approval_policy or CODEX_DEFAULT_APPROVAL_POLICY
                    if side_session_id is not None
                    else active_codex_approval_policy(session_id, thread_id)
                )
                if policy == "never":
                    # Native questions are optional input, not permission to
                    # invent an answer. Skip before installing any UI waiter.
                    return await decline_server_request(request_id, method, params)
            manager = (None if side_session_id is not None else source_manager or
                       existing_codex_app_server_manager_for_thread(thread_id))
            generation = manager.generation if manager is not None else 0
            loop = asyncio.get_running_loop()
            future: asyncio.Future[dict[str, Any]] = loop.create_future()
            interaction_id = f"codexreq_{generation}_{uuid.uuid4().hex[:16]}"
            display_params = dict(params)
            item_id = str(params.get("itemId") or "")
            approval_item = CODEX_APPROVAL_ITEM_CACHE.get((thread_id, item_id))
            if approval_item is not None:
                display_params["approvalItem"] = approval_item
                if (
                    method == "item/fileChange/requestApproval"
                    and "changes" not in display_params
                    and isinstance(approval_item.get("changes"), list)
                ):
                    display_params["changes"] = approval_item["changes"]
            public_params = bounded_codex_interaction_value(display_params)
            candidate = {
                "id": interaction_id,
                "native_request_id": request_id,
                "generation": generation,
                "session_id": session_id,
                "thread_id": thread_id,
                "side_chat": side_session_id is not None,
                "turn_id": str(params.get("turnId") or "") or None,
                "item_id": str(params.get("itemId") or "") or None,
                "method": method,
                "params": (
                    public_params if isinstance(public_params, dict) else {}
                ),
                "created_at": now_iso(),
                "auto_resolution_ms": (
                    int(params["autoResolutionMs"])
                    if isinstance(params.get("autoResolutionMs"), int)
                    else None
                ),
                "future": future,
                "responded": False,
                "resolution": "dismissed",
            }
            async with CODEX_PENDING_INTERACTIONS_LOCK:
                if (
                    len(CODEX_PENDING_INTERACTIONS)
                    < MAX_CODEX_PENDING_INTERACTIONS
                ):
                    CODEX_PENDING_INTERACTIONS[interaction_id] = candidate
                    pending = candidate
            if pending is not None:
                current_task = asyncio.current_task()
                if current_task is not None:
                    register_session_task(
                        CODEX_INTERACTION_HANDLER_TASKS,
                        session_id,
                        current_task,
                    )
    if pending is None:
        return await decline_server_request(request_id, method, params)
    future = pending["future"]
    interaction_id = str(pending["id"])

    try:
        await update_codex_pending_session_metadata(session_id)
        await append_event(
            session_id,
            "codex_interaction_requested",
            {"interaction": public_codex_interaction(pending)},
        )
        timeout_ms = pending.get("auto_resolution_ms")
        if method == "item/tool/requestUserInput" and timeout_ms is not None:
            try:
                result = await asyncio.wait_for(
                    asyncio.shield(future),
                    timeout=max(0.001, timeout_ms / 1000),
                )
            except asyncio.TimeoutError:
                fallback = await decline_server_request(request_id, method, params)
                async with CODEX_PENDING_INTERACTIONS_LOCK:
                    current = CODEX_PENDING_INTERACTIONS.get(interaction_id)
                    won = (
                        current is pending
                        and finish_codex_interaction_locked(
                            pending,
                            resolution="auto_resolved",
                            response=fallback,
                        )
                    )
                result = fallback if won else await asyncio.shield(future)
        else:
            result = await asyncio.shield(future)
        return result
    except asyncio.CancelledError:
        async with CODEX_PENDING_INTERACTIONS_LOCK:
            current = CODEX_PENDING_INTERACTIONS.get(interaction_id)
            if current is pending and not pending.get("responded"):
                finish_codex_interaction_locked(
                    pending,
                    resolution="dismissed",
                    response=_CODEX_CANCEL_INTERACTION_FUTURE,
                )
        raise
    finally:
        async with CODEX_PENDING_INTERACTIONS_LOCK:
            current = CODEX_PENDING_INTERACTIONS.get(interaction_id)
            if current is pending:
                CODEX_PENDING_INTERACTIONS.pop(interaction_id, None)
        with suppress(Exception):
            await asyncio.shield(update_codex_pending_session_metadata(session_id))
        with suppress(Exception):
            await asyncio.shield(
                append_event(
                    session_id,
                    "codex_interaction_resolved",
                    {
                        "interaction_id": interaction_id,
                        "request_method": method,
                        "resolution": pending.get("resolution") or "dismissed",
                    },
                )
            )


def codex_thread_status_message(status: dict[str, Any]) -> str:
    status_type = str(status.get("type") or "")
    if status_type == "active":
        flags = [
            str(flag)
            for flag in status.get("activeFlags") or []
            if str(flag or "")
        ]
        if "waitingOnApproval" in flags:
            return "Codex is waiting for approval."
        return "Codex is working."
    if status_type == "idle":
        return "Codex is idle."
    if status_type == "notLoaded":
        return "Codex thread is not loaded."
    if status_type == "systemError":
        detail = concise_error_message(
            status.get("error") or status.get("message") or ""
        )
        return (
            f"Codex thread error: {detail[:500]}"
            if detail and detail != "Unknown error"
            else "Codex thread encountered an error."
        )
    return "Codex thread status changed."


async def project_codex_notification(notification: dict[str, Any]) -> None:
    """Project durable thread control state without duplicating turn output."""
    method = str(notification.get("method") or "")
    params = (
        notification.get("params")
        if isinstance(notification.get("params"), dict)
        else {}
    )
    thread_id = str(params.get("threadId") or "")
    started_thread = params.get("thread") if method == "thread/started" else None
    if method == "thread/started":
        if not isinstance(started_thread, dict):
            return
        started_id = started_thread.get("id")
        if not isinstance(started_id, str) or not started_id or (thread_id and thread_id != started_id):
            return
        thread_id = started_id
    session_id = codex_session_id_for_thread(thread_id)
    quarantined_session_id = CODEX_QUARANTINED_GOAL_THREADS.get(thread_id)
    root_thread_id = session_codex_thread_id(
        STORE.sessions.get(session_id) or {}
    )
    with CODEX_SUBAGENT_INDEX_LOCK:
        indexed_child_session_id = CODEX_SUBAGENT_SESSION_INDEX.get(thread_id)
    is_child_thread = bool(
        session_id
        and thread_id != root_thread_id
        and indexed_child_session_id == session_id
    )
    if method == "thread/name/updated" and not is_child_thread:
        title = native_session_title(params.get("threadName"))
        if session_id and not is_child_thread and thread_id == root_thread_id and title:
            await STORE.adopt_auto_title(
                session_id, title, source="provider", backend=BACKEND_CODEX,
                provider_id=thread_id,
            )
        return
    if method == "turn/started" and thread_id and not is_child_thread:
        # Stop may win while an ordinary parent is between native turns. A
        # child result can then start B while A's already-completed supervisor
        # is draining cleanup. Keep that new B under the same explicit Stop;
        # never leave it running merely because admission observed idle A.
        stopped_continuation = False
        stopped_continuation_turn: str | None = None
        value = params.get("turn") if isinstance(params.get("turn"), dict) else {}
        incoming_turn_id = str(params.get("turnId") or value.get("id") or "")

        def stopped_continuation_still_current() -> bool:
            current_handle = getattr(getattr(manager, "client", None), "_turns_by_thread", {}).get(thread_id)
            exact_live_turn = bool(
                current_handle is handle and not getattr(handle, "_closed", False)
                and not getattr(handle, "_completed", True)
                and getattr(handle, "turn_id", "") == incoming_turn_id
            )
            cleanup_gap = bool(getattr(handle, "_closed", False) and current_handle is None)
            return bool(
                incoming_turn_id and incoming_turn_id not in getattr(handle, "_completed_turn_ids", set())
                and RUNS.active.get(session_id) is stopping_owner
                and (RUNS.current_turns.get(session_id) or {}).get("run_id") == stopping_owner.get("run_id")
                and session_id in RUNS.busy and stopping_owner.get("stop_requested")
                and existing_codex_app_server_manager_for_thread(thread_id) is manager
                and type(getattr(handle, "transport_generation", None)) is int
                and handle.transport_generation > 0
                and handle.transport_generation == getattr(manager, "generation", None)
                and (exact_live_turn or cleanup_gap)
            )

        async with RUNS.active_lock:
            stopping_owner = RUNS.active.get(session_id)
            manager = existing_codex_app_server_manager_for_thread(thread_id)
            handle = (stopping_owner or {}).get("codex_app_server_turn")
            if (
                stopping_owner and stopping_owner.get("stop_requested")
                and stopping_owner.get("provider_thread_id") == thread_id
                and isinstance(stopping_owner.get("codex_child_continuation_stop"), asyncio.Event)
                and handle is not None and manager is not None
                and getattr(handle, "transport_generation", None) == getattr(manager, "generation", None)
                and (RUNS.current_turns.get(session_id) or {}).get("run_id") == stopping_owner.get("run_id")
                and session_id in RUNS.busy
            ):
                stopped_continuation = True
                if stopped_continuation_still_current() and (
                    stopping_owner.get("provider_turn_id") != incoming_turn_id
                    or not stopping_owner.get("native_interrupt_sent")
                ):
                    stopping_owner["provider_turn_id"] = incoming_turn_id
                    stopping_owner["native_interrupt_sent"] = True
                    stopped_continuation_turn = incoming_turn_id
        if stopped_continuation:
            if stopped_continuation_turn:
                try:
                    await manager.client._request_connected(
                        "turn/interrupt", {"threadId": thread_id, "turnId": stopped_continuation_turn},
                        timeout=CODEX_GOAL_CONTROL_TIMEOUT_SECONDS,
                        before_send=stopped_continuation_still_current,
                    )
                except Exception:
                    async with RUNS.active_lock:
                        if RUNS.active.get(session_id) is stopping_owner and stopping_owner.get("provider_turn_id") == stopped_continuation_turn:
                            stopping_owner["native_interrupt_sent"] = False
                    logger.warning("could not interrupt stopped Codex child continuation session=%s turn=%s", session_id, stopped_continuation_turn)
            return
        owner_session_id = session_id or quarantined_session_id
        if owner_session_id and not await codex_thread_has_local_run_owner(
            owner_session_id,
            thread_id,
        ):
            turn_value = (
                params.get("turn")
                if isinstance(params.get("turn"), dict)
                else {}
            )
            turn_id = str(params.get("turnId") or turn_value.get("id") or "")
            manager = existing_codex_app_server_manager_for_thread(thread_id)
            interrupt_ok = False
            pause_ok = False
            errors: list[str] = []
            if manager is None:
                errors.append("Codex app-server manager is unavailable")
            else:
                if turn_id:
                    try:
                        await manager.request(
                            "turn/interrupt",
                            {"threadId": thread_id, "turnId": turn_id},
                            timeout=CODEX_GOAL_CONTROL_TIMEOUT_SECONDS,
                        )
                        interrupt_ok = True
                    except Exception as exc:
                        errors.append(
                            f"interrupt failed: {concise_error_message(exc)}"
                        )
                else:
                    errors.append("turn/started omitted the turn id")
                try:
                    paused_goal = await asyncio.wait_for(
                        manager.set_thread_goal(thread_id, status="paused"),
                        timeout=CODEX_GOAL_CONTROL_TIMEOUT_SECONDS,
                    )
                    pause_ok = bool(
                        isinstance(paused_goal, dict)
                        and str(paused_goal.get("status") or "") == "paused"
                    )
                    if not pause_ok:
                        errors.append("goal pause returned non-paused state")
                    elif session_id:
                        await persist_reconciled_codex_goal(
                            session_id,
                            thread_id,
                            paused_goal,
                        )
                except Exception as exc:
                    errors.append(
                        f"goal pause failed: {concise_error_message(exc)}"
                    )
            if session_id and (not interrupt_ok or not pause_ok):
                await quarantine_codex_goal_thread(
                    session_id,
                    thread_id,
                    reason=(
                        "Ownerless Codex goal continuation could not be fully "
                        "stopped: " + "; ".join(errors)
                    ),
                )
            logger.warning(
                "rejected ownerless Codex turn session=%s thread=%s turn=%s "
                "interrupt_ok=%s pause_ok=%s errors=%s",
                owner_session_id,
                thread_id,
                turn_id,
                interrupt_ok,
                pause_ok,
                "; ".join(errors),
            )
            # Never project an ownerless provider continuation as chat work.
            return
        # Goal activation can publish a native turn before its RPC returns.
        # Bind that turn to the explicit resume reservation immediately so
        # Stop and failed-RPC cleanup can interrupt it before the consumer runs.
        async with RUNS.active_lock:
            active = RUNS.active.get(session_id)
            if (
                active
                and active.get("codex_native_operation_kind") == "goal_resume"
                and str(active.get("provider_thread_id") or "") == thread_id
            ):
                turn_value = params.get("turn")
                turn_id = str(
                    params.get("turnId")
                    or (turn_value.get("id") if isinstance(turn_value, dict) else "")
                    or ""
                )
                if turn_id:
                    if active.get("provider_turn_id") != turn_id:
                        active["native_interrupt_sent"] = False
                    active["provider_turn_id"] = turn_id
                    active["provider_turn_ready"] = True
    if not session_id:
        return

    if method in {"thread/name/updated", "thread/started"}:
        # A provider title is identity, not new work or a child lifecycle tick.
        # Root/unknown threads cannot create a child through a rename packet.
        title = (started_thread.get("name", ...) if isinstance(started_thread, dict)
                 else params.get("threadName", ...))
        if not is_child_thread or not (title is None or isinstance(title, str)):
            return
        with CODEX_SUBAGENT_INDEX_LOCK:
            previous = dict(CODEX_SUBAGENT_STATE.get(thread_id) or {})
        if not previous or previous.get("session_id") != session_id:
            return
        await emit_codex_subagent_state(
            session_id,
            thread_id,
            previous.get("subagent_status"),
            title=title,
            run_id=previous.get("run_id"),
            inherit_run_id=False,
            identity_only=True,
        )
        return

    if method in {"item/started", "item/completed"}:
        item = params.get("item") if isinstance(params.get("item"), dict) else {}
        if str(item.get("type") or "") in {
            "collabAgentToolCall",
            "collabToolCall",
            "subAgentActivity",
        }:
            turn_id = str(params.get("turnId") or "") or None
            run_id = await codex_notification_run_id(
                session_id,
                thread_id,
                turn_id,
            )
            await project_codex_subagent_item(
                session_id,
                thread_id,
                item,
                run_id=run_id,
                completed=method == "item/completed",
            )
            return

    if is_child_thread:
        child_status: str | None = None
        child_activity = ""
        if method == "thread/status/changed":
            child_status = codex_child_status_from_thread(params.get("status"))
            if not child_status:
                return
            child_activity = f"Subagent {child_status}"
        elif method == "turn/completed":
            completed_turn = params.get("turn") if isinstance(params.get("turn"), dict) else {}
            child_status = str(completed_turn.get("status") or "failed")
            child_activity = f"Subagent {normalize_subagent_status(child_status)}"
        elif method == "thread/closed":
            child_status = "stopped"
            child_activity = "Subagent closed"
        else:
            # Child threads share their owning chat for lifecycle cards, but
            # their context window and native control items are
            # provider-local. Falling through here used to overwrite the
            # parent's context meter and render child context compactions as
            # top-level chat landmarks.
            return
        if await codex_session_has_active_run(session_id):
            await emit_codex_subagent_state(
                session_id,
                thread_id,
                child_status,
                activity=child_activity,
            )
            return
        # The parent chat is idle. A child that reports itself active now is
        # provider-local churn (a loaded rollout, a late replay), not chat
        # work: never manufacture a running card for a finished run. Terminal
        # transitions are still recorded once, detached from the finished
        # run's id.
        normalized_status = normalize_subagent_status(child_status)
        if normalized_status in {"starting", "running"}:
            return
        with CODEX_SUBAGENT_INDEX_LOCK:
            previous_status = (CODEX_SUBAGENT_STATE.get(thread_id) or {}).get(
                "subagent_status"
            )
        if (
            previous_status is not None
            and normalize_subagent_status(previous_status) == normalized_status
        ):
            return
        await emit_codex_subagent_state(
            session_id,
            thread_id,
            child_status,
            run_id=None,
            activity=child_activity,
            inherit_run_id=False,
        )
        return

    if method == "thread/status/changed":
        status = bounded_codex_interaction_value(params.get("status"), remaining=4_000)
        if not isinstance(status, dict):
            return
        changed = False
        async with STORE.transaction():
            session = STORE.sessions.get(session_id)
            if session and session.get("codex_thread_status") != status:
                session["codex_thread_status"] = status
                changed = True
        if changed:
            await append_event(
                session_id,
                "codex_thread_status",
                {
                    "status": status,
                    "message": codex_thread_status_message(status),
                },
            )
        return

    if method == "thread/goal/updated":
        goal = bounded_codex_interaction_value(params.get("goal"), remaining=12_000)
        if not isinstance(goal, dict):
            return
        if not CODEX_GOALS_ENABLED and str(goal.get("status") or "").lower() == "active":
            # A late notification from the manager generation being replaced
            # must never resurrect automatic continuation after the server-wide
            # policy was disabled.
            goal = {**goal, "status": "paused"}
        async with STORE.transaction():
            session = STORE.sessions.get(session_id)
            if session:
                session["codex_goal"] = goal
                if codex_goal_time_budget_is_exhausted(session):
                    session["codex_goal_time_budget_exhausted"] = True
                # Native goal notifications are durable control state. Persist
                # them without changing updated_at so routine progress does not
                # reorder the chat list.
                await STORE.save()
        await append_event(session_id, "codex_goal_updated", {"goal": goal})
        return

    if method == "thread/goal/cleared":
        async with STORE.transaction():
            session = STORE.sessions.get(session_id)
            if session:
                session["codex_goal"] = None
                session["codex_goal_time_budget_seconds"] = None
                session["codex_goal_time_budget_exhausted"] = False
                # Clearing a goal must survive a restart even though the
                # lifecycle notification is omitted from semantic history.
                await STORE.save()
        await append_event(session_id, "codex_goal_cleared", {})
        return

    if method == "thread/tokenUsage/updated":
        turn_id = str(params.get("turnId") or "") or None
        run_id = await codex_notification_run_id(
            session_id,
            thread_id,
            turn_id,
        )
        if run_id is None:
            previous_snapshot = current_codex_token_usage_snapshot(session_id)
            if (
                previous_snapshot is not None
                and str(previous_snapshot.get("thread_id") or "") == thread_id
                and (str(previous_snapshot.get("turn_id") or "") or None)
                == turn_id
            ):
                run_id = str(previous_snapshot.get("run_id") or "") or None
            if run_id is None:
                run_id = terminal_codex_token_usage_run_id(
                    session_id,
                    thread_id,
                    turn_id,
                )
        if run_id is None:
            # Global app-server notifications can arrive after the chat has
            # switched to codex exec. Never let an unattributed late sample
            # repopulate telemetry that the compatibility transport invalidated.
            return
        snapshot = codex_token_usage_snapshot(
            params.get("tokenUsage"),
            thread_id=thread_id,
            turn_id=turn_id,
            run_id=run_id,
        )
        if snapshot is None:
            return
        async with RUNS.active_lock:
            active = RUNS.active.get(session_id)
            if (
                active
                and active.get("codex_compaction_in_progress")
                and (
                    not active.get("provider_thread_id")
                    or str(active.get("provider_thread_id")) == thread_id
                )
            ):
                active["codex_compaction_token_usage_after"] = dict(snapshot)
        await record_codex_token_usage(session_id, snapshot)
        return

    if method == "turn/completed":
        completed_turn = (
            params.get("turn")
            if isinstance(params.get("turn"), dict)
            else {}
        )
        turn_id = str(
            params.get("turnId") or completed_turn.get("id") or ""
        ) or None
        run_id = await codex_notification_run_id(
            session_id,
            thread_id,
            turn_id,
        )
        if completed_turn.get("status") in {"failed", "interrupted"}:
            failed_compaction = None
            async with RUNS.active_lock:
                active = RUNS.active.get(session_id)
                if (
                    active and not active.get("codex_native_operation")
                    and active.get("codex_compaction_in_progress")
                    and active.get("provider_thread_id") == thread_id
                    and active.get("codex_compaction_turn_id") == turn_id
                ):
                    failed_compaction = {
                        "run_id": active.get("run_id"), "thread_id": thread_id,
                        "turn_id": turn_id, "item_id": active.get("codex_compaction_item_id"),
                        "token_usage_before": active.get("codex_compaction_token_usage_before"),
                        "token_usage_after": None,
                        "status": completed_turn["status"],
                        "message": "Codex automatic context compaction did not complete.",
                    }
                    for key in ("codex_compaction_in_progress", "codex_compaction_turn_id",
                                "codex_compaction_item_id", "codex_compaction_token_usage_before",
                                "codex_compaction_token_usage_after"):
                        active.pop(key, None)
            if failed_compaction is not None:
                item_id = failed_compaction["item_id"]
                if not codex_native_compaction_was_terminal(session_id, thread_id, turn_id, item_id):
                    failed_compaction["compaction_id"] = native_codex_compaction_id(thread_id, turn_id, item_id)
                    await append_event(session_id, "codex_compaction_completed", failed_compaction)
                    remember_codex_native_compaction_terminal(session_id, thread_id, turn_id, item_id)
        if turn_id is not None and run_id is not None:
            async with STORE.transaction():
                session = STORE.sessions.get(session_id)
                if session is not None:
                    # App-server dispatches notification handlers
                    # independently. Remember the terminal turn before reading
                    # its current snapshot so a usage notification that
                    # completes afterwards is also force-checkpointed.
                    terminal_marker = {
                        "thread_id": thread_id,
                        "turn_id": turn_id,
                    }
                    if run_id is not None:
                        terminal_marker["run_id"] = run_id
                    session["_codex_token_usage_terminal"] = terminal_marker
        snapshot = current_codex_token_usage_snapshot(session_id)
        snapshot_turn_id = (
            str(snapshot.get("turn_id") or "") or None
            if snapshot is not None
            else None
        )
        # Never relabel a stale prior-turn sample when Codex completes a turn
        # without publishing fresh usage for it.
        if snapshot is not None and (
            turn_id is None or snapshot_turn_id == turn_id
        ):
            snapshot.update(
                {
                    "thread_id": thread_id,
                    "turn_id": turn_id,
                }
            )
            # ACTIVE may already have been released by the ordinary turn
            # consumer. Preserve the sample's known attribution in that race.
            if run_id is not None:
                snapshot["run_id"] = run_id
            await record_codex_token_usage(
                session_id,
                snapshot,
                force_checkpoint=True,
            )
        return

    if method in {"item/started", "item/completed"}:
        item = (
            params.get("item")
            if isinstance(params.get("item"), dict)
            else {}
        )
        if str(item.get("type") or "") != "contextCompaction":
            return
        turn_id = str(params.get("turnId") or "") or None
        item_id = str(params.get("itemId") or item.get("id") or "") or None
        if method == "item/started":
            before = current_codex_token_usage_snapshot(session_id)
            explicit_native_compaction = False
            terminal_replay = False
            run_id = ""
            async with RUNS.active_lock:
                active = RUNS.active.get(session_id)
                if active and (
                    not active.get("provider_thread_id")
                    or str(active.get("provider_thread_id")) == thread_id
                ):
                    explicit_native_compaction = bool(
                        active.get("codex_native_operation")
                        and active.get("codex_native_operation_kind")
                        == "compaction"
                    )
                    run_id = str(active.get("run_id") or "")
                    turn_id = (
                        turn_id
                        or str(active.get("codex_compaction_turn_id") or "")
                        or str(active.get("provider_turn_id") or "")
                        or None
                    )
                    terminal_replay = (
                        not explicit_native_compaction
                        and codex_native_compaction_was_terminal(
                            session_id,
                            thread_id,
                            turn_id,
                            item_id,
                        )
                    )
                    if not terminal_replay:
                        active["codex_compaction_in_progress"] = True
                        active["codex_compaction_item_id"] = item_id
                        active["codex_compaction_turn_id"] = turn_id
                        if "codex_compaction_token_usage_before" not in active:
                            active["codex_compaction_token_usage_before"] = (
                                dict(before) if before is not None else None
                            )
            if terminal_replay:
                # Do not mutate ACTIVE for a replayed start. A matching
                # completion is not guaranteed to follow after thread resume.
                return
            if not explicit_native_compaction:
                compaction_id = native_codex_compaction_id(
                    thread_id,
                    turn_id,
                    item_id,
                )
                if codex_native_compaction_was_terminal(
                    session_id,
                    thread_id,
                    turn_id,
                    item_id,
                ):
                    # A reloaded thread can replay both lifecycle edges after
                    # the manual owner has already persisted its terminal row.
                    # Suppress the late start too, otherwise it becomes an
                    # orphan "Compacting" marker with no matching completion.
                    return
                await append_event(
                    session_id,
                    "codex_compaction_started",
                    {
                        "run_id": run_id or None,
                        "compaction_id": compaction_id,
                        "thread_id": thread_id,
                        "turn_id": turn_id,
                        "item_id": item_id,
                        "status": "in_progress",
                        "token_usage_before": before,
                        "message": "Codex started automatic context compaction.",
                    },
                )
            return
        async with RUNS.active_lock:
            active = RUNS.active.get(session_id)
            explicit_native_compaction = bool(
                active
                and active.get("codex_native_operation")
                and active.get("codex_native_operation_kind") == "compaction"
            )
            run_id = str(active.get("run_id") or "") if active else ""
            if active:
                turn_id = (
                    turn_id
                    or str(active.get("codex_compaction_turn_id") or "")
                    or str(active.get("provider_turn_id") or "")
                    or None
                )
                item_id = (
                    item_id
                    or str(active.get("codex_compaction_item_id") or "")
                    or None
                )
            if active and "codex_compaction_token_usage_before" in active:
                recorded_before = active.get(
                    "codex_compaction_token_usage_before"
                )
                before = (
                    dict(recorded_before)
                    if isinstance(recorded_before, dict)
                    else None
                )
            else:
                before = current_codex_token_usage_snapshot(session_id)
            after = (
                dict(active["codex_compaction_token_usage_after"])
                if active
                and isinstance(
                    active.get("codex_compaction_token_usage_after"), dict
                )
                else None
            )
            if active and not explicit_native_compaction:
                for key in (
                    "codex_compaction_in_progress",
                    "codex_compaction_item_id",
                    "codex_compaction_turn_id",
                    "codex_compaction_token_usage_before",
                    "codex_compaction_token_usage_after",
                ):
                    active.pop(key, None)
        native_compaction_id = native_codex_compaction_id(
            thread_id,
            turn_id,
            item_id,
        )
        if explicit_native_compaction:
            # Its owner emits one terminal event after canonical
            # turn/completed, while the chat remains reserved.
            remember_codex_native_compaction_terminal(
                session_id,
                thread_id,
                turn_id,
                item_id,
            )
            return
        if codex_native_compaction_was_terminal(
            session_id,
            thread_id,
            turn_id,
            item_id,
        ):
            return
        if after is not None:
            await record_codex_token_usage(
                session_id,
                after,
                force_checkpoint=True,
            )
        await append_event(
            session_id,
            "codex_compaction_completed",
            {
                "run_id": run_id or None,
                "compaction_id": native_compaction_id,
                "thread_id": thread_id,
                "turn_id": turn_id,
                "item_id": item_id,
                "status": "completed",
                "token_usage_before": before,
                "token_usage_after": after,
                "message": "Codex completed automatic context compaction.",
            },
        )


def codex_manager_owns_notification(manager: CodexAppServerManager, notification: dict[str, Any]) -> bool:
    """Pure source check: callbacks must not lease their long-lived reader."""
    if not any(manager is item for item in codex_app_server_managers()):
        return False
    params = notification.get("params")
    if not isinstance(params, dict):
        return False
    thread = params.get("thread") if isinstance(params.get("thread"), dict) else {}
    thread_id = str(params.get("threadId") or thread.get("id") or "")
    session_id = codex_session_id_for_thread(thread_id)
    if session_id is None and thread:
        _name, _path, parent = codex_subagent_thread_identity(thread)
        session_id = codex_session_id_for_thread(parent) if parent else None
    if session_id:
        owner = CODEX_SESSION_APP_SERVER_MANAGERS.get(session_id)
        if owner is not None:
            return owner is manager
        # An unsubscribed old native thread can still emit late events. Once
        # its session lease is gone it may never mutate the replacement state.
        return not any(manager is item for item in CODEX_RETIRED_APP_SERVER_MANAGERS)
    return True  # Newly created native identities are not bound to a chat yet.


def codex_binary_identity() -> tuple[Any, ...] | None:
    """Probe the launcher and its version: npm wrappers can stay unchanged."""
    try:
        executable = shutil.which(CODEX_BIN, path=codex_app_server_env().get("PATH"))
        if not executable:
            return None
        resolved = Path(executable).resolve()
        stamp = resolved.stat()
        result = runtime_command([executable, "--version"])
        version = safe_runtime_version(result.stdout or result.stderr)
        if result.returncode or not version:
            return None
        return (str(resolved), stamp.st_dev, stamp.st_ino, stamp.st_size,
                stamp.st_mtime_ns, version)
    except (OSError, subprocess.SubprocessError):
        return None


def retain_codex_manager_caller(manager: CodexAppServerManager, session_id: str) -> None:
    """Cover the gap between choosing a process and registering its request."""
    try:
        task = asyncio.current_task()
    except RuntimeError:
        return  # Read-only catalog helpers can run outside the event loop.
    if task is None or task is PROVIDERS.codex_manager_drain_task:
        return
    users = getattr(manager, "_hyadex_callers", None)
    if not isinstance(users, weakref.WeakKeyDictionary):
        users = manager._hyadex_callers = weakref.WeakKeyDictionary()
    if task not in users:
        task.add_done_callback(lambda _task: schedule_codex_manager_drain())
    users[task] = session_id


def codex_manager_has_callers(manager: CodexAppServerManager, session_id: str | None = None, *, ignore_task=None) -> bool:
    return any(task is not ignore_task and not task.done() and (session_id is None or not owner or owner == session_id)
               for task, owner in tuple(getattr(manager, "_hyadex_callers", {}).items()))


def codex_manager_work_belongs_to_session(work: Any, session_id: str) -> bool:
    thread_id = getattr(work, "_codex_thread_id", "")
    owner = codex_session_id_for_thread(thread_id) if thread_id else None
    # Unknown ownership stays process-wide. Work for a known different chat
    # must not prevent an idle chat from releasing its own subscription.
    return owner is None or owner == session_id


def codex_manager_has_callbacks(manager: CodexAppServerManager, session_id: str | None = None) -> bool:
    tasks = [task for task in manager.client._callback_tasks if not task.done()
             and (session_id is None or codex_manager_work_belongs_to_session(task, session_id))]
    for task in tasks:
        if not getattr(task, "_hyadex_drain_watched", False):
            task._hyadex_drain_watched = True
            task.add_done_callback(lambda _task: schedule_codex_manager_drain())
    return bool(tasks)


def schedule_codex_manager_drain(_notification: Any = None) -> None:
    if PROVIDERS.codex_manager_closing or not CODEX_RETIRED_APP_SERVER_MANAGERS:
        return
    if isinstance(_notification, dict) and _notification.get("method") not in {
        "turn/completed", "thread/closed", "thread/goal/updated", "thread/status/changed",
    }:
        return
    PROVIDERS.codex_manager_drain_requested = True
    if PROVIDERS.codex_manager_drain_task is None or PROVIDERS.codex_manager_drain_task.done():
        PROVIDERS.codex_manager_drain_task = asyncio.create_task(drain_retired_codex_managers())


async def refresh_codex_app_server_binary(*, force: bool = False) -> None:
    """Change admission, never interrupt a process that still owns work."""
    if CODEX_TRANSPORT == CODEX_TRANSPORT_EXEC or PROVIDERS.codex_manager_closing:
        return
    async with CODEX_BINARY_PROBE_LOCK:
        if not force and time.monotonic() - PROVIDERS.codex_binary_checked_at < 30.0:
            return
        identity = await asyncio.to_thread(codex_binary_identity)
        PROVIDERS.codex_binary_checked_at = time.monotonic()
        if identity is None:
            return  # A failed/mid-install probe must not disturb working clients.
        async with CODEX_GOALS_CONFIG_LOCK:
            async with CODEX_APP_SERVER_MANAGER_LOCK:
                if PROVIDERS.codex_manager_cleanup_epoch is not None or PROVIDERS.codex_manager_closing:
                    return
                previous = PROVIDERS.codex_binary_identity
                PROVIDERS.codex_binary_identity = identity
                manager = PROVIDERS.codex_manager
                launched = getattr(manager, "_hyadex_binary_identity", None) or previous
                if manager is not None and launched is not None and launched != identity:
                    CODEX_RETIRED_APP_SERVER_MANAGERS.append(manager)
                    PROVIDERS.codex_manager = None
    schedule_codex_manager_drain()


async def refresh_codex_app_server_login(*, source: str = "manager_lookup") -> bool:
    """Change normal-Codex admission; never kill a credential-owning process.

    Lookups, turn admission and catalog refreshes observe the native login
    files, so a `codex login` in the user's terminal moves new turns to a fresh
    app-server once the old one is idle. This is not a token refresh or login.
    """
    if CODEX_TRANSPORT == CODEX_TRANSPORT_EXEC or PROVIDERS.codex_manager_closing:
        return False
    # Sources are server-owned labels, never request text, account IDs or keys.
    if source not in {"manager_lookup", "turn_admission", "catalog_refresh"}:
        source = "manager_lookup"
    async with CODEX_LOGIN_PROBE_LOCK:
        if not await asyncio.to_thread(codex_auth.native_login_handoff_supported,
                codex_app_server_env(), cwd=existing_cwd(DEFAULT_CWD)):
            return False
        revision = await asyncio.to_thread(codex_auth.native_login_revision,
            codex_app_server_env(), cwd=existing_cwd(DEFAULT_CWD))
        async with CODEX_GOALS_CONFIG_LOCK:
            async with CODEX_APP_SERVER_MANAGER_LOCK:
                if PROVIDERS.codex_manager_cleanup_epoch is not None or PROVIDERS.codex_manager_closing:
                    return False
                observed_change = revision is not None and revision != PROVIDERS.codex_login_revision
                PROVIDERS.codex_login_revision = revision
                if observed_change:
                    expire_codex_login_diagnostic()
                for manager in codex_app_server_managers():
                    previous = getattr(manager, "_hyadex_login_revision", None)
                    changed = revision is not None and revision != previous
                    # Recheck CLIs is observational when the native sign-in
                    # revision is unchanged. Retiring a healthy writer here
                    # strands its chats behind an unnecessary login handoff.
                    if revision is not None and revision == previous:
                        manager._hyadex_login_superseded = False
                    if changed:
                        if getattr(manager, "_hyadex_login_superseded", False) is not True:
                            manager._hyadex_login_handoff_source = source
                            logger.info("Codex login handoff requested source=%s reason=login_revision_changed manager=%x", source, id(manager))
                        manager._hyadex_login_superseded = True
                        if not any(manager is item for item in CODEX_RETIRED_APP_SERVER_MANAGERS):
                            CODEX_RETIRED_APP_SERVER_MANAGERS.append(manager)
                        if PROVIDERS.codex_manager is manager:
                            PROVIDERS.codex_manager = None
    schedule_codex_manager_drain()
    return True


def codex_manager_session_busy(manager: CodexAppServerManager, session_id: str, *, ignore_task=None, ignore_maintenance=False) -> bool:
    session = STORE.sessions.get(session_id) or {}
    return bool(
        session_id in RUNS.busy or RUNS.active.get(session_id) is not None
        or (not ignore_maintenance and session_id in RUNS.maintenance)
        or codex_manager_has_callers(manager, session_id, ignore_task=ignore_task)
        or codex_manager_has_callbacks(manager, session_id)
        or any(task is not ignore_task and not task.done()
            for registry in (RUNS.turn_tasks, CODEX_NATIVE_ACTION_TASKS, CODEX_INTERACTION_HANDLER_TASKS)
            for task in registry.get(session_id, ()))
        or any(item.get("session_id") == session_id for item in CODEX_PENDING_INTERACTIONS.values())
        or session_id in SIDE_QUESTIONS.active_session_ids()
        or (isinstance(session.get("codex_goal"), dict)
            and session["codex_goal"].get("status") == "active")
        or codex_session_has_live_subagents(session_id)
    )


async def release_idle_codex_manager_session(manager: CodexAppServerManager, session_id: str, *, ignore_task=None) -> bool:
    """Called under the session lifecycle lock; retain ownership on any doubt."""
    def retained(reason: str) -> bool:
        if getattr(manager, "_hyadex_login_superseded", False) is True:
            reasons = getattr(manager, "_hyadex_login_handoff_blockers", None)
            if reasons is None:
                reasons = manager._hyadex_login_handoff_blockers = {}
            if reasons.get(session_id) != reason:
                reasons[session_id] = reason
                logger.info("Codex login handoff waiting source=%s manager=%x session=%s reason=%s",
                    getattr(manager, "_hyadex_login_handoff_source", "unknown"), id(manager), session_id, reason)
        return False

    def blocked(*, own_maintenance=False):
        return (codex_manager_session_busy(manager, session_id, ignore_task=ignore_task,
                    ignore_maintenance=own_maintenance)
                or any(codex_manager_work_belongs_to_session(request[1], session_id)
                       for request in manager.client._pending.values())
                or any(codex_manager_work_belongs_to_session(task, session_id)
                       for task in manager.client._server_request_tasks.values() if not task.done()))

    if blocked():
        return retained("owned_work")
    deadline = time.monotonic() + 8.0
    RUNS.maintenance.add(session_id)
    try:
        threads = [thread for thread in tuple(manager.client._loaded_threads)
                   if codex_session_id_for_thread(thread) == session_id]
        for thread in threads:
            if time.monotonic() >= deadline:
                return retained("metadata_deadline")
            if (thread in CODEX_APP_SERVER_PINNED_THREADS or thread in CODEX_INTERACTIVE_CONTROL_THREADS
                    or manager.active_turn(thread) is not None):
                return retained("active_or_pinned_thread")
            try:
                goal = await asyncio.wait_for(manager.get_thread_goal(thread), timeout=min(3.0, deadline - time.monotonic()))
            except CodexAppServerRequestError as exc:
                if exc.code not in {-32600, -32601}:
                    raise
                goal = None
            if isinstance(goal, dict) and goal.get("status") == "active":
                return retained("active_goal")
            try:
                terminals = await asyncio.wait_for(manager.list_background_terminals(thread), timeout=min(3.0, deadline - time.monotonic()))
            except CodexAppServerRequestError as exc:
                if exc.code not in {-32600, -32601}:
                    raise
                terminals = []
            # Those metadata requests yielded. A new borrower or native
            # request may now own the process even without a registered turn.
            if terminals or blocked(own_maintenance=True):
                return retained("background_terminals" if terminals else "owned_work")
            evicted = await asyncio.wait_for(evict_codex_app_server_thread(manager, thread, reinsert_on_failure=True),
                timeout=max(0.0, deadline - time.monotonic()))
            if not evicted or manager.is_thread_loaded(thread):
                return retained("thread_not_unloaded")
        if blocked(own_maintenance=True):
            return retained("owned_work")
        if CODEX_SESSION_APP_SERVER_MANAGERS.get(session_id) is manager:
            CODEX_SESSION_APP_SERVER_MANAGERS.pop(session_id, None)
            CODEX_GOAL_SYNC_GENERATIONS.pop(session_id, None)
        if getattr(manager, "_hyadex_login_superseded", False) is True:
            getattr(manager, "_hyadex_login_handoff_blockers", {}).pop(session_id, None)
            logger.info("Codex login handoff released source=%s manager=%x session=%s",
                getattr(manager, "_hyadex_login_handoff_source", "unknown"), id(manager), session_id)
        return True
    except Exception:
        return retained("metadata_or_unload_failed")
    finally:
        RUNS.maintenance.discard(session_id)


async def prepare_codex_login_turn(session: dict[str, Any]) -> None:
    """Pre-admission handoff under the chat lock, before message acceptance."""
    if CODEX_TRANSPORT == CODEX_TRANSPORT_EXEC:
        return
    await refresh_codex_app_server_login(source="turn_admission")
    session_id = str(session.get("id") or "")
    manager = existing_codex_app_server_manager(session)
    if getattr(manager, "_hyadex_login_superseded", False) is not True:
        return
    # Busy messages retain the normal durable queue path. Promotion repeats
    # this preflight after the accepted prior turn has settled; no replay.
    if session_id in RUNS.busy or RUNS.active.get(session_id) is not None:
        return
    if not await release_idle_codex_manager_session(manager, session_id, ignore_task=asyncio.current_task()):
        raise TransientAdmissionWait(409, codex_auth.HANDOFF_MESSAGE)
    schedule_codex_manager_drain()


async def drain_retired_codex_managers() -> None:
    """Event-driven, bounded idle cleanup; failed proof retains the old owner."""
    while PROVIDERS.codex_manager_drain_requested:
        PROVIDERS.codex_manager_drain_requested = False
        if CODEX_GOALS_RECONFIGURING or PROVIDERS.codex_manager_closing:
            return
        if PROVIDERS.codex_manager_cleanup_epoch is not None:
            return
        for manager in tuple(CODEX_RETIRED_APP_SERVER_MANAGERS):
            for session_id, owner in tuple(CODEX_SESSION_APP_SERVER_MANAGERS.items()):
                if owner is not manager or codex_manager_session_busy(manager, session_id):
                    continue
                lock = session_lifecycle_lock(session_id)
                if lock.locked():
                    continue  # Never invert the session -> manager lock order.
                async with lock:
                    await release_idle_codex_manager_session(manager, session_id)
            client = manager.client
            if (any(owner is manager for owner in CODEX_SESSION_APP_SERVER_MANAGERS.values())
                    or codex_manager_has_callers(manager) or client._loaded_threads
                    or client._pending or client._server_request_tasks or codex_manager_has_callbacks(manager)
                    or any(not turn._completed for turn in client._turns_by_thread.values())):
                continue
            # Keep the exact object registered until close has settled;
            # shutdown can still discover and close it on cancellation.
            async with client._start_lock:
                # Recheck after waiting: a pending resume/start or caller can
                # appear without a native turn. Never discard that generation.
                if (codex_manager_has_callers(manager) or client._loaded_threads
                        or client._pending or client._server_request_tasks or codex_manager_has_callbacks(manager)
                        or any(not turn._completed for turn in client._turns_by_thread.values())):
                    continue
                await manager.close()
                CODEX_RETIRED_APP_SERVER_MANAGERS.remove(manager)


def existing_codex_app_server_manager_for_thread(thread_id: str) -> CodexAppServerManager | None:
    session_id = codex_session_id_for_thread(thread_id)
    if session_id and session_id in STORE.sessions:
        return existing_codex_app_server_manager(STORE.sessions[session_id])
    return next((item for item in codex_app_server_managers() if item.is_thread_loaded(thread_id)), None)


async def codex_app_server_manager_for_thread(thread_id: str) -> CodexAppServerManager:
    session_id = codex_session_id_for_thread(thread_id)
    if session_id and session_id in STORE.sessions:
        return await codex_app_server_manager(STORE.sessions[session_id], allow_retired_login=True)
    owner = existing_codex_app_server_manager_for_thread(thread_id)
    if owner is not None:
        retain_codex_manager_caller(owner, "")
        return owner
    return await codex_app_server_manager(None)


async def prepare_codex_app_server_process(manager: CodexAppServerManager) -> None:
    manager._hyadex_binary_identity = await asyncio.to_thread(codex_binary_identity)
    manager._hyadex_login_revision = await asyncio.to_thread(codex_auth.native_login_revision,
        codex_app_server_env(), cwd=existing_cwd(DEFAULT_CWD))


async def broadcast_codex_usage_changed(manager: CodexAppServerManager) -> None:
    for session_id, session in tuple(STORE.sessions.items()):
        if (str(session.get("backend") or DEFAULT_BACKEND).lower() == BACKEND_CODEX
                and existing_codex_app_server_manager(session) is manager):
            await broadcast_provider_usage_changed(session_id, BACKEND_CODEX)


def project_codex_usage_notification(manager: CodexAppServerManager, notification: dict[str, Any]):
    # This synchronous cache update happens before the following account RPC
    # can resolve. Only a content-free invalidation goes to session sockets.
    method = notification.get("method")
    if method == "account/changed":
        PROVIDER_USAGE.invalidate_codex(manager)
    elif method != "account/rateLimits/updated" or not PROVIDER_USAGE.observe_codex(manager, notification):
        return None
    return broadcast_codex_usage_changed(manager)


async def codex_app_server_manager(sess: dict[str, Any] | None = None, *, allow_retired_login: bool = False) -> CodexAppServerManager:
    """Keep normal Codex stable; custom credentials own immutable managers."""
    ensure_provider_manager_factory_admission(codex=True)
    await refresh_codex_app_server_binary()
    await refresh_codex_app_server_login()
    session_id = str((sess or {}).get("id") or "")
    # Manager lookup shares the provider reconfiguration barrier, including the
    # fast path, so a sign-in reload cannot race a retained/created generation.
    async with CODEX_GOALS_CONFIG_LOCK:
        ensure_provider_manager_factory_admission(codex=True)
        manager = existing_codex_app_server_manager(sess) if sess is not None else None
        if manager is not None and not any(manager is item for item in codex_app_server_managers()):
            CODEX_SESSION_APP_SERVER_MANAGERS.pop(session_id, None)
            manager = None
        manager = manager or PROVIDERS.codex_manager
        if manager is not None:
            if (getattr(manager, "_hyadex_login_superseded", False) is True and not allow_retired_login
                    and getattr(manager, "_hyadex_callers", {}).get(asyncio.current_task()) != session_id):
                schedule_codex_manager_drain()
                raise TransientAdmissionWait(409, codex_auth.HANDOFF_MESSAGE)
            if session_id:
                CODEX_SESSION_APP_SERVER_MANAGERS[session_id] = manager
            retain_codex_manager_caller(manager, session_id)
            schedule_codex_manager_drain()
            return manager
        async with CODEX_APP_SERVER_MANAGER_LOCK:
            ensure_provider_manager_factory_admission(codex=True)
            manager = PROVIDERS.codex_manager
            if manager is None:
                manager = CodexAppServerManager(
                    CODEX_BIN,
                    cwd=existing_cwd(DEFAULT_CWD),
                    env_factory=codex_app_server_env,
                    app_server_args=() if CODEX_GOALS_ENABLED else ("--disable", "goals"),
                    before_start=lambda: prepare_codex_app_server_process(manager),
                    request_timeout=CODEX_APP_SERVER_TIMEOUT_SECONDS,
                    lifecycle_timeout=CODEX_APP_SERVER_LIFECYCLE_TIMEOUT_SECONDS,
                    process_stream_limit=CODEX_APP_SERVER_JSONL_LIMIT_BYTES,
                    notification_queue_limit=(
                        CODEX_APP_SERVER_NOTIFICATION_QUEUE_LIMIT
                    ),
                    server_request_handler=lambda request_id, method, params: handle_codex_server_request(
                        request_id, method, params, source_manager=manager),
                    notification_guard=lambda notification: codex_manager_owns_notification(manager, notification),
                    initialize_params={
                        "clientInfo": {
                            "name": "hyadex_server",
                            "title": "HyadexServer",
                            "version": SERVER_VERSION,
                        },
                        "capabilities": {
                            "experimentalApi": True,
                            "mcpServerOpenaiFormElicitation": True,
                        },
                    },
                    on_process_started=register_codex_app_server_child,
                    on_process_exited=unregister_codex_app_server_child,
                )
                manager.add_notification_handler(project_codex_notification)
                usage_handler = lambda notification, manager=manager: project_codex_usage_notification(manager, notification)
                manager.client.add_account_usage_handler(usage_handler)
                manager.add_notification_handler(cache_codex_approval_item)
                manager.add_notification_handler(schedule_codex_manager_drain)
                manager._hyadex_binary_identity = PROVIDERS.codex_binary_identity
                manager._hyadex_login_revision = PROVIDERS.codex_login_revision
                PROVIDERS.codex_manager_epoch += 1
                PROVIDERS.codex_manager = manager
            if session_id:
                CODEX_SESSION_APP_SERVER_MANAGERS[session_id] = manager
            retain_codex_manager_caller(manager, session_id)
            return manager


async def close_codex_app_server_manager() -> None:
    if PROVIDERS.codex_manager_closing:
        raise RuntimeError("Codex provider cleanup is already in progress")
    PROVIDERS.codex_manager_closing = True
    try:
        PROVIDERS.codex_manager_drain_requested = False
        drain_task = PROVIDERS.codex_manager_drain_task
        PROVIDERS.codex_manager_drain_task = None
        if drain_task is not None and not drain_task.done():
            drain_task.cancel()
            await asyncio.gather(drain_task, return_exceptions=True)
        await cancel_codex_interactions(resolution="server_closed")
        await cancel_codex_native_actions()
        async with CODEX_APP_SERVER_MANAGER_LOCK:
            if PROVIDERS.codex_manager_cleanup_epoch is not None:
                raise RuntimeError("Codex provider cleanup is already in progress")
            managers = codex_app_server_managers()
            cleanup_epoch = PROVIDERS.codex_manager_epoch
            PROVIDERS.codex_manager = None
            CODEX_RETIRED_APP_SERVER_MANAGERS.clear()
            CODEX_SESSION_APP_SERVER_MANAGERS.clear()
            PROVIDERS.codex_manager_cleanup_epoch = cleanup_epoch
        async with CODEX_APP_SERVER_THREAD_LRU_LOCK:
            for event in CODEX_APP_SERVER_EVICTING_THREADS.values():
                event.set()
            CODEX_APP_SERVER_EVICTING_THREADS.clear()
            CODEX_APP_SERVER_THREAD_LRU.clear()
            CODEX_APP_SERVER_PINNED_THREADS.clear()
            CODEX_APP_SERVER_THREAD_PIN_COUNTS.clear()
            CODEX_APP_SERVER_INVALIDATED_THREADS.clear()
        if managers:
            await asyncio.gather(*(manager.close() for manager in managers))
        shutdown_tasks = [
            task
            for registry in (
                RUNS.turn_tasks,
                CODEX_INTERACTION_HANDLER_TASKS,
                CLAUDE_INTERACTION_HANDLER_TASKS,
            )
            for tasks in tuple(registry.values())
            for task in tuple(tasks)
            if not task.done()
        ]
        if shutdown_tasks:
            _done, pending = await asyncio.wait(
                shutdown_tasks,
                timeout=max(0.01, CODEX_SESSION_CLEANUP_TIMEOUT_SECONDS),
            )
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
        # Closing the provider releases ordinary turns, whose finalizers may touch
        # or unpin their thread after the initial shutdown clear. Clear a second
        # time so a future manager generation cannot inherit stale LRU state.
        async with CODEX_APP_SERVER_THREAD_LRU_LOCK:
            for event in CODEX_APP_SERVER_EVICTING_THREADS.values():
                event.set()
            CODEX_APP_SERVER_EVICTING_THREADS.clear()
            CODEX_APP_SERVER_THREAD_LRU.clear()
            CODEX_APP_SERVER_PINNED_THREADS.clear()
            CODEX_APP_SERVER_THREAD_PIN_COUNTS.clear()
            CODEX_APP_SERVER_INVALIDATED_THREADS.clear()
        CODEX_APPROVAL_ITEM_CACHE.clear()
        CODEX_INTERACTIVE_CONTROL_THREADS.clear()
        CODEX_INTERACTIVE_CONTROL_THREAD_COUNTS.clear()
        CODEX_QUARANTINED_GOAL_THREADS.clear()
        await reset_codex_ephemeral_runtime_metadata()
        async with CODEX_APP_SERVER_MANAGER_LOCK:
            if PROVIDERS.codex_manager_cleanup_epoch == cleanup_epoch:
                PROVIDERS.codex_manager_cleanup_epoch = None

    finally:
        PROVIDERS.codex_manager_closing = False


def codex_control_http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, CodexAppServerRequestError) and exc.code in {
        -32600,
        -32601,
    }:
        return HTTPException(
            status_code=501,
            detail=f"This Codex app-server does not support that control: {exc}",
        )
    if isinstance(exc, CodexAppServerError):
        return HTTPException(
            status_code=502,
            detail=f"Codex app-server control failed: {concise_error_message(exc)}",
        )
    if isinstance(exc, (ValueError, TypeError)):
        return HTTPException(status_code=400, detail=str(exc))
    return HTTPException(
        status_code=500,
        detail=f"Codex control failed: {concise_error_message(exc)}",
    )


async def acquire_codex_control_thread(
    session_id: str,
    *,
    reserve_session: bool = False,
    allow_active_goal_mutation: bool = False,
) -> tuple[CodexAppServerManager, str, dict[str, Any]]:
    """Load and pin a Codex thread for an explicit desktop control."""
    reservation_task = asyncio.current_task() if reserve_session else None
    if reservation_task is not None:
        register_session_task(
            RUNS.turn_tasks,
            session_id,
            reservation_task,
        )
    ensure_session_not_deleting(session_id)
    session = STORE.sessions.get(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="session not found")
    if str(session.get("backend") or DEFAULT_BACKEND) != BACKEND_CODEX:
        raise HTTPException(status_code=409, detail="Codex controls require a Codex chat")
    if CODEX_TRANSPORT == CODEX_TRANSPORT_EXEC:
        raise HTTPException(
            status_code=503,
            detail="Codex controls require the app-server transport",
        )

    if reserve_session:
        await prepare_codex_login_turn(session)

    reserved = False
    reservation_id = (
        f"codexcontrol_{uuid.uuid4().hex[:16]}" if reserve_session else ""
    )
    maintenance_reserved = False
    active_thread_id = ""
    async with RUNS.active_lock:
        lifecycle_blocker = server_lifecycle_admission_blocker()
        if lifecycle_blocker:
            raise HTTPException(status_code=503, detail=lifecycle_blocker)
        if CODEX_GOALS_RECONFIGURING:
            raise HTTPException(
                status_code=409,
                detail="wait for Codex goals configuration to finish",
            )
        if session_id in RUNS.maintenance:
            raise HTTPException(
                status_code=409,
                detail="wait for Codex session maintenance to finish",
            )
        if session_id in RUNS.busy:
            active = RUNS.active.get(session_id)
            # interactive_app_server describes the initiating client's ability
            # to render approvals, not whether this loaded thread can accept a
            # goal mutation from another compatible client.
            if (
                not allow_active_goal_mutation
                or not active
                or active.get("backend") != BACKEND_CODEX
                or active.get("transport") != CODEX_TRANSPORT_APP_SERVER
                or not str(active.get("provider_thread_id") or "").strip()
                or (reserve_session and (
                    active.get("codex_goal_handoff_closed") or active.get("stop_requested")
                ))
            ):
                raise HTTPException(
                    status_code=409,
                    detail="wait for the active Codex turn to finish",
                )
            active_thread_id = str(active["provider_thread_id"]).strip()
        elif reserve_session:
            RUNS.busy.add(session_id)
            RUNS.current_turns[session_id] = {
                "run_id": None,
                "backend": BACKEND_CODEX,
                "purpose": "codex_native_control",
                "interactive_app_server": True,
                "codex_control_reservation_id": reservation_id,
            }
            reserved = True
        if not reserve_session:
            RUNS.maintenance.add(session_id)
            maintenance_reserved = True

    manager: CodexAppServerManager | None = None
    thread_id = ""
    thread_pinned = False
    control_lease_acquired = False
    try:
        if active_thread_id:
            manager = existing_codex_app_server_manager(session)
            stored_thread_id = str(session_provider_id(session) or "").strip()
            if (
                manager is None
                or not manager.is_thread_loaded(active_thread_id)
                or (
                    stored_thread_id
                    and stored_thread_id != active_thread_id
                )
            ):
                raise HTTPException(
                    status_code=409,
                    detail="wait for the active Codex thread to finish loading",
                )
            thread_id = active_thread_id
            await pin_codex_app_server_thread(thread_id, manager)
            thread_pinned = True
            acquire_codex_interactive_control_lease(thread_id)
            control_lease_acquired = True
            async with RUNS.active_lock:
                active = RUNS.active.get(session_id)
                active_changed = (
                    session_id not in RUNS.busy
                    or not active
                    or active.get("backend") != BACKEND_CODEX
                    or active.get("transport") != CODEX_TRANSPORT_APP_SERVER
                    or str(active.get("provider_thread_id") or "").strip()
                    != thread_id
                    or (reserve_session and (
                        active.get("codex_goal_handoff_closed") or active.get("stop_requested")
                    ))
                )
            if active_changed:
                raise HTTPException(
                    status_code=409,
                    detail="the active Codex turn changed; retry the control",
                )
            return manager, thread_id, dict(
                STORE.sessions.get(session_id) or session
            )
        manager = await codex_app_server_manager(session, allow_retired_login=not reserve_session)
        await manager.start()
        cwd = existing_cwd(str(session.get("cwd") or DEFAULT_CWD))
        thread_id, _instruction_hash = await ensure_codex_app_server_thread(
            manager,
            session_id,
            session,
            cwd,
        )
        thread_pinned = True
        acquire_codex_interactive_control_lease(thread_id)
        control_lease_acquired = True
        if reserved:
            bound, stop_before_bind = await bind_codex_control_reservation(
                session_id,
                reservation_id,
                {
                    "proc": None,
                    "run_id": None,
                    "backend": BACKEND_CODEX,
                    "transport": CODEX_TRANSPORT_APP_SERVER,
                    "provider_thread_id": thread_id,
                    "provider_session_id": thread_id,
                    "provider_turn_id": None,
                    "provider_turn_ready": False,
                    "interactive_app_server": True,
                    "codex_approval_policy": effective_codex_approval_policy(session),
                    "codex_native_operation": True,
                    "codex_native_operation_kind": None,
                    "owner_task": reservation_task,
                    "started_at": time.time(),
                    "started_at_iso": now_iso(),
                    "stop_requested": False,
                },
            )
            if not bound:
                with suppress(Exception):
                    if stop_before_bind:
                        await append_event(session_id, "turn_stopped", {
                            "run_id": None,
                            "backend": BACKEND_CODEX,
                            "message": "Codex control stopped before it started.",
                        })
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "Codex control stopped before it started"
                        if stop_before_bind
                        else "the Codex control reservation changed; retry"
                    ),
                )
        selected_session = dict(STORE.sessions.get(session_id) or session)
        if reserved:
            selected_session["_codex_control_reservation_id"] = reservation_id
        return manager, thread_id, selected_session
    except BaseException as exc:
        if control_lease_acquired:
            release_codex_interactive_control_lease(thread_id)
        if thread_pinned and thread_id and manager is not None:
            with suppress(Exception):
                await unpin_codex_app_server_thread(manager, thread_id)
        if reserved:
            await release_codex_control_slot(
                session_id,
                expected_thread_id=thread_id,
                expected_reservation_id=reservation_id,
                allow_prebind=True,
            )
        if maintenance_reserved:
            async with RUNS.active_lock:
                RUNS.maintenance.discard(session_id)
        if isinstance(exc, asyncio.CancelledError):
            raise
        if isinstance(exc, HTTPException):
            raise
        if not isinstance(exc, Exception):
            raise
        raise codex_control_http_error(exc) from exc
    finally:
        if reservation_task is not None:
            tasks = RUNS.turn_tasks.get(session_id)
            if tasks is not None:
                tasks.discard(reservation_task)
                if not tasks:
                    RUNS.turn_tasks.pop(session_id, None)


async def release_codex_control_slot(
    session_id: str,
    *,
    expected_thread_id: str,
    expected_reservation_id: str,
    allow_prebind: bool = False,
) -> bool:
    """Release only the exact native-control reservation.

    Native controls do not necessarily have a run id before their provider
    turn starts.  A cancelled control may also remain blocked while unpinning
    its thread; if a replacement turn starts meanwhile, its late cleanup must
    not clear the replacement's ACTIVE/BUSY state. Thread identity alone is
    insufficient because two controls can reuse the same provider thread.
    """

    expected_thread = str(expected_thread_id or "").strip()
    expected_reservation = str(expected_reservation_id or "").strip()
    async with RUNS.active_lock:
        active = RUNS.active.get(session_id)
        current = RUNS.current_turns.get(session_id)
        if isinstance(active, dict):
            active_thread = str(
                active.get("provider_thread_id")
                or active.get("provider_session_id")
                or ""
            ).strip()
            active_reservation = str(
                active.get("codex_control_reservation_id") or ""
            ).strip()
            owns_slot = bool(
                active.get("codex_native_operation")
                and expected_reservation
                and active_reservation == expected_reservation
                and (
                    not expected_thread
                    or active_thread == expected_thread
                )
            )
        else:
            current_reservation = (
                str(current.get("codex_control_reservation_id") or "").strip()
                if isinstance(current, dict)
                else ""
            )
            owns_slot = bool(
                allow_prebind
                and isinstance(current, dict)
                and current.get("purpose") == "codex_native_control"
                and expected_reservation
                and current_reservation == expected_reservation
            )
        if not owns_slot:
            return False
        RUNS.active.pop(session_id, None)
        RUNS.busy.discard(session_id)
        RUNS.current_turns.pop(session_id, None)
        RUNS.stop_requests.discard(session_id)
        return True


async def claim_codex_control_terminal_publication(
    session_id: str,
    operation_id: str,
    thread_id: str,
    reservation_id: str,
) -> bool:
    """Atomically release one native operation into a publication fence.

    The short maintenance reservation blocks every new turn admission while
    terminal/error rows are persisted. A delayed finalizer that already lost
    its reservation cannot claim the fence and therefore emits nothing for a
    replacement turn.
    """

    expected_operation = str(operation_id or "").strip()
    expected_thread = str(thread_id or "").strip()
    expected_reservation = str(reservation_id or "").strip()
    async with RUNS.active_lock:
        active = RUNS.active.get(session_id)
        current = RUNS.current_turns.get(session_id)
        if not isinstance(active, dict) or not isinstance(current, dict):
            return False
        active_thread = str(
            active.get("provider_thread_id")
            or active.get("provider_session_id")
            or ""
        ).strip()
        owns_operation = bool(
            expected_operation
            and expected_reservation
            and session_id in RUNS.busy
            and session_id not in RUNS.maintenance
            and active.get("codex_native_operation")
            and str(active.get("run_id") or "") == expected_operation
            and str(active.get("codex_control_reservation_id") or "").strip()
            == expected_reservation
            and str(current.get("codex_control_reservation_id") or "").strip()
            == expected_reservation
            and (
                not expected_thread
                or active_thread == expected_thread
            )
        )
        if not owns_operation:
            return False
        RUNS.active.pop(session_id, None)
        RUNS.busy.discard(session_id)
        RUNS.current_turns.pop(session_id, None)
        RUNS.stop_requests.discard(session_id)
        RUNS.maintenance.add(session_id)
        CODEX_CONTROL_TERMINAL_FENCES[session_id] = expected_reservation
        return True


def register_codex_native_action(
    session_id: str,
    operation_id: str,
    task: asyncio.Task[Any],
) -> None:
    key = (session_id, operation_id)
    CODEX_NATIVE_ACTION_TASKS[key] = task

    def discard(completed: asyncio.Task[Any]) -> None:
        if CODEX_NATIVE_ACTION_TASKS.get(key) is completed:
            CODEX_NATIVE_ACTION_TASKS.pop(key, None)
        if completed.cancelled():
            return
        error = completed.exception()
        if error is not None:
            logger.error(
                "Codex native control failed session=%s operation=%s error=%s",
                session_id,
                operation_id,
                concise_error_message(error),
            )

    task.add_done_callback(discard)


async def cancel_codex_native_actions(session_id: str | None = None) -> None:
    """Cancel and join native control consumers before their owner disappears."""
    selected = [
        (task_session_id, operation_id, task)
        for (task_session_id, operation_id), task in tuple(
            CODEX_NATIVE_ACTION_TASKS.items()
        )
        if session_id is None or task_session_id == session_id
    ]
    selected_session_ids = {task_session_id for task_session_id, _, _ in selected}
    owned_session_ids: set[str] = set()
    interrupts: list[tuple[str, dict[str, Any]]] = []
    async with RUNS.active_lock:
        for task_session_id in selected_session_ids:
            active = RUNS.active.get(task_session_id)
            if not active or not active.get("codex_native_operation"):
                continue
            owned_session_ids.add(task_session_id)
            active["stop_requested"] = True
            if active.get("run_id"):
                RUNS.stopped_runs.add(str(active["run_id"]))
            thread_id = str(active.get("provider_thread_id") or "")
            turn_id = str(active.get("provider_turn_id") or "")
            if (
                thread_id
                and turn_id
                and not active.get("native_interrupt_sent")
            ):
                active["native_interrupt_sent"] = True
                interrupts.append((task_session_id, {
                    "threadId": thread_id,
                    "turnId": turn_id,
                }))
    for task_session_id, params in interrupts:
        manager = existing_codex_app_server_manager_for_thread(params["threadId"])
        if manager is not None:
            try:
                await manager.request("turn/interrupt", params)
            except Exception as exc:
                async with RUNS.active_lock:
                    active = RUNS.active.get(task_session_id)
                    if active:
                        active["native_interrupt_sent"] = False
                logger.warning(
                    "Codex native control cleanup interrupt failed "
                    "session=%s error=%s",
                    task_session_id,
                    concise_error_message(exc),
                )
    unowned_tasks = [
        task
        for task_session_id, _, task in selected
        if task_session_id not in owned_session_ids
    ]
    for task in unowned_tasks:
        task.cancel()
    if unowned_tasks:
        await asyncio.gather(*unowned_tasks, return_exceptions=True)

    owned_tasks = [
        task
        for task_session_id, _, task in selected
        if task_session_id in owned_session_ids and not task.done()
    ]
    if not owned_tasks:
        return
    if session_id is None:
        # Manager shutdown follows immediately and terminates the shared
        # provider process, so local consumers can be cancelled without
        # orphaning their native operations.
        for task in owned_tasks:
            task.cancel()
        await asyncio.gather(*owned_tasks, return_exceptions=True)
        return

    _done, pending = await asyncio.wait(
        owned_tasks,
        timeout=max(0.01, CODEX_SESSION_CLEANUP_TIMEOUT_SECONDS),
    )
    if pending:
        raise HTTPException(
            status_code=409,
            detail=(
                "Codex native control has not acknowledged interruption yet; "
                "the session was not deleted. Retry shortly."
            ),
        )


async def send_codex_goal_steer(
    session_id: str, operation_id: str, manager: CodexAppServerManager,
    thread_id: str, reservation_id: str, subscription: Any,
    steer_queue: asyncio.Queue[dict[str, Any]], request: dict[str, Any],
) -> dict[str, Any]:
    """Steer one exact native goal turn without replacing its authority/owner."""
    selected = request["selected"]
    expected_turn_id = str(request.get("expected_provider_turn_id") or "")
    generation = manager.generation

    def delivery_owner_valid() -> bool:
        # Also called synchronously under the transport writer lock. There is
        # no await between that check and stdin.write: Stop/Pause cannot slip
        # into a wait-for-writer window after the last ownership check.
        active = RUNS.active.get(session_id) or {}
        current = RUNS.current_turns.get(session_id) or {}
        session = STORE.sessions.get(session_id) or {}
        goal = session.get("codex_goal")
        goal_identity = (
            str(goal.get("id") or ""), str(goal.get("objective") or ""),
        ) if isinstance(goal, dict) else None
        turn = active.get("codex_app_server_turn")
        same_owner_lane = (
            (
                bool(reservation_id)
                and active.get("codex_native_operation_kind") == "goal_resume"
                and active.get("native_steer_queue") is steer_queue
            ) or (
                not reservation_id
                and not active.get("codex_native_operation_kind")
                and not active.get("standalone_provider_context")
                and active.get("codex_goal_steer_queue") is steer_queue
                and turn is not None
                and not getattr(turn, "_closed", False)
                and not getattr(turn, "_completed", False)
                and getattr(turn, "_subscription", None) is subscription
                and str(getattr(turn, "turn_id", "")) == expected_turn_id
                and str(getattr(turn, "thread_id", "")) == thread_id
            )
        )
        return bool(
            CODEX_GOALS_ENABLED and manager.generation == generation
            and not getattr(subscription, "_closed", False)
            and session_id not in DELETING_SESSIONS
            and session_id not in DELETED_SESSION_TOMBSTONES
            and session_id not in RUNS.maintenance
            and session_id in RUNS.busy
            and session_id not in RUNS.stop_requests
            and operation_id not in RUNS.stopped_runs
            and isinstance(goal, dict) and goal.get("status") == "active"
            and goal_identity == request.get("goal_identity")
            and not codex_goal_time_budget_is_exhausted(session)
            and same_owner_lane
            and not active.get("stop_requested")
            and active.get("provider_turn_ready")
            and expected_turn_id
            and str(active.get("provider_turn_id") or "") == expected_turn_id
            and str(active.get("provider_thread_id") or "") == thread_id
            and str(active.get("run_id") or "") == operation_id
            and str(current.get("run_id") or "") == operation_id
            and str(active.get("codex_control_reservation_id") or "") == reservation_id
            and str(current.get("codex_control_reservation_id") or "") == reservation_id
            and codex_goal_steer_selection_is_plain(selected)
            # Only text/files are sent. The current turn's command, references,
            # runtime and authority stay intact, regardless of picker changes
            # or capability metadata on an older queued message.
        )

    async def validate_delivery_owner() -> None:
        async with RUNS.active_lock:
            allowed = delivery_owner_valid()
        if not allowed:
            raise NativeSteerHandoffError(
                "The goal, native turn, or control owner changed before delivery; the follow-up was not sent",
                safe_to_requeue=True,
            )

    try:
        await validate_delivery_owner()
        prompt = build_user_provider_prompt(
            session_id, str(selected.get("prompt") or ""), list(selected.get("file_ids") or []),
        )
        await fence_native_steer_delivery(session_id, selected, backend=BACKEND_CODEX)
        # Stop/Pause, budget/goal changes, and native turn rollover can win
        # during the durable fence. Check again at the RPC boundary.
        await validate_delivery_owner()
        request["_goal_steer_rpc_started"] = True
        acknowledged_turn_id, watermark = await manager.steer_turn_with_notification_watermark(
            thread_id, expected_turn_id,
            [{"type": "text", "text": prompt, "text_elements": []}],
            client_user_message_id=str(selected.get("queued_id") or ""),
            notification_subscription=subscription,
            before_send=delivery_owner_valid,
        )
        if acknowledged_turn_id != expected_turn_id or isinstance(watermark, bool) or not isinstance(watermark, int) or watermark < 0:
            raise CodexAppServerProtocolError(
                "Codex goal steer returned an invalid acknowledgement boundary",
                request_sent=True, safe_to_retry=False,
            )
        return {"request": request, "watermark": watermark, "provider_turn_id": expected_turn_id}
    except NativeSteerHandoffError:
        raise
    except BaseException as exc:
        safe = not request.get("_goal_steer_rpc_started") or isinstance(exc, CodexAppServerRequestError) or (
            isinstance(exc, CodexAppServerError)
            and getattr(exc, "request_sent", True) is False
            and getattr(exc, "safe_to_retry", False) is True
        )
        if safe:
            request["_goal_steer_rpc_started"] = False
        if isinstance(exc, asyncio.CancelledError):
            raise
        raise NativeSteerHandoffError(
            concise_error_message(exc), safe_to_requeue=bool(safe), delivery_uncertain=not safe,
        ) from exc


async def commit_codex_goal_steer(
    session_id: str, operation_id: str, thread_id: str,
    reservation_id: str, pending: dict[str, Any],
) -> dict[str, Any]:
    """Record accepted input without restarting, resuming, or rebinding a goal."""
    request = pending["request"]
    selected = request["selected"]
    async with RUNS.active_lock:
        active = RUNS.active.get(session_id) or {}
        current = RUNS.current_turns.get(session_id) or {}
        owned = (
            str(active.get("run_id") or "") == operation_id
            and str(current.get("run_id") or "") == operation_id
            and str(active.get("provider_thread_id") or "") == thread_id
            and str(active.get("codex_control_reservation_id") or "") == reservation_id
            and str(current.get("codex_control_reservation_id") or "") == reservation_id
        )
    if not owned:
        raise NativeSteerHandoffError(
            "The goal owner changed after delivery; the follow-up was not replayed",
            safe_to_requeue=False, delivery_uncertain=True,
        )
    display_prompt = str(selected.get("display_prompt") if selected.get("display_prompt") is not None else selected.get("prompt") or "")
    display_file_ids = list(
        selected.get("display_file_ids")
        if selected.get("display_file_ids") is not None
        else selected.get("file_ids") or []
    )
    await append_durable_event_batch(session_id, [
        ("turn_queue_run_now", {
            "queued_id": selected.get("queued_id"), "run_id": operation_id,
            "backend": BACKEND_CODEX, "prompt": display_prompt,
            "file_ids": display_file_ids,
            "native_steer": True, "native_goal_steer": True,
            "interrupted": False, "replays_interrupted_message": False,
            "remaining": request.get("remaining", 0), "superseded_queued_ids": [],
            "message": "Follow-up sent to the active Codex goal without pausing it.",
        }),
        ("turn_steered", {
            "run_id": operation_id, "backend": BACKEND_CODEX,
            "purpose": "codex_goal_resume", "provider_turn_id": pending["provider_turn_id"],
            "queued_id": selected.get("queued_id"), "prompt": display_prompt,
            "file_ids": display_file_ids, "native_steer": True, "native_goal_steer": True,
            "provider_user_authored": True,
        }),
    ])
    return {
        "ok": True, "queued_id": selected.get("queued_id"), "run_id": operation_id,
        "interrupted": False, "native_steer": True, "native_goal_steer": True,
        "replays_interrupted_message": False, "superseded_queued_ids": [],
    }


def invalidated_codex_thread_error(thread_id: str) -> CodexAppServerError:
    return CodexAppServerError(
        (
            "the cached Codex thread is being refreshed after a provider "
            f"account change ({thread_id})"
        ),
        request_sent=False,
        safe_to_retry=True,
    )


async def pin_codex_app_server_thread(
    thread_id: str,
    manager: CodexAppServerManager | None = None,
) -> None:
    if not thread_id:
        return
    while True:
        retire_invalidated = False
        async with CODEX_APP_SERVER_THREAD_LRU_LOCK:
            eviction = CODEX_APP_SERVER_EVICTING_THREADS.get(thread_id)
            if eviction is not None:
                wait_for_eviction = eviction
            elif thread_id in CODEX_APP_SERVER_INVALIDATED_THREADS:
                wait_for_eviction = None
                if CODEX_APP_SERVER_THREAD_PIN_COUNTS.get(thread_id, 0) > 0:
                    raise invalidated_codex_thread_error(thread_id)
                retire_invalidated = True
            else:
                CODEX_APP_SERVER_THREAD_PIN_COUNTS[thread_id] = (
                    CODEX_APP_SERVER_THREAD_PIN_COUNTS.get(thread_id, 0) + 1
                )
                CODEX_APP_SERVER_PINNED_THREADS.add(thread_id)
                return
        if wait_for_eviction is not None:
            await wait_for_eviction.wait()
            continue
        if retire_invalidated:
            if manager is None or not await retire_invalidated_codex_app_server_thread(
                manager,
                thread_id,
            ):
                raise invalidated_codex_thread_error(thread_id)


async def evict_codex_app_server_thread(
    manager: CodexAppServerManager,
    thread_id: str,
    *,
    reinsert_on_failure: bool,
) -> bool:
    """Reserve then unsubscribe a thread without holding the global LRU lock."""
    if not thread_id:
        return False
    while True:
        async with CODEX_APP_SERVER_THREAD_LRU_LOCK:
            existing = CODEX_APP_SERVER_EVICTING_THREADS.get(thread_id)
            if existing is not None:
                wait_for_existing = existing
            else:
                wait_for_existing = None
                if (
                    thread_id in CODEX_APP_SERVER_PINNED_THREADS
                    or thread_id in CODEX_INTERACTIVE_CONTROL_THREADS
                    or manager.active_turn(thread_id) is not None
                    or not manager.is_thread_loaded(thread_id)
                ):
                    return False
                eviction = asyncio.Event()
                CODEX_APP_SERVER_EVICTING_THREADS[thread_id] = eviction
                CODEX_APP_SERVER_THREAD_LRU.pop(thread_id, None)
                break
        if wait_for_existing is not None:
            await wait_for_existing.wait()
            return not manager.is_thread_loaded(thread_id)

    succeeded = False
    try:
        await manager.unsubscribe_thread(thread_id)
        succeeded = True
        return True
    except CodexAppServerError:
        return False
    finally:
        async with CODEX_APP_SERVER_THREAD_LRU_LOCK:
            CODEX_APP_SERVER_EVICTING_THREADS.pop(thread_id, None)
            if (
                not succeeded
                and reinsert_on_failure
                and manager.is_thread_loaded(thread_id)
            ):
                CODEX_APP_SERVER_THREAD_LRU[thread_id] = time.monotonic()
            eviction.set()


async def retire_invalidated_codex_app_server_thread(
    manager: CodexAppServerManager,
    thread_id: str,
) -> bool:
    """Unload an invalidated subscription, preserving the fence on failure."""

    try:
        evicted = await evict_codex_app_server_thread(
            manager,
            thread_id,
            reinsert_on_failure=False,
        )
    except Exception as exc:
        logger.warning(
            "failed to retire invalidated Codex thread=%s: %s",
            thread_id,
            concise_error_message(exc),
        )
        evicted = False
    unloaded = evicted or not manager.is_thread_loaded(thread_id)
    if unloaded:
        async with CODEX_APP_SERVER_THREAD_LRU_LOCK:
            if CODEX_APP_SERVER_THREAD_PIN_COUNTS.get(thread_id, 0) == 0:
                CODEX_APP_SERVER_INVALIDATED_THREADS.discard(thread_id)
    return unloaded


async def unpin_codex_app_server_thread(
    manager: CodexAppServerManager,
    thread_id: str,
    *,
    invalidate_loaded_thread: bool = False,
) -> None:
    """Release exactly the caller's pin and defer invalidation until safe."""

    if not thread_id:
        return
    async with CODEX_APP_SERVER_THREAD_LRU_LOCK:
        count = CODEX_APP_SERVER_THREAD_PIN_COUNTS.get(thread_id, 0)
        if count <= 0:
            return
        if invalidate_loaded_thread:
            CODEX_APP_SERVER_INVALIDATED_THREADS.add(thread_id)
            CODEX_APP_SERVER_THREAD_LRU.pop(thread_id, None)
        remaining = count - 1
        if remaining > 0:
            CODEX_APP_SERVER_THREAD_PIN_COUNTS[thread_id] = remaining
            return
        CODEX_APP_SERVER_THREAD_PIN_COUNTS.pop(thread_id, None)
        CODEX_APP_SERVER_PINNED_THREADS.discard(thread_id)
        must_retire = thread_id in CODEX_APP_SERVER_INVALIDATED_THREADS
    if must_retire:
        await retire_invalidated_codex_app_server_thread(manager, thread_id)
        return
    retained = any(
        str(session_provider_id(session) or "") == thread_id
        for session in STORE.sessions.values()
    )
    if retained:
        await touch_codex_app_server_thread(manager, thread_id)
        return
    async with CODEX_APP_SERVER_THREAD_LRU_LOCK:
        CODEX_APP_SERVER_THREAD_LRU.pop(thread_id, None)
    await evict_codex_app_server_thread(
        manager,
        thread_id,
        reinsert_on_failure=False,
    )


async def touch_codex_app_server_thread(
    manager: CodexAppServerManager,
    thread_id: str,
) -> None:
    """Bound idle subscriptions across the normal and custom native processes."""
    if not thread_id:
        return
    async with CODEX_APP_SERVER_THREAD_LRU_LOCK:
        if thread_id in CODEX_APP_SERVER_INVALIDATED_THREADS:
            CODEX_APP_SERVER_THREAD_LRU.pop(thread_id, None)
            return
        CODEX_APP_SERVER_THREAD_LRU.pop(thread_id, None)
        CODEX_APP_SERVER_THREAD_LRU[thread_id] = time.monotonic()
        managers = (manager, *(
            current for current in codex_app_server_managers() if current is not manager
        ))
        candidates = []
        for candidate in tuple(CODEX_APP_SERVER_THREAD_LRU):
            owner = next((current for current in managers
                if current.is_thread_loaded(candidate)), None)
            if owner is None:
                CODEX_APP_SERVER_THREAD_LRU.pop(candidate, None)
            elif (
                candidate not in CODEX_APP_SERVER_PINNED_THREADS
                and candidate not in CODEX_APP_SERVER_EVICTING_THREADS
                and owner.active_turn(candidate) is None
            ):
                candidates.append((candidate, owner))
        overflow = max(
            0,
            len(CODEX_APP_SERVER_THREAD_LRU)
            - CODEX_APP_SERVER_MAX_LOADED_THREADS,
        )
        candidates = candidates[:overflow]
    for candidate, owner in candidates:
        await evict_codex_app_server_thread(
            owner,
            candidate,
            reinsert_on_failure=True,
        )


def expire_codex_login_diagnostic() -> None:
    """A login hint requires new native evidence, never reuse a cached denial."""
    with RUNTIME_DIAGNOSTICS_LOCK:
        PROVIDERS.codex_model_migration_target = "gpt-6-luna"
        current = dict(RUNTIME_DIAGNOSTICS.get(BACKEND_CODEX) or runtime_diagnostic_payload(
            BACKEND_CODEX, "unknown", installed=None, authenticated=None))
        current["checked_at_epoch"] = 0.0
        current.pop("_codex_auth_mode", None)
        current.pop("_codex_auth_mode_checked_at_epoch", None)
        store_runtime_diagnostic(current)


def codex_user_developer_instructions() -> str:
    try:
        payload = load_codex_user_config(codex_user_config_path())
    except Exception as exc:
        logger.debug("codex developer instruction discovery skipped: %s", exc)
        return ""
    return str(payload.get("developer_instructions") or "").strip()


def codex_thread_instructions(session_id: str, sess: dict[str, Any]) -> str:
    """Return stable thread-level Codex instructions, never turn/user content."""
    provider_context = CODEX_PROMPT_PRELUDE.format(
        manifest_path=str(codex_manifest_path(session_id)),
        terminal_session=terminal_session_name(session_id),
        chat_id=session_id,
    )
    user_developer_instructions = codex_user_developer_instructions()
    return "\n\n".join(
        value
        for value in (
            user_developer_instructions,
            provider_context.rstrip(),
            session_prompt_addendum(sess).strip(),
        )
        if value
    )


def codex_thread_instruction_hash(session_id: str, sess: dict[str, Any]) -> str:
    payload = (
        f"hyadex-policy-v{CODEX_THREAD_POLICY_VERSION}\0"
        f"{codex_thread_instructions(session_id, sess)}"
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


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


async def apply_codex_goal_time_budget_limit(
    session_id: str,
    run_id: str,
    manager: CodexAppServerManager,
    provider_id: str,
    turn: Any,
    time_budget_seconds: Any,
) -> None:
    """Persist and enforce an Hyadex time limit without trusting I/O."""
    async with STORE.transaction():
        current = STORE.sessions.get(session_id)
        if current:
            current["codex_goal_time_budget_exhausted"] = True
            current["updated_at"] = now_iso()
            await STORE.save()
    goal: dict[str, Any] | None = None
    with suppress(Exception):
        goal = await manager.set_thread_goal(
            provider_id,
            status="budgetLimited",
        )
    if isinstance(goal, dict):
        async with STORE.transaction():
            current = STORE.sessions.get(session_id)
            if current:
                current["codex_goal"] = goal
                current["updated_at"] = now_iso()
                await STORE.save()
    async with RUNS.active_lock:
        active = RUNS.active.get(session_id)
        if active:
            active["stop_requested"] = True
            RUNS.stopped_runs.add(str(active.get("run_id") or run_id))
    if turn is not None and turn.turn_id:
        with suppress(CodexAppServerError):
            await turn.interrupt()
    try:
        await append_event(
            session_id,
            "codex_goal_budget_limited",
            {
                "run_id": run_id,
                "time_budget_seconds": time_budget_seconds,
                "time_budget_owner": "Hyadex",
                "message": (
                    "The persistent goal reached its Hyadex time limit."
                ),
            },
        )
    except Exception as exc:
        logger.warning(
            "failed to persist Codex goal budget event session=%s: %s",
            session_id,
            concise_error_message(exc),
        )


def codex_thread_params(
    sess: dict[str, Any],
    cwd: str,
    *,
    developer_instructions: str | None = None,
) -> dict[str, Any]:
    model, _effort, service_tier = codex_runtime_settings(sess)
    params: dict[str, Any] = {
        "cwd": cwd,
        "approvalPolicy": CODEX_NONINTERACTIVE_APPROVAL_POLICY,
        "sandbox": CODEX_DEFAULT_SANDBOX_MODE,
    }
    if developer_instructions is not None:
        params["developerInstructions"] = developer_instructions
    if model:
        params["model"] = model
    if service_tier:
        params["serviceTier"] = codex_app_server_service_tier(service_tier)
    # Honor explicit per-thread settings for start, resume and fork without
    # replacing the provider's subagent defaults or changing global config.
    config = codex_effective_thread_config(sess)
    if config:
        # Dotted keys: Codex applies the per-thread config map like ``-c``
        # overrides, and a nested ``{"agents": {...}}`` would replace the
        # user's whole [agents] table instead of only the leaf we set.
        params["config"] = flatten_codex_config_overrides(config)
    # This process-secret HTTP MCP is a provider tool transport, not model
    # context.  The static endpoint learns the exact live run only from
    # Codex-owned turn metadata supplied below at turn/start.
    flat_config = params.setdefault("config", {})
    reserved_prefix = f"mcp_servers.{CODEX_PROVIDER_MCP_NAME}"
    for key in tuple(flat_config):
        if key == reserved_prefix or key.startswith(reserved_prefix + "."):
            flat_config.pop(key, None)
    flat_config.update(codex_provider_mcp_config())
    return params


def codex_provider_mcp_config() -> dict[str, Any]:
    """Return the static, process-scoped provider-tool MCP configuration."""

    prefix = f"mcp_servers.{CODEX_PROVIDER_MCP_NAME}"
    return {
        f"{prefix}.url": (
            f"{provider_helper_server_origin()}{CODEX_PROVIDER_MCP_PATH}"
        ),
        f"{prefix}.http_headers": {
            CODEX_PROVIDER_MCP_HEADER_NAME: CODEX_PROVIDER_MCP_HEADER_SECRET,
        },
        f"{prefix}.required": True,
        f"{prefix}.default_tools_approval_mode": "approve",
        f"{prefix}.supports_parallel_tool_calls": False,
        f"{prefix}.startup_timeout_sec": 10,
        f"{prefix}.tool_timeout_sec": int(PROVIDER_TOOL_TIMEOUT_SECONDS + 5),
    }


def codex_provider_mcp_run_proof(
    session_id: str,
    provider_thread_id: str,
    run_id: str,
) -> str:
    """Bind provider metadata to one server process, chat, thread, and run."""

    payload = json.dumps(
        [
            "hyadex-provider-mcp-v1",
            SERVER_INSTANCE_ID,
            str(session_id),
            str(provider_thread_id),
            str(run_id),
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hmac.new(
        CODEX_PROVIDER_MCP_PROOF_KEY,
        payload,
        hashlib.sha256,
    ).hexdigest()


def codex_raw_developer_message(text: str) -> dict[str, Any]:
    return {
        "type": "message",
        "role": "developer",
        "content": [{"type": "input_text", "text": text}],
    }


def codex_goal_api_is_unsupported(exc: BaseException) -> bool:
    return isinstance(exc, CodexAppServerRequestError) and exc.code in {
        -32600,
        -32601,
    }


async def codex_thread_has_local_run_owner(
    session_id: str,
    thread_id: str,
) -> bool:
    """Return whether Hyadex currently owns work on this exact thread.

    A native goal and a provider thread status are durable control state, not
    run ownership. Only the local BUSY/ACTIVE bookkeeping may authorize a
    provider continuation.
    """

    async with RUNS.active_lock:
        active = RUNS.active.get(session_id)
        return bool(
            session_id in RUNS.busy
            and active
            and active.get("backend") == BACKEND_CODEX
            and active.get("transport") == CODEX_TRANSPORT_APP_SERVER
            and str(
                active.get("provider_thread_id")
                or active.get("provider_session_id")
                or ""
            ).strip()
            == thread_id
        )


async def persist_reconciled_codex_goal(
    session_id: str,
    thread_id: str,
    goal: dict[str, Any] | None,
    *,
    allow_unbound_thread: bool = False,
) -> bool:
    """Persist provider-authoritative goal state without reordering the chat."""

    changed = False
    async with STORE.transaction():
        current = STORE.sessions.get(session_id)
        if current is None:
            return False
        current_thread_id = str(session_provider_id(current) or "").strip()
        if current_thread_id != thread_id and not (
            allow_unbound_thread and not current_thread_id
        ):
            return False
        if current.get("codex_goal") != goal:
            current["codex_goal"] = goal
            changed = True
        if goal is None:
            if current.get("codex_goal_time_budget_seconds") is not None:
                current["codex_goal_time_budget_seconds"] = None
                changed = True
            if current.get("codex_goal_time_budget_exhausted") is not False:
                current["codex_goal_time_budget_exhausted"] = False
                changed = True
        elif codex_goal_time_budget_is_exhausted(current):
            if current.get("codex_goal_time_budget_exhausted") is not True:
                current["codex_goal_time_budget_exhausted"] = True
                changed = True
        reconciliation = current.get("_codex_goal_reconciliation_required")
        if isinstance(reconciliation, dict) and (
            allow_unbound_thread
            or str(reconciliation.get("replacement_thread_id") or "")
            == thread_id
            or str(reconciliation.get("thread_id") or "") == thread_id
        ):
            current.pop("_codex_goal_reconciliation_required", None)
            changed = True
        if changed:
            await STORE.save()
    return True


def validate_inherited_codex_goal(
    expected_goal: dict[str, Any],
    native_goal: dict[str, Any] | None,
) -> None:
    """Require a native fork to preserve the parent goal faithfully.

    A forked active goal is deliberately paused before it is exposed because
    the new chat has no local run owner. All other representable fields must
    match the parent snapshot exactly; otherwise the caller must discard the
    native fork and use the bounded-memory fallback.
    """

    if not isinstance(native_goal, dict):
        raise CodexAppServerProtocolError(
            "the native fork did not preserve the inherited goal",
            request_sent=True,
            safe_to_retry=False,
        )

    expected_objective = str(expected_goal.get("objective") or "").strip()
    native_objective = str(native_goal.get("objective") or "").strip()
    if not expected_objective or native_objective != expected_objective:
        raise CodexAppServerProtocolError(
            "the native fork did not preserve the inherited goal objective",
            request_sent=True,
            safe_to_retry=False,
        )

    expected_token_budget = expected_goal.get("tokenBudget")
    if expected_token_budget is None and "tokenBudget" not in expected_goal:
        expected_token_budget = expected_goal.get("token_budget")
    if isinstance(expected_token_budget, bool) or (
        expected_token_budget is not None
        and not isinstance(expected_token_budget, int)
    ):
        raise CodexAppServerProtocolError(
            "the cached inherited goal has an invalid token budget",
            request_sent=False,
            safe_to_retry=False,
        )
    if native_goal.get("tokenBudget") != expected_token_budget:
        raise CodexAppServerProtocolError(
            "the native fork did not preserve the inherited goal token budget",
            request_sent=True,
            safe_to_retry=False,
        )

    expected_status = str(expected_goal.get("status") or "").strip()
    if expected_status in CODEX_GOAL_STATUSES:
        # Reconciliation pauses an inherited active goal before validation so
        # ownerless background work can never escape through the new chat.
        exposed_status = "paused" if expected_status == "active" else expected_status
        if str(native_goal.get("status") or "").strip() != exposed_status:
            raise CodexAppServerProtocolError(
                "the native fork did not preserve the inherited goal status",
                request_sent=True,
                safe_to_retry=False,
            )


async def reconcile_codex_thread_goal(
    manager: CodexAppServerManager,
    session_id: str,
    thread_id: str,
    *,
    new_thread: bool = False,
    force: bool = False,
    require_goal_support: bool = False,
    expected_goal: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Reconcile cached and native goal state once per provider generation.

    Existing threads are read from Codex because the local session cache may
    predate a daemon restart. A fresh rollover thread receives the previous
    objective in the paused state, so migration can never create ownerless
    background work.
    """

    generation_value = getattr(manager, "generation", 0)
    generation = generation_value if isinstance(generation_value, int) else 0
    sync_key = (id(manager), generation, thread_id)
    session = STORE.sessions.get(session_id) or {}
    if (
        expected_goal is None
        and not force
        and CODEX_GOAL_SYNC_GENERATIONS.get(session_id) == sync_key
    ):
        cached = session.get("codex_goal")
        return dict(cached) if isinstance(cached, dict) else None

    cached_goal = session.get("codex_goal")
    native_goal: dict[str, Any] | None
    try:
        if new_thread:
            objective = (
                str(cached_goal.get("objective") or "").strip()
                if isinstance(cached_goal, dict)
                else ""
            )
            if objective:
                cached_status = str(cached_goal.get("status") or "")
                migrated_status = (
                    "paused"
                    if cached_status == "active"
                    else cached_status
                    if cached_status in CODEX_GOAL_STATUSES
                    else "paused"
                )
                kwargs: dict[str, Any] = {
                    "objective": objective,
                    # Never resume a migrated goal before a local turn owns it.
                    "status": migrated_status,
                }
                token_budget = cached_goal.get("tokenBudget")
                if token_budget is None:
                    token_budget = cached_goal.get("token_budget")
                if isinstance(token_budget, int) and not isinstance(
                    token_budget, bool
                ):
                    kwargs["token_budget"] = token_budget
                native_goal = await asyncio.wait_for(
                    manager.set_thread_goal(thread_id, **kwargs),
                    timeout=CODEX_GOAL_CONTROL_TIMEOUT_SECONDS,
                )
            else:
                native_goal = None
        else:
            get_goal = getattr(manager, "get_thread_goal", None)
            if get_goal is None:
                # Test doubles and legacy managers without native goals keep
                # the old behavior. Production managers always expose this.
                if require_goal_support or expected_goal is not None:
                    raise CodexAppServerProtocolError(
                        "the native fork cannot verify the inherited goal",
                        request_sent=False,
                        safe_to_retry=False,
                    )
                CODEX_GOAL_SYNC_GENERATIONS[session_id] = sync_key
                return dict(cached_goal) if isinstance(cached_goal, dict) else None
            get_result = get_goal(thread_id)
            if not inspect.isawaitable(get_result):
                # Keep lightweight protocol test doubles compatible; a real
                # Codex manager always returns an awaitable here.
                if require_goal_support or expected_goal is not None:
                    raise CodexAppServerProtocolError(
                        "the native fork cannot verify the inherited goal",
                        request_sent=False,
                        safe_to_retry=False,
                    )
                CODEX_GOAL_SYNC_GENERATIONS[session_id] = sync_key
                return dict(cached_goal) if isinstance(cached_goal, dict) else None
            native_goal = await asyncio.wait_for(
                get_result,
                timeout=CODEX_GOAL_CONTROL_TIMEOUT_SECONDS,
            )
            if (
                isinstance(native_goal, dict)
                and str(native_goal.get("status") or "") == "active"
                and not await codex_thread_has_local_run_owner(
                    session_id,
                    thread_id,
                )
            ):
                native_goal = await asyncio.wait_for(
                    manager.set_thread_goal(thread_id, status="paused"),
                    timeout=CODEX_GOAL_CONTROL_TIMEOUT_SECONDS,
                )
    except Exception as exc:
        if not codex_goal_api_is_unsupported(exc):
            raise
        if require_goal_support:
            raise
        logger.info(
            "Codex goal reconciliation unsupported session=%s thread=%s",
            session_id,
            thread_id,
        )
        CODEX_GOAL_SYNC_GENERATIONS[session_id] = sync_key
        return dict(cached_goal) if isinstance(cached_goal, dict) else None

    if (require_goal_support or expected_goal is not None) and native_goal is None:
        raise CodexAppServerProtocolError(
            "the native fork did not preserve the inherited goal",
            request_sent=True,
            safe_to_retry=False,
        )
    if native_goal is not None and not isinstance(native_goal, dict):
        raise CodexAppServerProtocolError(
            "thread goal reconciliation returned invalid state",
            request_sent=True,
            safe_to_retry=False,
        )
    if expected_goal is not None:
        validate_inherited_codex_goal(expected_goal, native_goal)
    persisted = await persist_reconciled_codex_goal(
        session_id,
        thread_id,
        native_goal,
        allow_unbound_thread=new_thread,
    )
    if persisted:
        CODEX_GOAL_SYNC_GENERATIONS[session_id] = sync_key
    return native_goal


async def ensure_codex_app_server_thread(
    manager: CodexAppServerManager,
    session_id: str,
    sess: dict[str, Any],
    cwd: str,
    *,
    expected_run_id: str | None = None,
) -> tuple[str, str]:
    """Load/create and lease a provider thread, applying changed policy once."""
    native_settings = {**sess, "codex_config_overrides": sanitize_codex_thread_config(
        sess.get("codex_config_overrides") or {}, source=f"session {session_id}")}
    provider_id = str(session_provider_id(sess) or "")
    original_provider_id = provider_id
    had_provider_id = bool(provider_id)
    instructions = codex_thread_instructions(session_id, sess)
    instruction_hash = codex_thread_instruction_hash(session_id, sess)
    stored_hash = str(sess.get("codex_instruction_hash") or "")
    policy_changed = stored_hash != instruction_hash
    pinned = False
    try:
        if provider_id:
            await pin_codex_app_server_thread(provider_id, manager)
            pinned = True
        already_loaded = bool(
            provider_id and manager.is_thread_loaded(provider_id)
        )

        if not provider_id:
            provider_id = await manager.start_thread(
                {
                    **codex_thread_params(
                        native_settings,
                        cwd,
                        developer_instructions=instructions,
                    ),
                    "ephemeral": False,
                    "serviceName": "Hyadex",
                }
            )
            await pin_codex_app_server_thread(provider_id, manager)
            pinned = True
        elif already_loaded and policy_changed:
            # A loaded thread keeps its native thread settings. Reload it with
            # the new policy before accepting another user turn so chat-scoped
            # manifest, jobs, and terminal routing cannot remain stale.
            await manager.unsubscribe_thread(provider_id)
            provider_id = await manager.resume_thread(
                provider_id,
                {
                    **codex_thread_params(
                        native_settings,
                        cwd,
                        developer_instructions=instructions,
                    ),
                    "excludeTurns": True,
                },
            )
        elif not already_loaded:
            resume_params = {
                **codex_thread_params(
                    native_settings,
                    cwd,
                    developer_instructions=(
                        instructions if policy_changed else None
                    ),
                ),
                "excludeTurns": True,
            }
            provider_id = await manager.resume_thread(provider_id, resume_params)

        if not already_loaded or policy_changed:
            await record_codex_subagent_limit_application(manager, session_id, native_settings)

        await reconcile_codex_thread_goal(
            manager,
            session_id,
            provider_id,
            new_thread=not had_provider_id,
        )

        # Current Codex releases can defer changed resume instructions until a
        # later turn. Inject the one-time migration as a developer item so the
        # first post-upgrade turn is correct without ever wrapping user content.
        if had_provider_id and policy_changed:
            await manager.inject_items(
                provider_id,
                [codex_raw_developer_message(instructions)],
            )

        memory_seed = str(sess.get("memory_seed") or "").strip()
        if memory_seed and (
            not sess.get("memory_seed_used")
            or not session_provider_id(sess)
        ):
            await manager.inject_items(
                provider_id,
                [
                    codex_raw_developer_message(
                        "Fork memory context follows. Treat it as bounded background "
                        "for the next user request; do not repeat it verbatim.\n\n"
                        f"{memory_seed}"
                    )
                ],
            )
            async with STORE.transaction():
                current = STORE.sessions.get(session_id)
                if current:
                    current["memory_seed_used"] = True
                    current["updated_at"] = now_iso()
                    await STORE.save()

        provider_state_changed = (
            str(sess.get("session_id") or "") != provider_id
            or str(sess.get("codex_thread_id") or "") != provider_id
            or not str(sess.get("backend") or "").strip()
        )
        if provider_state_changed or policy_changed:
            if expected_run_id is None:
                await STORE.save_provider_session(
                    session_id,
                    provider_id,
                    BACKEND_CODEX,
                    codex_instruction_hash=instruction_hash,
                )
            else:
                await persist_run_provider_session(
                    session_id,
                    expected_run_id,
                    BACKEND_CODEX,
                    provider_id,
                    codex_instruction_hash=instruction_hash,
                    emit_event=False,
                )
        if original_provider_id and original_provider_id != provider_id:
            if CODEX_THREAD_SESSION_INDEX.get(original_provider_id) == session_id:
                CODEX_THREAD_SESSION_INDEX.pop(original_provider_id, None)
        CODEX_THREAD_SESSION_INDEX[provider_id] = session_id
        await touch_codex_app_server_thread(manager, provider_id)
        return provider_id, instruction_hash
    except BaseException:
        if pinned and provider_id:
            with suppress(Exception):
                await unpin_codex_app_server_thread(manager, provider_id)
        raise


async def start_standalone_codex_app_server_thread(
    manager: CodexAppServerManager,
    session_id: str,
    sess: dict[str, Any],
    cwd: str,
) -> str:
    """Start one non-persistent Codex thread for a standalone job run."""

    return await manager.start_thread({
        **codex_thread_params(
            sess,
            cwd,
            developer_instructions=codex_thread_instructions(
                session_id,
                sess,
            ),
        ),
        # Output is persisted in the parent Hyadex job run. Avoid creating
        # a second durable Codex chat that the user cannot navigate directly.
        "ephemeral": True,
        "serviceName": "Hyadex Scheduled Job",
    })


async def acquire_codex_run_thread(
    manager: CodexAppServerManager,
    session_id: str,
    sess: dict[str, Any],
    cwd: str,
    *,
    standalone_provider_context: bool,
    expected_run_id: str | None = None,
) -> str:
    """Acquire a pinned thread without binding standalone runs to the chat."""

    if standalone_provider_context:
        provider_id = await start_standalone_codex_app_server_thread(
            manager,
            session_id,
            sess,
            cwd,
        )
        await pin_codex_app_server_thread(provider_id, manager)
        return provider_id
    ensure_kwargs: dict[str, Any] = {}
    if expected_run_id is not None:
        ensure_kwargs["expected_run_id"] = expected_run_id
    resumed_existing_thread = bool(str(session_provider_id(sess) or "").strip())
    provider_id, _instruction_hash = await ensure_codex_app_server_thread(
        manager,
        session_id,
        sess,
        cwd,
        **ensure_kwargs,
    )
    if resumed_existing_thread and provider_id:
        # A brand-new thread has nothing on disk yet. Only resumed threads can
        # have grown large; warn off the turn's critical path.
        schedule_codex_thread_hygiene_check(session_id, provider_id, expected_run_id)
    return provider_id


def build_claude_sdk_options(
    session_id: str,
    sess: dict[str, Any],
    cwd: str,
    manifest_path: Path,
    provider_runtime_env: dict[str, str] | None = None,
) -> tuple[Any, str, str]:
    """Build one stable, chat-scoped SDK process configuration."""

    # Print mode may receive a per-process projection, but a persistent SDK
    # process never does. Validate a supplied projection as defense in depth,
    # then keep it out of options/env/configuration identity entirely.
    validate_provider_runtime_env(provider_runtime_env)
    env = agent_runner_env(session_id)
    # Claude Agent SDK overlays ``options.env`` onto the server process's
    # environment rather than replacing it.  Missing keys therefore inherit
    # into the provider-controlled CLI even though ``agent_runner_env``
    # removes them.  Explicitly shadow every server/provider authority secret
    # with an empty value at this final transport boundary.
    for inherited_name in tuple(os.environ):
        if (
            inherited_name in PROVIDER_SECRET_ENV_NAMES
            or is_scrubbed_provider_runtime_env_name(inherited_name)
        ):
            env[inherited_name] = ""
    env["HYADEX_CHAT_ID"] = session_id
    subagent_limit = sess.get("subagent_limit")
    subagent_settings: dict[str, Any] = {}
    if type(subagent_limit) is int and subagent_limit > 0:
        subagent_env = {
            "CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS": str(subagent_limit),
        }
        env.update(subagent_env)
        # Native settings env wins over inherited process env. Keep the
        # explicit chat override in its process-only flag-settings layer too.
        subagent_settings["settings"] = json.dumps(
            {"env": subagent_env}, separators=(",", ":"),
        )
    cli_path = claude_sdk_cli_path(env)
    system_prompt = session_system_prompt(session_id, sess, manifest_path)
    provider_id = resolve_claude_resume_provider(sess, cwd)[0]
    if sess.get("fork_resume_session_at") and not (provider_id and sess.get("fork_from")):
        raise ValueError("The completed Claude fork snapshot is unavailable; refusing an empty resume.")
    permission_owner = {"token": ""}
    provider_tool_owner = {"ownership_token": "", "run_id": ""}
    provider_tool_slots = asyncio.Semaphore(
        PROVIDER_TOOL_MAX_CONCURRENT_PER_CLAUDE_CHAT
    )

    async def can_use_tool(
        tool_name: str,
        input_data: dict[str, Any],
        context: Any,
    ) -> Any:
        return await handle_claude_tool_permission(
            session_id,
            tool_name,
            input_data,
            context,
            owner_token=permission_owner["token"],
        )

    def bind_permission_owner(ownership_token: str) -> None:
        permission_owner["token"] = str(ownership_token)

    setattr(can_use_tool, "_hyadex_bind_owner", bind_permission_owner)

    async def run_hyadex_tool(input_data: dict[str, Any]) -> dict[str, Any]:
        ownership_token = str(provider_tool_owner["ownership_token"] or "")
        owner_run_id = str(provider_tool_owner["run_id"] or "")
        try:
            helper, arguments, stdin = validate_provider_tool_input(input_data)
            bounded_input = {
                "helper": helper,
                "arguments": arguments,
                **({"stdin": stdin} if stdin else {}),
            }
            # Claude's SDK dispatches MCP calls concurrently. Fail fast above
            # a small per-chat ceiling instead of allowing one adversarial or
            # confused model response to spawn unbounded helper processes.
            if provider_tool_slots.locked():
                raise ProviderToolError(
                    "too many Hyadex provider actions are already running"
                )
            await provider_tool_slots.acquire()
            try:
                if provider_tool_call_is_read_only(helper, arguments):
                    # The in-process Claude MCP SDK does not expose a transport
                    # call ID. Identical reads must remain live rather than being
                    # mistaken for retries and replaying a stale mailbox/listing.
                    text, is_error = await execute_provider_tool(
                        session_id,
                        owner_run_id,
                        bounded_input,
                        backend=BACKEND_CLAUDE,
                        claude_owner_token=ownership_token,
                    )
                else:
                    replay_digest = hashlib.sha256(
                        json.dumps(
                            bounded_input,
                            sort_keys=True,
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ).encode("utf-8")
                    ).hexdigest()
                    text, is_error = await execute_provider_tool_once(
                        session_id,
                        owner_run_id,
                        bounded_input,
                        replay_key="claude:" + replay_digest,
                        backend=BACKEND_CLAUDE,
                        claude_owner_token=ownership_token,
                    )
            finally:
                provider_tool_slots.release()
        except ProviderToolError as exc:
            text, is_error = str(exc), True
        return {
            "content": [{"type": "text", "text": text}],
            "is_error": is_error,
        }

    hyadex_mcp = create_claude_sdk_mcp_server(
        name=CLAUDE_PROVIDER_MCP_SERVER_NAME,
        version="1.0.0",
        tool_name="run",
        description=PROVIDER_TOOL_DESCRIPTION,
        input_schema=PROVIDER_TOOL_INPUT_SCHEMA,
        handler=run_hyadex_tool,
    )

    extra_args: dict[str, str | None] = {
        "replay-user-messages": None,
        "allow-dangerously-skip-permissions": None,
        "system-prompt-snapshot": "off",
    }
    if provider_id and sess.get("fork_from"):
        extra_args["name"] = f"Fork: {sess.get('title') or sess['id']}"
        if sess.get("fork_resume_session_at"):
            extra_args["resume-session-at"] = str(sess["fork_resume_session_at"])
    options = create_claude_agent_options(
        system_prompt={
            "type": "preset",
            "preset": "claude_code",
            "append": system_prompt,
        },
        permission_mode=effective_claude_permission_mode(sess),
        can_use_tool=can_use_tool,
        hooks=claude_background_tracking_hooks(),
        disallowed_tools=list(CLAUDE_NON_DURABLE_SCHEDULER_TOOLS),
        # This internal tool performs its own exact live-run authorization.
        # Do not make the desktop ask the user to approve the transport
        # wrapper in normal Claude permission modes.
        allowed_tools=[CLAUDE_PROVIDER_MCP_TOOL_NAME],
        model=str(sess.get("model") or "").strip() or None,
        effort=str(sess.get("effort") or "").strip() or None,
        cwd=cwd,
        cli_path=cli_path,
        env=env,
        mcp_servers={CLAUDE_PROVIDER_MCP_SERVER_NAME: hyadex_mcp},
        resume=provider_id,
        fork_session=bool(provider_id and sess.get("fork_from")),
        setting_sources=["user", "project", "local"],
        # Newer Claude models omit user-visible thinking text unless the SDK
        # explicitly requests summaries. Keep adaptive thinking tied to the
        # selected effort while exposing only Claude's provider-approved
        # summaries, never raw hidden chain-of-thought.
        thinking={"type": "adaptive", "display": "summarized"},
        include_partial_messages=False,
        max_buffer_size=PROCESS_STREAM_LIMIT,
        extra_args=extra_args,
        **subagent_settings,
        stderr=lambda line: logger.warning(
            "Claude SDK stderr session=%s: %s",
            session_id,
            compact_memory_text(line, 2_000),
        ),
    )
    def bind_provider_tool_owner(ownership_token: str, run_id: str) -> None:
        provider_tool_owner["ownership_token"] = str(ownership_token)
        provider_tool_owner["run_id"] = str(run_id)

    if isinstance(options, dict):
        options["_hyadex_bind_provider_tool_owner"] = (
            bind_provider_tool_owner
        )
    else:
        setattr(
            options,
            "_hyadex_bind_provider_tool_owner",
            bind_provider_tool_owner,
        )
    return (
        options,
        claude_sdk_configuration_key(sess, cwd, cli_path, system_prompt),
        cli_path,
    )


async def discover_session_provider_commands(
    session_id: str,
    session: dict[str, Any],
    *,
    refresh: bool = False,
) -> tuple[dict[str, Any], ProviderCommandInventory]:
    """Read one backend's native inventory without exposing provider metadata."""

    backend = str(session.get("backend") or DEFAULT_BACKEND).strip().lower()
    requested_cwd = str(session.get("cwd") or DEFAULT_CWD)
    cwd = existing_cwd(requested_cwd)
    fallback_empty = empty_provider_command_inventory(
        backend,
        cwd=cwd,
        selector_secret=PROVIDER_COMMAND_EMPTY_FALLBACK_SECRET,
        binding_context=session_id,
    )
    if backend == BACKEND_CODEX:
        if CODEX_TRANSPORT == CODEX_TRANSPORT_EXEC:
            support = provider_command_support(
                False,
                "unavailable",
                "Local skills require the Codex app-server transport.",
            )
            return (
                provider_command_snapshot(fallback_empty, support),
                fallback_empty,
            )
    elif backend == BACKEND_CLAUDE:
        if CLAUDE_TRANSPORT == CLAUDE_TRANSPORT_PRINT:
            support = provider_command_support(
                False,
                "unavailable",
                "Local commands require the Claude Agent SDK transport.",
            )
            return (
                provider_command_snapshot(fallback_empty, support),
                fallback_empty,
            )
    else:
        support = provider_command_support(
            False,
            "unsupported",
            "This backend does not expose local commands to Hyadex.",
        )
        return provider_command_snapshot(fallback_empty, support), fallback_empty

    try:
        identity = provider_command_selector_secret()
    except Exception:
        support = provider_command_support(
            False,
            "unavailable",
            "Local provider commands are temporarily unavailable.",
        )
        return provider_command_snapshot(fallback_empty, support), fallback_empty
    empty = empty_provider_command_inventory(
        backend,
        cwd=cwd,
        selector_secret=identity,
        binding_context=session_id,
    )

    if backend == BACKEND_CODEX:
        try:
            manager = await codex_app_server_manager(session)
            raw = await manager.request(
                "skills/list",
                {"cwds": [cwd], "forceReload": bool(refresh)},
            )
            inventory = codex_provider_command_inventory(
                raw,
                cwd=cwd,
                selector_secret=identity,
                binding_context=session_id,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            support = provider_command_support(
                False,
                "unavailable",
                "Codex local skills are temporarily unavailable.",
            )
            return provider_command_snapshot(empty, support), empty
        support = provider_command_support(True, "native")
        if inventory.truncated:
            support["reason"] = (
                f"Showing the first {MAX_PROVIDER_COMMANDS} provider skills."
            )
        return provider_command_snapshot(inventory, support), inventory
    if backend == BACKEND_CLAUDE:
        try:
            manager = await claude_sdk_manager()
            if refresh:
                # This is deliberately non-forcing. A refresh may replace an
                # idle cached connection but never interrupts a live turn.
                await manager.evict(session_id, force=False)
            options, configuration_key, _cli_path = build_claude_sdk_options(
                session_id,
                session,
                cwd,
                codex_manifest_path(session_id),
            )
            raw, generation = await manager.get_server_info(
                session_id,
                options=options,
                configuration_key=configuration_key,
            )
            inventory = claude_provider_command_inventory(
                raw,
                cwd=cwd,
                selector_secret=identity,
                binding_context=session_id,
                control_generation=generation,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            support = provider_command_support(
                False,
                "unavailable",
                "Claude local commands are temporarily unavailable.",
            )
            return provider_command_snapshot(empty, support), empty
        support = provider_command_support(True, "native")
        if inventory.truncated:
            support["reason"] = (
                f"Showing the first {MAX_PROVIDER_COMMANDS} provider commands."
            )
        return provider_command_snapshot(inventory, support), inventory
    raise AssertionError(f"unhandled provider-command backend: {backend}")


async def resolve_provider_command_selection(
    session_id: str,
    session: dict[str, Any],
    selection: SkillSelection | None,
    *,
    prompt: str,
    purpose: str | None,
    provider_context_mode: str,
    client_capabilities: list[str],
    refresh_native: bool = False,
) -> ProviderCommandRecord | None:
    """Revalidate an opaque provider command at every actual admission."""

    if selection is None:
        return None
    if purpose is not None or provider_context_mode != "chat":
        raise HTTPException(
            status_code=400,
            detail="provider commands are available only for ordinary chat turns",
        )
    backend = str(session.get("backend") or DEFAULT_BACKEND).strip().lower()
    required_capability = (
        CODEX_INTERACTIVE_CLIENT_CAPABILITY
        if backend == BACKEND_CODEX
        else CLAUDE_SDK_INTERACTIVE_CLIENT_CAPABILITY
        if backend == BACKEND_CLAUDE
        else None
    )
    if required_capability is None:
        raise HTTPException(
            status_code=400,
            detail="the selected backend does not support provider commands",
        )
    if required_capability not in set(client_capabilities):
        raise HTTPException(
            status_code=400,
            detail="this client cannot safely run the selected provider command",
        )
    snapshot, inventory = await discover_session_provider_commands(
        session_id,
        session,
        # Revalidate against the exact current provider generation. Do not
        # evict it here: Claude command inventories can vary across connects.
        refresh=bool(refresh_native and backend == BACKEND_CODEX),
    )
    if snapshot["support"].get("available") is not True:
        deterministic_transport_mismatch = (
            backend == BACKEND_CODEX
            and CODEX_TRANSPORT == CODEX_TRANSPORT_EXEC
        ) or (
            backend == BACKEND_CLAUDE
            and CLAUDE_TRANSPORT == CLAUDE_TRANSPORT_PRINT
        )
        if (
            snapshot["support"].get("mode") == "unsupported"
            or deterministic_transport_mismatch
        ):
            raise ProviderCommandSelectionInvalid(
                detail="the selected backend cannot run provider commands",
            )
        raise ProviderCommandSelectionUnavailable(
            detail="provider command discovery is temporarily unavailable",
        )
    if selection.revision != inventory.revision:
        raise ProviderCommandSelectionInvalid(
            detail="the provider command list changed; choose the command again",
        )
    record = inventory.resolve(selection.id)
    if record is None:
        raise ProviderCommandSelectionInvalid(
            detail="the selected provider command is no longer available",
        )
    invocation = str(record.public.get("invocation") or "")
    if not provider_command_prompt_matches(prompt, invocation):
        raise ProviderCommandSelectionInvalid(
            detail="the selected provider command does not match the message",
        )
    return record


def codex_provider_command_turn_input(
    prompt: str,
    command: ProviderCommandRecord | None,
) -> list[dict[str, Any]]:
    """Translate the slash palette token into Codex native structured input."""

    if command is None:
        return [{"type": "text", "text": prompt, "text_elements": []}]
    name = str(command.native.get("name") or "")
    path = str(command.native.get("path") or "")
    invocation = str(command.public.get("invocation") or "")
    if not name or not path or not provider_command_prompt_matches(prompt, invocation):
        raise ValueError("invalid validated Codex skill selection")
    suffix = prompt[len(invocation):]
    return [
        {"type": "text", "text": f"${name}{suffix}", "text_elements": []},
        {"type": "skill", "name": name, "path": path},
    ]


def build_codex_cmd(
    session_id: str,
    sess: dict[str, Any],
    prompt: str,
    manifest_path: Path,
    *,
    disable_provider_subagents: bool = False,
) -> list[str]:
    provider_id = sess.get("codex_thread_id") or (
        sess.get("session_id") if sess.get("backend") == BACKEND_CODEX else None
    )
    combined_developer_instructions = codex_thread_instructions(session_id, sess)
    model, normalized_effort, effective_service_tier = codex_runtime_settings(
        sess
    )
    cmd = [CODEX_BIN, "exec", *codex_goals_cli_args()]
    if provider_id:
        cmd.append("resume")
    if model:
        cmd.extend(["--model", model])
    if normalized_effort:
        cmd.extend(["-c", f"model_reasoning_effort={normalized_effort}"])
    if effective_service_tier:
        cmd.extend(["-c", f"service_tier={effective_service_tier}"])
    cmd.extend([
        "-c",
        f"developer_instructions={json.dumps(combined_developer_instructions, ensure_ascii=False)}",
    ])
    if disable_provider_subagents:
        # Exec fallback has no first-class tool transport on which to enforce
        # top-level ownership. Its capability lives in the process environment,
        # so provider-created child agents must be disabled at the CLI policy
        # boundary or they would inherit the same authority wholesale.
        cmd.extend([
            "-c",
            "agents.enabled=false",
            "-c",
            "agents.max_concurrent_threads_per_session=1",
        ])
    if provider_id:
        cmd.append(str(provider_id))
    cmd.append("--json")
    cmd.extend(["-c", "model_reasoning_summary=detailed"])
    cmd.extend(["--disable", "image_generation"])
    if not provider_id:
        cmd.append("--skip-git-repo-check")
    cmd.append("--dangerously-bypass-approvals-and-sandbox")
    cmd.append(prompt)
    return cmd


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


def parse_codex_call_arguments(value: Any) -> Any:
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return {}
        with suppress(Exception):
            return json.loads(text)
        return text
    return value if value is not None else {}


def codex_call_tool(call_id: str, name: str, arguments: Any) -> dict[str, Any]:
    parsed = parse_codex_call_arguments(arguments)
    tool_name = str(name or "tool")
    tool_input: Any = parsed
    if tool_name == "exec_command" and isinstance(parsed, dict):
        command = str(parsed.get("cmd") or parsed.get("command") or "")
        tool_input = {"command": command}
        if parsed.get("workdir"):
            tool_input["workdir"] = parsed["workdir"]
    elif tool_name == "apply_patch" and isinstance(parsed, str):
        tool_input = {"patch": parsed}
    elif tool_name == "apply_patch" and isinstance(parsed, dict) and "input" in parsed:
        tool_input = {"patch": parsed.get("input")}
    return {
        "id": call_id or f"tool_{uuid.uuid4().hex[:8]}",
        "name": "Bash" if tool_name == "exec_command" else tool_name,
        "input": tool_input if isinstance(tool_input, dict) else {"value": tool_input},
    }


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


def codex_reasoning_text(payload: dict[str, Any]) -> str:
    text = str(payload.get("text") or "").strip()
    if text:
        return text
    summary = payload.get("summary")
    if isinstance(summary, list):
        parts: list[str] = []
        for item in summary:
            if isinstance(item, dict):
                part = str(item.get("text") or item.get("summary_text") or "").strip()
                if part:
                    parts.append(part)
            elif isinstance(item, str) and item.strip():
                parts.append(item.strip())
        return "\n".join(parts).strip()
    return ""


def codex_app_server_reasoning_summary(payload: dict[str, Any]) -> str:
    """Extract the summary channel without substituting plaintext reasoning."""
    summary = payload.get("summary")
    if not isinstance(summary, list):
        return ""
    parts: list[str] = []
    for item in summary:
        if isinstance(item, dict):
            part = str(item.get("text") or item.get("summary_text") or "").strip()
            if part:
                parts.append(part)
        elif isinstance(item, str) and item.strip():
            parts.append(item.strip())
    return "\n".join(parts).strip()


def codex_app_server_reasoning_plaintext(payload: dict[str, Any]) -> str:
    """Read only plaintext explicitly exposed by the provider; never encrypted data."""
    value = payload.get("text")
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        return "\n".join(part.strip() for part in value if isinstance(part, str) and part.strip())
    content = payload.get("content")
    if isinstance(content, list):
        # App-server's completed ThreadItem uses content: string[]. Retain
        # Responses-style reasoning_text blocks for older compatibility paths.
        parts = [part if isinstance(part, str) else part.get("text", "")
            if isinstance(part, dict) and part.get("type") == "reasoning_text" else ""
            for part in content]
        return "\n".join(part.strip() for part in parts if isinstance(part, str) and part.strip())
    return ""


def codex_exec_agent_message(event: dict[str, Any]) -> tuple[str, str] | None:
    """Return text and phase from every Codex exec agent-message shape."""
    event_type = str(event.get("type") or "")
    if event_type in {"item.started", "item.completed"}:
        item = event.get("item") if isinstance(event.get("item"), dict) else {}
        if (
            event_type == "item.completed"
            and str(item.get("type") or "") == "agent_message"
        ):
            return str(item.get("text") or ""), str(item.get("phase") or "")
        return None
    payload = (
        event.get("payload")
        if isinstance(event.get("payload"), dict)
        else {}
    )
    if (
        event_type == "event_msg"
        and str(payload.get("type") or "") == "agent_message"
    ):
        return (
            str(payload.get("message") or ""),
            str(payload.get("phase") or ""),
        )
    if (
        event_type == "response_item"
        and str(payload.get("type") or "") == "message"
        and str(payload.get("role") or "") == "assistant"
    ):
        return (
            text_from_content(payload.get("content")),
            str(payload.get("phase") or ""),
        )
    return None


class CodexExecMessageBuffer:
    """Reconcile phase-less CLI events with phased rollout records."""

    def __init__(self) -> None:
        self.pending = ""
        self.commentary_emitted: set[str] = set()
        self.final_emitted = False

    @property
    def has_pending(self) -> bool:
        return bool(self.pending)

    def observe(self, text: str, phase: str = "") -> list[tuple[str, str]]:
        if self.final_emitted:
            return []
        normalized = clean_assistant_text(text)
        if not normalized:
            return []
        normalized_phase = str(phase or "").strip()
        if normalized_phase in {"commentary", "final_answer"}:
            outputs: list[tuple[str, str]] = []
            if self.pending and self.pending != normalized:
                outputs.extend(self.flush(final=False))
            elif self.pending == normalized:
                self.pending = ""
            if normalized_phase == "final_answer":
                self.final_emitted = True
                outputs.append(("final_answer", normalized))
                return outputs
            if normalized in self.commentary_emitted:
                return outputs
            self.commentary_emitted.add(normalized)
            outputs.append(("commentary", normalized))
            return outputs
        if normalized == self.pending:
            return []
        outputs = self.flush(final=False)
        self.pending = normalized
        return outputs

    def flush(self, *, final: bool) -> list[tuple[str, str]]:
        text = self.pending
        self.pending = ""
        if not text or self.final_emitted:
            return []
        if final:
            self.final_emitted = True
            return [("final_answer", text)]
        if text in self.commentary_emitted:
            return []
        self.commentary_emitted.add(text)
        return [("commentary", text)]


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


class CodexForkCleanupError(CodexAppServerProtocolError):
    """A created provider fork has no crash-safe cleanup owner yet."""

    def __init__(self, thread_id: str) -> None:
        self.thread_id = str(thread_id or "").strip()
        super().__init__(
            "thread/fork could not establish crash-safe cleanup ownership",
            request_sent=True,
            safe_to_retry=False,
        )


async def fork_codex_thread(
    source_thread_id: str,
    sess: dict[str, Any],
    *,
    last_turn_id: str | None = None,
) -> str:
    cwd = existing_cwd(str(sess.get("cwd") or DEFAULT_CWD))
    manager = await codex_app_server_manager(sess)
    bound_fork_ids: set[str] = set()

    async def journal_created_fork(thread_id: str) -> bool:
        # Persist the provider generation before publishing orphan recovery.
        # Joining this task on cancellation preserves both pieces together.
        bound_fork_ids.add(thread_id)
        return await persist_abandoned_fork_provider_thread(thread_id)

    async def cleanup_created_fork(thread_id: str) -> bool:
        if thread_id in bound_fork_ids:
            return await retire_or_record_failed_codex_fork(thread_id, manager=manager)
        # A failed binding write must not publish an orphan ledger entry that
        # restart recovery could mistake for the normal provider's thread.
        return await retire_failed_codex_fork(thread_id, manager=manager)

    params = {
        **codex_thread_params(sess, cwd),
        "ephemeral": False,
        "excludeTurns": True,
        # A fork can inherit a persistent goal. Keep it dormant until the
        # child chat has its own policy and the inherited goal is reconciled.
        "deferGoalContinuation": True,
    }
    try:
        forked_id = await manager.fork_thread(
            source_thread_id, params,
            **({"last_turn_id": last_turn_id} if last_turn_id else {}),
        )
    except BaseException as fork_exc:
        # app-server can report a late-created child after thread/fork itself
        # timed out or was cancelled.  If deleting that child also failed, the
        # transport preserves its ID on the original exception.  Take durable
        # ownership here before propagating the original failure; otherwise a
        # cancelled HTTP request could strand a provider thread that no local
        # session references.
        unretired_ids = tuple(
            dict.fromkeys(
                str(thread_id or "").strip()
                for thread_id in (
                    getattr(fork_exc, "unretired_fork_thread_ids", ()) or ()
                )
                if str(thread_id or "").strip()
            )
        )
        for unretired_id in unretired_ids:
            journal_task = asyncio.create_task(
                journal_created_fork(unretired_id)
            )
            journaled = False
            try:
                journaled = bool(await asyncio.shield(journal_task))
            except BaseException:
                with suppress(BaseException):
                    journaled = bool(
                        await join_task_despite_caller_cancellation(
                            journal_task
                        )
                    )
            if journaled:
                continue
            cleanup_task = asyncio.create_task(
                cleanup_created_fork(unretired_id)
            )
            cleanup_is_durable = False
            with suppress(BaseException):
                cleanup_is_durable = bool(
                    await join_task_despite_caller_cancellation(cleanup_task)
                )
            if not cleanup_is_durable:
                raise CodexForkCleanupError(unretired_id) from fork_exc
        raise

    # thread/fork creates a persistent provider object before Hyadex has a
    # local child session that can own it. Journal the ID before verification
    # (or any other await) so a process crash cannot leave an undiscoverable
    # provider fork behind.
    journal_task = asyncio.create_task(
        journal_created_fork(forked_id)
    )
    try:
        journaled = await asyncio.shield(journal_task)
    except BaseException as journal_exc:
        journal_is_durable = False
        with suppress(BaseException):
            journal_is_durable = bool(
                await join_task_despite_caller_cancellation(journal_task)
            )
        cleanup_task = asyncio.create_task(
            cleanup_created_fork(forked_id)
        )
        cleanup_is_durable = False
        with suppress(BaseException):
            cleanup_is_durable = bool(
                await join_task_despite_caller_cancellation(cleanup_task)
            )
        if not (journal_is_durable or cleanup_is_durable):
            raise CodexForkCleanupError(forked_id) from journal_exc
        raise
    if not journaled:
        cleanup_task = asyncio.create_task(
            cleanup_created_fork(forked_id)
        )
        cleanup_is_durable = False
        with suppress(BaseException):
            cleanup_is_durable = bool(
                await join_task_despite_caller_cancellation(cleanup_task)
            )
        if not cleanup_is_durable:
            raise CodexForkCleanupError(forked_id)
        raise CodexAppServerProtocolError(
            "thread/fork cleanup journal could not be persisted",
            request_sent=True,
            safe_to_retry=False,
        )

    try:
        forked_thread = await manager.read_thread(
            forked_id,
            include_turns=False,
        )
        if str(forked_thread.get("forkedFromId") or "").strip() != source_thread_id:
            raise CodexAppServerProtocolError(
                "thread/fork ancestry could not be verified",
                request_sent=True,
                safe_to_retry=False,
            )
        forked_cwd = str(forked_thread.get("cwd") or "").strip()
        # Codex canonicalizes cwd, including symlinked workspaces and macOS
        # /tmp -> /private/tmp. Both paths must identify the same workspace.
        if not forked_cwd or os.path.realpath(forked_cwd) != os.path.realpath(cwd):
            raise CodexAppServerProtocolError(
                "thread/fork working directory could not be verified",
                request_sent=True,
                safe_to_retry=False,
            )
        if last_turn_id:
            turns = await manager.list_turns(
                forked_id, limit=1, items_view="summary", sort_direction="desc",
            )
            if (
                not turns
                or str(turns[0].get("id") or "") != last_turn_id
                or str(turns[0].get("status") or "") != "completed"
            ):
                raise CodexAppServerProtocolError(
                    "thread/fork completed-turn boundary could not be verified",
                    request_sent=True, safe_to_retry=False,
                )
        await touch_codex_app_server_thread(manager, forked_id)
        return forked_id
    except BaseException:
        # The ID is known but the child cannot be trusted. It was never exposed
        # to a user, so delete it when possible and otherwise retain the durable
        # cleanup journal for startup recovery.
        cleanup_task = asyncio.create_task(
            cleanup_created_fork(forked_id)
        )
        with suppress(BaseException):
            await join_task_despite_caller_cancellation(cleanup_task)
        raise


async def bind_forked_codex_thread(
    session_id: str,
    thread_id: str,
    sess: dict[str, Any],
    *,
    require_goal_support: bool = False,
    expected_goal: dict[str, Any] | None = None,
) -> tuple[str, str]:
    """Bind a native fork to its child chat's policy before exposing it."""
    native_settings = {**sess, "codex_config_overrides": sanitize_codex_thread_config(
        sess.get("codex_config_overrides") or {}, source=f"session {session_id}")}
    cwd = existing_cwd(str(sess.get("cwd") or DEFAULT_CWD))
    manager = await codex_app_server_manager(sess)
    instructions = codex_thread_instructions(session_id, sess)
    instruction_hash = codex_thread_instruction_hash(session_id, sess)
    await pin_codex_app_server_thread(thread_id, manager)
    bound_thread_id = thread_id
    try:
        if manager.is_thread_loaded(thread_id):
            await manager.unsubscribe_thread(thread_id)
        resumed_thread_id = await manager.resume_thread(
            thread_id,
            {
                **codex_thread_params(
                    native_settings,
                    cwd,
                    developer_instructions=instructions,
                ),
                "excludeTurns": True,
            },
        )
        if resumed_thread_id != thread_id:
            raise CodexAppServerProtocolError(
                "thread/resume changed the forked thread identity",
                request_sent=True,
                safe_to_retry=False,
            )
        bound_thread_id = resumed_thread_id
        # Current Codex releases can defer changed resume instructions until a
        # later turn. Persist the child-scoped policy as a developer item too,
        # before the fork is returned to any client.
        await manager.inject_items(
            bound_thread_id,
            [codex_raw_developer_message(instructions)],
        )
        await STORE.save_provider_session(
            session_id,
            bound_thread_id,
            BACKEND_CODEX,
            codex_instruction_hash=instruction_hash,
        )
        # thread/fork deferred inherited goal continuation. Reconcile now,
        # before the endpoint exposes the child; an active goal is paused
        # because this new chat has no local run owner.
        await reconcile_codex_thread_goal(
            manager,
            session_id,
            bound_thread_id,
            force=True,
            require_goal_support=require_goal_support,
            expected_goal=expected_goal,
        )
        await record_codex_subagent_limit_application(manager, session_id, native_settings)
        await touch_codex_app_server_thread(manager, bound_thread_id)
        return bound_thread_id, instruction_hash
    except Exception:
        # Any failed bind (resume, policy injection, persistence, or goal
        # reconciliation) must leave neither an attached nor a loaded provider
        # fork. The caller can safely fall back to bounded memory afterward.
        async with STORE.transaction():
            current = STORE.sessions.get(session_id)
            if current and session_provider_id(current) == bound_thread_id:
                current["session_id"] = None
                # A hidden staged fork remains the durable recovery owner until
                # its caller either retires the provider or publishes the
                # successfully bound child. Never erase that last reference on
                # a failed policy/goal bind.
                current["codex_thread_id"] = (
                    thread_id if current.get("_fork_initializing") else None
                )
                current.pop("codex_instruction_hash", None)
                current.pop("codex_instruction_version", None)
                await STORE.save()
        if manager.is_thread_loaded(bound_thread_id):
            with suppress(Exception):
                await manager.unsubscribe_thread(bound_thread_id)
        raise
    finally:
        await unpin_codex_app_server_thread(manager, bound_thread_id)


async def retire_failed_codex_fork(
    thread_id: str, *, manager: CodexAppServerManager | None = None,
) -> bool:
    """Unload a provider fork that will not be exposed as an Hyadex chat."""
    clean_thread_id = str(thread_id or "").strip()
    if not clean_thread_id:
        return True
    # Stop routing provider callbacks before remote cleanup yields. This is
    # also used when a staged child survives as a memory fallback after native
    # bind failure, so a late event must not leak into that local child.
    CODEX_THREAD_SESSION_INDEX.pop(clean_thread_id, None)
    deleted = False
    try:
        if manager is None:
            manager = await codex_app_server_manager_for_thread(clean_thread_id)
        try:
            await manager.delete_thread(clean_thread_id)
            deleted = True
        except Exception:
            if manager.is_thread_loaded(clean_thread_id):
                await manager.unsubscribe_thread(clean_thread_id)
    except Exception as exc:
        logger.warning(
            "failed to unload abandoned Codex fork thread=%s: %s",
            clean_thread_id,
            concise_error_message(exc),
        )
    async with CODEX_APP_SERVER_THREAD_LRU_LOCK:
        CODEX_APP_SERVER_THREAD_LRU.pop(clean_thread_id, None)
        CODEX_APP_SERVER_THREAD_PIN_COUNTS.pop(clean_thread_id, None)
        CODEX_APP_SERVER_PINNED_THREADS.discard(clean_thread_id)
        CODEX_APP_SERVER_INVALIDATED_THREADS.discard(clean_thread_id)
    return deleted


async def persist_abandoned_fork_provider_thread(thread_id: str) -> bool:
    """Durably fence a provider fork that could not be retired immediately."""

    clean_thread_id = str(thread_id or "").strip()
    if not clean_thread_id:
        return True
    async with ABANDONED_FORK_PROVIDER_THREADS_LOCK:
        pending = set(ABANDONED_FORK_PROVIDER_THREADS)
        pending.add(clean_thread_id)
        try:
            await asyncio.to_thread(
                write_abandoned_fork_thread_ids,
                pending,
            )
        except OSError as exc:
            # Retain the in-memory fence even though a crash would lose it.
            # A staged local child, when present, remains durable and lets the
            # next startup retry this ledger write before removing the child.
            ABANDONED_FORK_PROVIDER_THREADS.add(clean_thread_id)
            logger.warning(
                "could not persist abandoned provider fork thread=%s: %s",
                clean_thread_id,
                concise_error_message(exc),
            )
            return False
        ABANDONED_FORK_PROVIDER_THREADS.clear()
        ABANDONED_FORK_PROVIDER_THREADS.update(pending)
        return True


async def forget_abandoned_fork_provider_thread(thread_id: str) -> bool:
    """Remove a cleanup fence only after another durable owner exists."""

    clean_thread_id = str(thread_id or "").strip()
    if not clean_thread_id:
        return True
    async with ABANDONED_FORK_PROVIDER_THREADS_LOCK:
        if clean_thread_id not in ABANDONED_FORK_PROVIDER_THREADS:
            return True
        remaining = ABANDONED_FORK_PROVIDER_THREADS - {clean_thread_id}
        try:
            await asyncio.to_thread(
                write_abandoned_fork_thread_ids,
                remaining,
            )
        except OSError as exc:
            logger.warning(
                "could not clear abandoned provider fork thread=%s: %s",
                clean_thread_id,
                concise_error_message(exc),
            )
            return False
        ABANDONED_FORK_PROVIDER_THREADS.clear()
        ABANDONED_FORK_PROVIDER_THREADS.update(remaining)
        return True


async def retire_or_record_failed_codex_fork(
    thread_id: str, *, manager: CodexAppServerManager | None = None,
) -> bool:
    """Make a failed provider fork either deleted or durably recoverable."""

    clean_thread_id = str(thread_id or "").strip()
    if not clean_thread_id:
        return True
    deleted = await retire_failed_codex_fork(
        clean_thread_id, **({"manager": manager} if manager is not None else {}),
    )
    if deleted:
        # Remote deletion itself is durable cleanup. A failed ledger rewrite
        # can leave a harmless stale fence for startup to remove, but must not
        # prevent deletion of the hidden local child.
        await forget_abandoned_fork_provider_thread(clean_thread_id)
        return True
    return await persist_abandoned_fork_provider_thread(clean_thread_id)


def codex_app_server_tool(item: dict[str, Any]) -> dict[str, Any] | None:
    item_type = str(item.get("type") or "")
    item_id = str(item.get("id") or f"tool_{uuid.uuid4().hex[:8]}")
    if item_type == "commandExecution":
        tool_input: dict[str, Any] = {"command": str(item.get("command") or "")}
        if item.get("cwd"):
            tool_input["workdir"] = item.get("cwd")
        return {"id": item_id, "name": "Bash", "input": tool_input}
    if item_type == "fileChange":
        return {
            "id": item_id,
            "name": "apply_patch",
            "input": {"changes": item.get("changes") or []},
        }
    if item_type == "mcpToolCall":
        server = str(item.get("server") or "mcp")
        name = str(item.get("tool") or "tool")
        return {
            "id": item_id,
            "name": f"{server}/{name}",
            "input": item.get("arguments") or {},
        }
    if item_type == "dynamicToolCall":
        namespace = str(item.get("namespace") or "dynamic")
        name = str(item.get("tool") or "tool")
        return {
            "id": item_id,
            "name": f"{namespace}/{name}",
            "input": item.get("arguments") or {},
        }
    if item_type in {"collabAgentToolCall", "collabToolCall"}:
        operation = str(item.get("tool") or "collaboration")
        normalized_operation = re.sub(r"[^a-z0-9]", "", operation.lower())
        receiver_thread_ids = codex_collaboration_thread_ids(item)
        if normalized_operation == "spawnagent":
            return {
                "id": item_id,
                "name": "spawn_agent",
                "input": {
                    "task_name": receiver_thread_ids[0]
                    if receiver_thread_ids
                    else "Codex subagent",
                    "message": str(item.get("prompt") or ""),
                    "model": item.get("model"),
                    "reasoning_effort": item.get("reasoningEffort"),
                },
            }
        return {
            "id": item_id,
            "name": f"Collaboration/{operation}",
            "input": {
                "operation": operation,
                "receiver_thread_ids": receiver_thread_ids,
            },
        }
    if item_type == "webSearch":
        return {
            "id": item_id,
            "name": "WebSearch",
            "input": {"query": item.get("query") or ""},
        }
    if item_type == "imageView":
        return {
            "id": item_id,
            "name": "ViewImage",
            "input": {"path": item.get("path") or ""},
        }
    if item_type == "imageGeneration":
        return {"id": item_id, "name": "ImageGeneration", "input": {}}
    return None


def codex_app_server_tool_output(
    item: dict[str, Any],
    delta_text: str = "",
) -> tuple[Any, int | None, bool]:
    item_type = str(item.get("type") or "")
    status = str(item.get("status") or "").lower()
    is_error = status in {"failed", "declined", "cancelled", "canceled", "errored"}
    if item_type == "commandExecution":
        output = item.get("aggregatedOutput") or delta_text
        exit_code = item.get("exitCode")
        return output, int(exit_code) if isinstance(exit_code, int) else None, is_error
    if item_type == "fileChange":
        return item.get("changes") or delta_text, None, is_error
    if item_type in {"mcpToolCall", "dynamicToolCall"}:
        output = item.get("result") or item.get("contentItems") or item.get("error") or delta_text
        return output, None, is_error or bool(item.get("error")) or item.get("success") is False
    if item_type in {"collabAgentToolCall", "collabToolCall"}:
        return (
            item.get("agentsStates")
            or item.get("agentStatus")
            or status
            or delta_text,
            None,
            is_error,
        )
    if item_type == "webSearch":
        return item.get("action") or item.get("query") or delta_text, None, is_error
    if item_type == "imageView":
        return item.get("path") or delta_text, None, is_error
    if item_type == "imageGeneration":
        return item.get("result") or item.get("savedPath") or delta_text, None, is_error
    return delta_text, None, is_error


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


async def retain_codex_goal_run_owner(
    session_id: str, run_id: str, manager: CodexAppServerManager,
    thread_id: str,
    *, subscription: Any = None, handled_sequence: int = 0,
) -> bool:
    """Keep an ordinary runner supervised when its native goal continues.

    Goal activation and this terminal decision share the lifecycle lock. A
    successful Resume cannot land between the decision and owner release.
    Native tool goal updates are drained before examining provider state.
    """
    await manager.wait_for_notification_handler(
        project_codex_notification, thread_id,
    )
    async with session_lifecycle_lock(session_id):
        async with RUNS.active_lock:
            active = RUNS.active.get(session_id)
            if not (
                active and active.get("run_id") == run_id
                and active.get("provider_thread_id") == thread_id
                and session_id in RUNS.busy
                and (RUNS.current_turns.get(session_id) or {}).get("run_id") == run_id
            ):
                return False
            goal = (STORE.sessions.get(session_id) or {}).get("codex_goal")
            enqueued = getattr(subscription, "_last_enqueued_sequence", 0)
            pending_output = isinstance(enqueued, int) and enqueued > handled_sequence
            retain = bool(
                CODEX_GOALS_ENABLED
                and isinstance(goal, dict)
                and (goal.get("status") == "active" or pending_output)
                and not active.get("stop_requested")
                and run_id not in RUNS.stopped_runs
            )
            active["codex_goal_handoff_closed"] = not retain
            if not retain:
                return False
            # Same supervised task, authority, runtime lease and thread pin.
            # Do not create a synthetic user message or a detached successor.
            active["codex_native_operation"] = True
            active["codex_native_operation_kind"] = "goal_resume"
            active["codex_control_reservation_id"] = run_id
            active["codex_app_server_turn"] = None
            active["provider_turn_id"] = None
            active["provider_turn_ready"] = False
            active["native_interrupt_sent"] = False
            active["native_steer_queue"] = asyncio.Queue(maxsize=1)
            RUNS.current_turns[session_id]["purpose"] = "codex_goal_resume"
            RUNS.current_turns[session_id]["codex_control_reservation_id"] = run_id
            # Register the existing supervisor, not a new task, so Delete and
            # server drain see the same native owner as Stop and Force Send.
            owner = asyncio.current_task()
            if owner is not None:
                register_codex_native_action(session_id, run_id, owner)
            return True


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


CODEX_GOALS_ADMIN_MAX_BODY_BYTES = 256


def register_codex_app_server_child(pid: int, pgid: int | None) -> None:
    with suppress(Exception):
        register_provider_child(pid, pgid, kind="codex-app-server")


def unregister_codex_app_server_child(pid: int, _pgid: int | None) -> None:
    with suppress(Exception):
        unregister_provider_child(pid)


def request_exact_codex_provider_mcp_secret(request: Request) -> bool:
    """Authenticate the process-private Codex MCP transport header."""

    if request.url.query:
        return False
    raw_headers = request.scope.get("headers", [])
    names = [bytes(name).lower() for name, _value in raw_headers]
    forbidden = {
        b"authorization",
        b"cookie",
        b"x-hyadex-token",
        b"x-zenithdock-token",
        b"x-hyadex-provider-capability",
        b"x-hyadex-cross-chat-capability",
    }
    if any(name in forbidden for name in names):
        return False
    expected_name = CODEX_PROVIDER_MCP_HEADER_NAME.lower().encode("ascii")
    values = [
        bytes(value)
        for name, value in raw_headers
        if bytes(name).lower() == expected_name
    ]
    if len(values) != 1:
        return False
    try:
        supplied = values[0].decode("ascii")
    except UnicodeDecodeError:
        return False
    return hmac.compare_digest(supplied, CODEX_PROVIDER_MCP_HEADER_SECRET)


def codex_provider_mcp_tool_definition() -> dict[str, Any]:
    return {
        "name": "run",
        "description": PROVIDER_TOOL_DESCRIPTION,
        "inputSchema": PROVIDER_TOOL_INPUT_SCHEMA,
    }


def codex_provider_mcp_jsonrpc_error(
    request_id: Any,
    code: int,
    message: str,
) -> JSONResponse:
    return JSONResponse({
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": code, "message": message},
    })


def codex_goals_admin_status() -> dict[str, Any]:
    enabled = bool(CODEX_GOALS_ENABLED)
    return {
        "enabled": enabled,
        "configurable": False,
        "default_enabled": bool(CODEX_GOALS_DEFAULT_ENABLED),
        "scope": "server",
        "reconfiguring": bool(CODEX_GOALS_RECONFIGURING),
        "message": "Persistent Codex goals are always available. Manage each goal in its chat.",
    }


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


async def pause_idle_codex_goals_before_disable() -> dict[str, int]:
    paused = 0
    fenced = 0
    def loaded_provider_goal_may_exist(session: dict[str, Any]) -> bool:
        manager = existing_codex_app_server_manager(session)
        thread_id = str(session_provider_id(session) or "").strip()
        return bool(
            manager is not None
            and thread_id
            and manager.is_thread_loaded(thread_id)
        )

    candidates = [
        session_id
        for session_id, session in list(STORE.sessions.items())
        if str(session.get("backend") or DEFAULT_BACKEND).lower() == BACKEND_CODEX
        and (
            isinstance(session.get("codex_goal"), dict)
            or bool(session.get("_codex_goal_reconciliation_required"))
            or loaded_provider_goal_may_exist(session)
        )
    ]
    for session_id in candidates:
        async with session_lifecycle_lock(session_id):
            if (
                session_id in DELETING_SESSIONS
                or session_id not in STORE.sessions
            ):
                continue
            result = await settle_idle_codex_goal_for_stop(session_id)
            if result.get("goal_paused"):
                paused += 1
            if result.get("goal_fenced"):
                fenced += 1
            current_goal = (STORE.sessions.get(session_id) or {}).get("codex_goal")
            if (
                isinstance(current_goal, dict)
                and str(current_goal.get("status") or "").lower() == "active"
            ):
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "Could not safely pause the active Codex goal in "
                        f"{session_id}; retry after stopping that chat"
                    ),
                )
    return {"paused_goal_count": paused, "fenced_goal_count": fenced}


async def get_codex_goals_admin() -> dict[str, Any]:
    return codex_goals_admin_status()


async def put_codex_goals_admin(
    req: CodexGoalsAdminRequest,
) -> dict[str, Any]:
    # Preserve the authenticated legacy route without allowing an older client
    # to disable goals for every chat or restart the shared provider manager.
    if not req.enabled:
        raise HTTPException(
            status_code=409,
            detail="Persistent Codex goals cannot be disabled server-wide. Pause individual goals in their chats.",
        )
    return codex_goals_admin_status()


async def create_native_side_chat(session_id: str, *, persisted_state=None, persist_state=None, durable=False):
    """Bind a side conversation to the provider's actual parent.

    No visible-message projection, main turn, queue or goal mutation belongs
    here. Codex owns a separate native fork; Claude uses native /btw control.
    """
    if SERVER.shutting_down:
        raise side_questions.SideQuestionError(503, "Server is shutting down")
    if not native_session_available(session_id):
        raise side_questions.SideQuestionError(404, "Chat not found")
    session = dict(STORE.sessions[session_id])
    backend = str(session.get("backend") or DEFAULT_BACKEND).lower()
    if backend not in {BACKEND_CODEX, BACKEND_CLAUDE}:
        raise side_questions.SideQuestionError(503, "This backend does not support side questions")
    def provider_id(value):
        return (session_codex_thread_id(value) if backend == BACKEND_CODEX else
                str(value.get("claude_session_id") or value.get("session_id") or "").strip())
    parent_id = provider_id(session)
    if not parent_id:
        raise side_questions.SideQuestionError(409, "The native conversation has not started yet")
    binding = {"backend": backend, "parent_id": parent_id}
    if persisted_state is not None and any(persisted_state.get(key) != value for key, value in binding.items()):
        raise side_questions.SideQuestionError(410, "The main provider conversation changed; clear Side chat")
    if durable and persist_state is not None and persisted_state is None:
        await persist_state(binding)

    class NativeSideChat:
        codex = None

        def current(self):
            if SERVER.shutting_down:
                raise side_questions.SideQuestionError(503, "Server is shutting down")
            if backend == BACKEND_CODEX and CODEX_GOALS_RECONFIGURING:
                raise side_questions.SideQuestionError(409, "Wait for Codex configuration to finish before using Side chat")
            if not native_session_available(session_id):
                raise side_questions.SideQuestionError(404, "Chat not found")
            current = STORE.sessions[session_id]
            if str(current.get("backend") or DEFAULT_BACKEND).lower() != backend or provider_id(current) != parent_id:
                raise side_questions.SideQuestionError(410, "The main provider conversation changed; clear Side chat")
            return current

        async def ask(self, question, *, history):
            current = self.current()
            if backend == BACKEND_CLAUDE:
                from hyadex.providers.claude.sdk import (ClaudeSDKGenerationChanged, ClaudeSDKConfigurationConflict,
                    ClaudeSDKRunActive, ClaudeSDKSupervisorError)
                try:
                    cwd = existing_cwd(str(current.get("cwd") or DEFAULT_CWD))
                    options, configuration_key, _cli_path = build_claude_sdk_options(
                        session_id, current, cwd, codex_manifest_path(session_id))
                    manager = await claude_sdk_manager()
                    result = await manager.ask_side_question(session_id, question, history=history,
                        options=options, configuration_key=configuration_key, expected_provider_id=parent_id)
                except ClaudeSDKGenerationChanged:
                    raise side_questions.SideQuestionError(410, "Claude conversation changed; clear Side chat") from None
                except (ClaudeSDKConfigurationConflict, ClaudeSDKRunActive):
                    raise side_questions.SideQuestionError(409, "Claude conversation is changing; retry") from None
                except ClaudeSDKSupervisorError:
                    raise side_questions.SideQuestionError(503, "Claude native side questions are unavailable; retry after reconnecting") from None
            else:
                from codex_side_question import NativeCodexSideChat
                if self.codex is None:
                    cwd = existing_cwd(str(current.get("cwd") or DEFAULT_CWD))
                    model, effort, service_tier = codex_runtime_settings(current)
                    approval_policy = current.get("codex_approval_policy")
                    if approval_policy not in CODEX_APPROVAL_POLICIES:
                        approval_policy = CODEX_DEFAULT_APPROVAL_POLICY
                    reviewer = current.get("codex_approvals_reviewer")
                    if reviewer not in CODEX_APPROVAL_REVIEWERS:
                        reviewer = CODEX_DEFAULT_APPROVALS_REVIEWER
                    fork_overrides = {
                        "approvalPolicy": approval_policy,
                        "approvalsReviewer": reviewer,
                        "runtimeWorkspaceRoots": [cwd],
                    }
                    turn_overrides = dict(fork_overrides)
                    permission_profile = str(current.get("codex_permission_profile")
                                             or CODEX_DEFAULT_PERMISSION_PROFILE or "").strip()
                    if permission_profile:
                        fork_overrides["permissions"] = permission_profile
                        turn_overrides["permissions"] = permission_profile
                    else:
                        sandbox = current.get("codex_sandbox_mode")
                        if sandbox not in CODEX_SANDBOX_MODES:
                            sandbox = CODEX_DEFAULT_SANDBOX_MODE
                        fork_overrides["sandbox"] = sandbox
                        turn_overrides["sandboxPolicy"] = {"type": CODEX_SANDBOX_POLICY_TYPES[sandbox]}
                    config = flatten_codex_config_overrides(codex_effective_thread_config(current))
                    reserved_prefix = f"mcp_servers.{CODEX_PROVIDER_MCP_NAME}"
                    config = {key: value for key, value in config.items()
                              if key != reserved_prefix and not key.startswith(reserved_prefix + ".")}
                    fork_overrides["config"] = config
                    if effort:
                        turn_overrides["effort"] = effort
                    if service_tier:
                        fork_overrides["serviceTier"] = codex_app_server_service_tier(service_tier)
                        turn_overrides["serviceTier"] = codex_app_server_service_tier(service_tier)

                    async def side_server_request(request_id, method, params):
                        def is_current():
                            try:
                                self.current()
                            except side_questions.SideQuestionError:
                                return False
                            return bool(self.codex is not None and not self.codex.closed
                                        and self.codex.thread_id
                                        and str(params.get("threadId") or "") == self.codex.thread_id)
                        return await handle_codex_server_request(request_id, method, params,
                            side_session_id=session_id, side_owner_is_current=is_current,
                            side_approval_policy=approval_policy)

                    async def save_codex_state(value):
                        if persist_state is not None:
                            await persist_state({**binding, "codex": value})
                    self.codex = NativeCodexSideChat(parent_id, executable=CODEX_BIN,
                        model=model if isinstance(model, str) and model.strip() else None,
                        cwd=cwd, fork_overrides=fork_overrides, turn_overrides=turn_overrides,
                        server_request_handler=side_server_request,
                        env=side_questions.isolated_environment(runner_env()),
                        durable=durable,
                        resume_state=(persisted_state or {}).get("codex"), persist_state=save_codex_state)
                result = {"answer": await self.codex.ask(question),
                          "context_note": "Native Codex context from when Side chat started, including tool results. Clear Side chat to use the latest main context."}
            self.current()
            return {"backend": backend, **result}

        async def close(self):
            if self.codex is not None:
                await self.codex.close()
            # Claude /btw is request-scoped; never close the main SDK client.

    return NativeSideChat()


SIDE_QUESTIONS = side_questions.SideQuestions(native_factory=create_native_side_chat,
    storage_path=STATE_DIR / "side_chats.sqlite3", notify=lambda session_id, event: HUB.broadcast(session_id, event),
    admission_check=require_side_question_admission)


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


def validate_codex_goal_resume_budget(
    session: dict[str, Any], req: CodexGoalRequest,
) -> None:
    if req.status != "active":
        return
    goal = session.get("codex_goal")
    if not isinstance(goal, dict):
        return
    fields = request_fields_set(req)
    if "objective" in fields and str(req.objective or "").strip() != str(goal.get("objective") or "").strip():
        return
    candidate = dict(session)
    if "time_budget_seconds" in fields:
        previous = candidate.get("codex_goal_time_budget_seconds")
        candidate["codex_goal_time_budget_seconds"] = req.time_budget_seconds
        if (
            req.time_budget_seconds is None
            or not isinstance(previous, (int, float))
            or isinstance(previous, bool)
            or req.time_budget_seconds > previous
        ):
            candidate["codex_goal_time_budget_exhausted"] = False
    if codex_goal_time_budget_is_exhausted(candidate):
        raise HTTPException(
            status_code=409,
            detail="The goal's time budget is exhausted. Increase or clear the time limit before resuming.",
        )


async def stop_codex_goal_resume(
    session_id: str,
    manager: CodexAppServerManager,
    thread_id: str,
    reservation_id: str,
) -> None:
    """Fence a failed/cancelled goal activation before releasing its owner."""
    async with RUNS.active_lock:
        active = RUNS.active.get(session_id)
        if (
            not active
            or active.get("codex_native_operation_kind") != "goal_resume"
            or str(active.get("codex_control_reservation_id") or "") != reservation_id
            or str(active.get("provider_thread_id") or "") != thread_id
        ):
            return
        active["stop_requested"] = True
        active_snapshot = dict(active)
    try:
        paused, _changed, error = await pause_active_codex_goal_for_stop(
            session_id, active_snapshot,
        )
    except Exception as exc:
        paused = False
        error = f"Could not persist the paused goal: {concise_error_message(exc)}"
    if not paused:
        try:
            await quarantine_codex_goal_thread(
                session_id, thread_id,
                reason=error or "Could not pause a failed goal resume",
            )
        except Exception as exc:
            logger.warning("failed to quarantine goal resume session=%s thread=%s: %s", session_id, thread_id, concise_error_message(exc))
    # The projector binds early turn/started notifications to this exact
    # reservation even when goal/set fails after accepting the mutation.
    async with RUNS.active_lock:
        active = RUNS.active.get(session_id)
        turn_id = str(active.get("provider_turn_id") or "") if active else ""
        owned = bool(active and str(active.get("codex_control_reservation_id") or "") == reservation_id)
        needs_interrupt = bool(owned and turn_id and not active.get("native_interrupt_sent"))
        if needs_interrupt:
            active["native_interrupt_sent"] = True
    if needs_interrupt:
        try:
            await manager.request(
                "turn/interrupt", {"threadId": thread_id, "turnId": turn_id},
                timeout=CODEX_GOAL_CONTROL_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            async with RUNS.active_lock:
                active = RUNS.active.get(session_id)
                if active and str(active.get("codex_control_reservation_id") or "") == reservation_id:
                    active["native_interrupt_sent"] = False
            try:
                await quarantine_codex_goal_thread(
                    session_id, thread_id,
                    reason=f"Could not interrupt a failed goal resume: {concise_error_message(exc)}",
                )
            except Exception as quarantine_error:
                logger.warning("failed to quarantine interrupted goal session=%s thread=%s: %s", session_id, thread_id, concise_error_message(quarantine_error))


async def quarantine_codex_goal_thread(
    session_id: str,
    thread_id: str,
    *,
    reason: str,
    run_id: str | None = None,
    clear_goal: bool = False,
) -> bool:
    """Fence a provider thread whose goal state cannot be controlled.

    Detaching the provider identity makes the next user turn start a fresh
    thread. The old thread stays in a process-local denylist so late goal
    continuations are interrupted. This is a recovery boundary, not evidence
    that a run remains live.
    """

    thread_id = str(thread_id or "").strip()
    if not thread_id:
        CODEX_GOAL_SYNC_GENERATIONS.pop(session_id, None)
        changed = False
        async with STORE.transaction():
            current = STORE.sessions.get(session_id)
            if current is None:
                return False
            goal = current.get("codex_goal")
            if clear_goal:
                if goal is not None:
                    current["codex_goal"] = None
                    changed = True
                if current.get("codex_goal_time_budget_seconds") is not None:
                    current["codex_goal_time_budget_seconds"] = None
                    changed = True
                if current.get("codex_goal_time_budget_exhausted") is not False:
                    current["codex_goal_time_budget_exhausted"] = False
                    changed = True
            elif (
                isinstance(goal, dict)
                and str(goal.get("status") or "") != "paused"
            ):
                current["codex_goal"] = {**goal, "status": "paused"}
                changed = True
            marker = {
                "thread_id": None,
                "recorded_at": now_iso(),
                "reason": compact_memory_text(reason, 1000),
            }
            current["_codex_goal_reconciliation_required"] = marker
            changed = True
            if changed:
                await STORE.save()
        return True
    CODEX_QUARANTINED_GOAL_THREADS[thread_id] = session_id
    CODEX_GOAL_SYNC_GENERATIONS.pop(session_id, None)
    should_rollover = False
    changed = False
    async with STORE.transaction():
        current = STORE.sessions.get(session_id)
        if current is None:
            return True
        current_thread_id = str(session_provider_id(current) or "").strip()
        # Never let a delayed failure from an old thread overwrite state that
        # has already been rebound to a newer provider thread.
        if current_thread_id not in {"", thread_id}:
            return True
        goal = current.get("codex_goal")
        if clear_goal:
            if goal is not None:
                current["codex_goal"] = None
                changed = True
            if current.get("codex_goal_time_budget_seconds") is not None:
                current["codex_goal_time_budget_seconds"] = None
                changed = True
            if current.get("codex_goal_time_budget_exhausted") is not False:
                current["codex_goal_time_budget_exhausted"] = False
                changed = True
        elif isinstance(goal, dict) and str(goal.get("status") or "") != "paused":
            current["codex_goal"] = {**goal, "status": "paused"}
            changed = True
        marker = {
            "thread_id": thread_id,
            "recorded_at": now_iso(),
            "reason": compact_memory_text(reason, 1000),
        }
        if current.get("_codex_goal_reconciliation_required") != marker:
            current["_codex_goal_reconciliation_required"] = marker
            changed = True
        should_rollover = current_thread_id == thread_id
        if changed:
            await STORE.save()

    if should_rollover:
        await rollover_codex_provider_session(
            session_id,
            run_id or f"goal-fence-{uuid.uuid4().hex[:12]}",
            thread_id,
            reason,
            message=(
                "Detached a Codex thread whose persistent goal could not be "
                "safely reconciled. The next turn will use a fresh thread."
            ),
        )
    logger.warning(
        "quarantined Codex goal thread session=%s thread=%s reason=%s",
        session_id,
        thread_id,
        compact_memory_text(reason, 500),
    )
    return True


async def pause_active_codex_goal_for_stop(
    session_id: str,
    active: dict[str, Any] | None,
) -> tuple[bool, bool, str | None]:
    """Pause native goal continuation before interrupting its current turn.

    Codex goals may start another provider turn immediately after an
    interruption.  A Stop operation therefore is not terminal until the goal
    is paused.  The objective remains persisted so the user can resume it.
    """
    if (
        not active
        or active.get("backend") != BACKEND_CODEX
        or active.get("transport") != CODEX_TRANSPORT_APP_SERVER
    ):
        return True, False, None

    session = STORE.sessions.get(session_id) or {}
    manager = existing_codex_app_server_manager(session)
    thread_id = str(
        active.get("provider_thread_id")
        or active.get("provider_session_id")
        or session_provider_id(session)
        or ""
    ).strip()
    if manager is None or not thread_id:
        cached_goal = session.get("codex_goal")
        if not (
            isinstance(cached_goal, dict)
            and str(cached_goal.get("status") or "") == "active"
        ) and not session.get("_codex_goal_reconciliation_required"):
            return True, False, None
        return (
            False,
            False,
            "The active Codex goal could not be paused because its provider "
            "thread is unavailable. Retry Stop.",
        )

    try:
        goal = await reconcile_codex_thread_goal(
            manager,
            session_id,
            thread_id,
            force=True,
        )
        if (
            not isinstance(goal, dict)
            or str(goal.get("status") or "") != "active"
        ):
            return True, False, None
        paused_goal = await asyncio.wait_for(
            manager.set_thread_goal(thread_id, status="paused"),
            timeout=CODEX_GOAL_CONTROL_TIMEOUT_SECONDS,
        )
        if (
            not isinstance(paused_goal, dict)
            or str(paused_goal.get("status") or "") != "paused"
        ):
            raise CodexAppServerProtocolError(
                "thread/goal/set did not return a paused goal",
                request_sent=True,
                safe_to_retry=False,
            )
    except Exception as exc:
        message = (
            "Timed out while pausing the active Codex goal. Retry Stop."
            if isinstance(exc, asyncio.TimeoutError)
            else f"Could not pause the active Codex goal: {concise_error_message(exc)}"
        )
        logger.warning(
            "Codex goal pause before stop failed session=%s thread=%s error=%s",
            session_id,
            thread_id,
            concise_error_message(exc),
        )
        return False, False, message

    # The native notification normally persists this state too, but persist
    # the direct response before interrupt so a reconnect cannot resurrect an
    # active goal from stale local state.
    await persist_reconciled_codex_goal(
        session_id,
        thread_id,
        paused_goal,
    )
    generation_value = getattr(manager, "generation", 0)
    generation = generation_value if isinstance(generation_value, int) else 0
    CODEX_GOAL_SYNC_GENERATIONS[session_id] = (id(manager), generation, thread_id)
    return True, True, None


async def settle_idle_codex_goal_for_stop(
    session_id: str,
) -> dict[str, Any]:
    """Pause or fence a goal when no local turn owns the chat."""

    session = STORE.sessions.get(session_id) or {}
    if str(session.get("backend") or DEFAULT_BACKEND) != BACKEND_CODEX:
        return {}
    thread_id = str(session_provider_id(session) or "").strip()
    cached_goal = session.get("codex_goal")
    manager = existing_codex_app_server_manager(session)
    should_check_native = bool(
        thread_id
        and manager is not None
        and manager.is_thread_loaded(thread_id)
    )
    if not should_check_native and not (
        isinstance(cached_goal, dict)
        and str(cached_goal.get("status") or "") == "active"
    ) and not session.get("_codex_goal_reconciliation_required"):
        return {}
    synthetic_active = {
        "backend": BACKEND_CODEX,
        "transport": CODEX_TRANSPORT_APP_SERVER,
        "provider_thread_id": thread_id,
        "provider_session_id": thread_id,
    }
    goal_pause_ok, goal_paused, goal_pause_error = (
        await pause_active_codex_goal_for_stop(
            session_id,
            synthetic_active,
        )
    )
    if goal_pause_ok:
        return {"goal_paused": goal_paused}
    fenced = await quarantine_codex_goal_thread(
        session_id,
        thread_id,
        reason=goal_pause_error
        or "Could not reconcile the idle Codex goal while stopping.",
    )
    return {
        "goal_paused": False,
        "goal_fenced": fenced,
        "message": (
            "No local turn was running. The stale Codex goal thread was fenced."
        ),
    }
