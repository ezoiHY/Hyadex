"""Claude provider runtime: SDK manager, options, goals, permissions and context usage."""
from __future__ import annotations

import asyncio
from contextlib import suppress
import copy
import hashlib
import importlib.util
import ipaddress
import json
import logging.handlers
import math
import os
from pathlib import Path
import re
import shutil
import signal
import stat
import subprocess
import time
from typing import Any, Literal
from urllib.parse import urlparse
import uuid

from fastapi import HTTPException
from pydantic import BaseModel, Field

from claude_background_reconciliation import (
    CONSUMED_EVENT as CLAUDE_BACKGROUND_CONSUMED_EVENT,
    normalize_task_receipts,
    pending_reconciliation_state,
    RECONCILED_EVENT as CLAUDE_BACKGROUND_RECONCILED_EVENT,
    SESSION_FIELD as CLAUDE_BACKGROUND_SESSION_FIELD,
)
from claude_goals import ClaudeGoalProjection
from hyadex.core.config import (
    BACKEND_CLAUDE,
    CLAUDE_BIN,
    CLAUDE_CLI_MODEL_ALIASES,
    CLAUDE_CONTEXT_USAGE_TIMEOUT_SECONDS,
    CLAUDE_CURRENT_MODEL_FALLBACKS,
    CLAUDE_LEGACY_MODEL_FALLBACKS,
    CLAUDE_MCP_CONTROL_TIMEOUT_SECONDS,
    CLAUDE_MODEL_VALUE_RE,
    CLAUDE_MODELS_API_MAX_BYTES,
    CLAUDE_PERMISSION_MODE_OPTIONS,
    CLAUDE_PERMISSION_MODES,
    CLAUDE_PROJECTS_ROOT,
    CLAUDE_PROVIDER_SECRET_ENV_NAMES,
    CLAUDE_SDK_CONNECT_TIMEOUT_SECONDS,
    CLAUDE_SDK_IDLE_TTL_SECONDS,
    CLAUDE_SDK_INTERACTIVE_CLIENT_CAPABILITY,
    CLAUDE_SDK_MAX_LOADED_CHATS,
    CLAUDE_TRANSPORT,
    CLAUDE_TRANSPORT_AGENT_SDK,
    CLAUDE_TRANSPORT_PRINT,
    CODEX_SESSION_CLEANUP_TIMEOUT_SECONDS,
    DEFAULT_BACKEND,
    DEFAULT_CWD,
    hyadex_setting,
    MAX_CODEX_PENDING_INTERACTIONS,
    PROCESS_STREAM_LIMIT,
    RUNTIME_CATALOG_DEADLINE,
    RUNTIME_CATALOG_TIMEOUT_SECONDS,
    SERVER_VERSION,
    STOP_CONFIRM_TIMEOUT_SECONDS,
)
from hyadex.core.event_log import project_provider_history_event_for_egress
from hyadex.core.paths import codex_manifest_path, events_path, existing_cwd, now_iso
from hyadex.core.runs import RUNS
from hyadex.core.server_logging import concise_error_message
from hyadex.core.session_files import iter_session_events
from hyadex.core.session_settings import effective_claude_permission_mode
from hyadex.core.lifecycle_admission import server_lifecycle_admission_blocker
from hyadex.core.sessions import (
    append_event,
    CLAUDE_STOP_FENCE_SESSIONS,
    ensure_session_not_deleting,
    event_output_text,
    register_session_task,
    session_lifecycle_lock,
    session_system_prompt,
    STORE,
)
from hyadex.core.state import DELETED_SESSION_TOMBSTONES, DELETING_SESSIONS
from hyadex.core.util import (
    _patch_changed_paths,
    clean_assistant_text,
    normalized_tool_name,
    runner_env,
    SkillSelection,
)
from hyadex.features.files import run_event_metadata
from hyadex.features.usage import (
    bounded_codex_interaction_value,
    broadcast_provider_runtime_changed,
    broadcast_provider_usage_changed,
    clear_provider_context_usage_locked,
    context_usage_runtime_fields,
    current_provider_context_usage_generation,
    join_task_despite_caller_cancellation,
    next_provider_context_usage_generation,
    provider_context_usage_signal,
    PROVIDER_USAGE,
)
from hyadex.providers.authority import agent_runner_env
from hyadex.providers.claude.catalog import (
    CLAUDE_ACCOUNT_USAGE,
    claude_sdk_cli_path,
    run_catalog_command,
)
from hyadex.providers.claude.context_usage import (
    claude_provider_id_for_session,
    claude_sdk_dependency_available,
)
from hyadex.providers.claude.goals import (
    CLAUDE_GOAL_PENDING,
    CLAUDE_GOAL_PROJECTIONS,
    refresh_claude_goal,
)
from hyadex.providers.claude.history import (
    claude_history_candidates,
    claude_transcript_matches_cwd,
    find_claude_history,
    session_provider_id,
)
from hyadex.providers.claude.interactions import (
    cancel_claude_interactions,
    CLAUDE_INTERACTION_HANDLER_TASKS,
    CLAUDE_PENDING_INTERACTIONS,
    CLAUDE_PENDING_INTERACTIONS_LOCK,
    public_claude_interaction,
)
from hyadex.providers.claude.sdk import (
    CLAUDE_NON_DURABLE_SCHEDULER_TOOLS,
    claude_nondurable_scheduler_reason,
    CLAUDE_PROVIDER_MCP_TOOL_NAME,
    ClaudeSDKUnavailable,
    create_claude_agent_options,
    default_claude_sdk_client_factory,
)
from hyadex.providers.claude.sdk_manager import ClaudeSDKSupervisorManager
from hyadex.providers.instructions import CLAUDE_SDK_CONFIGURATION_VERSION
from hyadex.providers.runtime import (
    is_scrubbed_provider_runtime_env_name,
    PROVIDER_SECRET_ENV_NAMES,
)
from hyadex.providers.state import PROVIDERS
from hyadex.providers.claude.subagents import (
    build_claude_subagent_snapshot,
    claude_sdk_field,
    claude_sdk_subagent_lifecycle_raw,
    claude_sdk_type,
    CLAUDE_STOP_FENCE_ATTEMPT_LOCKS,
    CLAUDE_STOP_FENCE_ATTEMPT_TASKS,
)
import provider_usage

logger = logging.getLogger("hyadex-server")


class TurnRequest(BaseModel):
    prompt: str
    client_request_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{8,128}$")
    # Retain attribution when replaying turns accepted before browser sharing
    # was retired. These fields grant no access or browser capability.
    shared_chat_id: str | None = Field(default=None, pattern=r"^interactive_[a-f0-9]{32}$")
    shared_chat_request_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{8,128}$")
    file_ids: list[str] = Field(default_factory=list)
    backend: str | None = None
    model: str | None = None
    effort: str | None = None
    skill_selection: SkillSelection | None = None
    display_prompt: str | None = None
    purpose: str | None = None
    job_id: str | None = None
    job_title: str | None = None
    job_scheduled_run_at: float | None = None
    steer_interrupted_run_id: str | None = None
    client_capabilities: list[str] = Field(default_factory=list, max_length=16)
    # @Chat references and Team targets were retired with cross-chat
    # messaging. A stale client gets a validation error instead of having
    # its routing silently dropped.
    chat_references: list[dict[str, Any]] = Field(default_factory=list, max_length=0)
    team_references: list[dict[str, Any]] = Field(default_factory=list, max_length=0)
    secure_peer_envelope_id: None = None

    @property
    def shared_chat_metadata(self) -> dict[str, str]:
        if not self.shared_chat_id or not self.shared_chat_request_id:
            return {}
        return {"shared_chat_id": self.shared_chat_id,
                "shared_chat_request_id": self.shared_chat_request_id,
                "author_label": "Collaborator"}


# A durable stop marker can fail after the Claude supervisor is already gone.
# Keep one bounded, observable retry owner per chat instead of requiring the
# user to press Stop again to release update/turn admission.
CLAUDE_STOP_FENCE_RETRY_TASKS: dict[str, asyncio.Task[Any]] = {}
CLAUDE_STOP_FENCE_RETRY_DELAYS_SECONDS = (5.0, 15.0, 30.0, 60.0)


CLAUDE_SDK_MANAGER_LOCK = asyncio.Lock()


CLAUDE_NO_SESSION_PERSISTENCE_SUPPORTED: bool | None = None


def explicit_stop_in_progress(session_id: str) -> bool:
    """Return whether explicit Stop still owns turn-admission cleanup."""

    operation = RUNS.explicit_stops.get(session_id)
    return operation is not None and not operation.done()


def detached_stop_in_progress(session_id: str) -> bool:
    """Return whether an inner Stop teardown still owns this session."""

    return any(
        not task.done()
        for task in RUNS.detached_stop_tasks_by_session.get(session_id, ())
    )


def stop_cleanup_in_progress(session_id: str) -> bool:
    """Return whether either layer of explicit Stop can affect this chat."""

    return (
        explicit_stop_in_progress(session_id)
        or detached_stop_in_progress(session_id)
    )


def queued_claude_runtime_matches_active(
    session_id: str,
    selected: dict[str, Any],
    active: dict[str, Any],
) -> bool:
    if CLAUDE_SDK_INTERACTIVE_CLIENT_CAPABILITY not in {
        str(value)
        for value in selected.get("client_capabilities") or []
    }:
        # Native SDK steering may immediately surface an approval/question.
        # Never steer into a client that did not advertise those cards.
        return False
    session = dict(STORE.sessions.get(session_id) or {})
    if selected.get("model") is not None:
        session["model"] = selected.get("model")
    if selected.get("effort") is not None:
        session["effort"] = selected.get("effort")
    try:
        cwd = existing_cwd(str(session.get("cwd") or DEFAULT_CWD))
        cli_path = claude_sdk_cli_path(agent_runner_env(session_id))
        configuration_key = claude_sdk_configuration_key(
            session,
            cwd,
            cli_path,
            session_system_prompt(
                session_id,
                session,
                codex_manifest_path(session_id),
            ),
        )
    except Exception:
        # A changed or currently invalid runtime must go through the ordinary
        # queued-turn path, where setup can surface an actionable error. It
        # must never reuse the active SDK process merely because model/effort
        # happen to match.
        return False
    return configuration_key == str(active.get("configuration_key") or "")


async def mark_claude_sdk_turn_ready_after_ack(
    session_id: str,
    run_id: str,
    handle: Any,
) -> bool:
    """Publish native-steer readiness only after Claude replays this query.

    ``ClaudeSDKClient.query()`` returning proves only that the stdin write was
    accepted. Interrupting before the CLI replays the exact UUID-bearing frame
    makes delivery uncertain, so Force Send must remain deferred until this
    stronger provider boundary. Run and handle identity fence late ACKs from a
    superseded logical turn.
    """

    wait_acknowledged = getattr(handle, "wait_acknowledged", None)
    if not callable(wait_acknowledged):
        return False
    try:
        await wait_acknowledged()
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.debug(
            "Claude replay acknowledgment waiter failed session=%s run=%s: %s",
            session_id,
            run_id,
            concise_error_message(exc),
        )
        return False
    async with RUNS.active_lock:
        active = RUNS.active.get(session_id)
        if (
            not active
            or str(active.get("run_id") or "") != run_id
            or active.get("claude_sdk_run") is not handle
            or active.get("stop_requested")
            or bool(getattr(handle, "done", False))
        ):
            return False
        active["provider_turn_ready"] = True
        active["provider_starting"] = False
        tools_ready = active.get("provider_tools_ready")
        if isinstance(tools_ready, asyncio.Event):
            tools_ready.set()
        return True


def claude_project_dir_for_cwd(cwd: str) -> Path:
    # Historical fast path. Current Claude versions apply additional private
    # sanitization, so resume also validates UUID candidates by transcript cwd.
    project_name = str(Path(cwd).expanduser()).replace("/", "-")
    return CLAUDE_PROJECTS_ROOT / project_name


def claude_resume_file_for_cwd(provider_id: str, cwd: str) -> Path:
    return claude_project_dir_for_cwd(cwd) / f"{provider_id}.jsonl"


def resolve_claude_resume_provider(sess: dict[str, Any], cwd: str) -> tuple[str | None, str | None]:
    provider_id = claude_provider_id_for_session(sess)
    if not provider_id:
        return None, None

    saved_cwd = str(sess.get("claude_session_cwd") or "").strip()
    if saved_cwd and str(Path(saved_cwd).expanduser()) != str(Path(cwd).expanduser()):
        return None, f"Claude resume skipped: provider session {provider_id} was created in {saved_cwd}, not {cwd}."

    expected = claude_resume_file_for_cwd(provider_id, cwd)
    if expected.exists():
        return provider_id, None

    candidates = claude_history_candidates(provider_id)
    if any(claude_transcript_matches_cwd(candidate, cwd) for candidate in candidates):
        return provider_id, None
    if candidates:
        return None, f"Claude resume skipped: provider session {provider_id} is not available for cwd {cwd}."

    return None, f"Claude resume skipped: provider session {provider_id} has no local transcript for cwd {cwd}."


def claude_fork_has_conversation(session_id: str) -> bool:
    """Return whether a provider-less Claude parent has context to preserve."""

    for event in iter_session_events(session_id):
        event = project_provider_history_event_for_egress(event, session_id)
        event_type = str(event.get("type") or "")
        if event_type == "turn_started" and str(event.get("prompt") or "").strip():
            return True
        if event_type == "assistant_text" and str(event.get("text") or "").strip():
            return True
        if event_type == "turn_finished" and str(event.get("result_text") or "").strip():
            return True
    return False


def validated_claude_fork_provider_id(
    parent: dict[str, Any],
    parent_session_id: str,
    cwd: str,
) -> str | None:
    """Fail closed unless a non-empty Claude fork can inherit provider context."""

    requested_provider_id = claude_provider_id_for_session(parent)
    if not requested_provider_id:
        if claude_fork_has_conversation(parent_session_id):
            raise HTTPException(
                status_code=409,
                detail=(
                    "This Claude chat has conversation history but no resumable "
                    "provider session, so it cannot be forked safely. Start a new "
                    "chat or restore the original Claude session transcript first."
                ),
            )
        return None

    resolved_provider_id, resume_error = resolve_claude_resume_provider(parent, cwd)
    if resolved_provider_id != requested_provider_id:
        detail = str(resume_error or "the provider session could not be resolved").strip()
        raise HTTPException(
            status_code=409,
            detail=(
                "This Claude chat cannot be forked without losing its provider "
                f"context. {detail} Restore the transcript or the chat's original "
                "working directory, then retry."
            ),
        )
    return requested_provider_id


CLAUDE_TASK_NOTIFICATION_OPEN = "<task-notification>"
CLAUDE_TASK_NOTIFICATION_CLOSE = "</task-notification>"


def is_claude_task_notification_text(value: Any) -> bool:
    """Recognize a complete legacy Claude workflow-control envelope.

    New transcripts carry the authoritative ``origin.kind`` marker. This
    bounded structural fallback exists only so already-imported beta rows can
    be hidden without rewriting an append-only event log.
    """

    text = str(value or "").strip()
    if (
        not text.startswith(CLAUDE_TASK_NOTIFICATION_OPEN)
        or not text.endswith(CLAUDE_TASK_NOTIFICATION_CLOSE)
    ):
        return False
    inner = text[
        len(CLAUDE_TASK_NOTIFICATION_OPEN) :
        -len(CLAUDE_TASK_NOTIFICATION_CLOSE)
    ]
    return bool(
        re.search(r"<task-id>[^<\r\n]{1,512}</task-id>", inner)
        and (
            "<status>" in inner
            or "<output-file>" in inner
            or "<summary>" in inner
        )
    )


def ensure_provider_manager_factory_admission(*, codex: bool = False) -> None:
    """Reject even read-route lazy starts while provider teardown owns state."""

    blocker = server_lifecycle_admission_blocker()
    if blocker or (codex and (PROVIDERS.codex_manager_closing or PROVIDERS.codex_manager_cleanup_epoch is not None)):
        raise HTTPException(
            status_code=503,
            detail=blocker or "HyadexServer is finishing Codex provider cleanup",
        )


async def observe_claude_provider_usage(session_id: str, generation: str, message: Any) -> None:
    if (PROVIDERS.claude_sdk_manager is not None
            and PROVIDERS.claude_sdk_manager.usage_generation(session_id) == generation
            and PROVIDER_USAGE.observe_claude(session_id, generation, message)):
        # A native rate-limit event means the account allowance moved.
        CLAUDE_ACCOUNT_USAGE["stale"] = True
        await broadcast_provider_usage_changed(session_id, BACKEND_CLAUDE)


async def claude_sdk_manager() -> ClaudeSDKSupervisorManager:
    """Return the bounded registry of independent per-chat Claude clients."""

    ensure_provider_manager_factory_admission()
    manager = PROVIDERS.claude_sdk_manager
    if manager is not None:
        return manager
    async with CLAUDE_SDK_MANAGER_LOCK:
        ensure_provider_manager_factory_admission()
        manager = PROVIDERS.claude_sdk_manager
        if manager is None:
            manager = ClaudeSDKSupervisorManager(
                max_clients=CLAUDE_SDK_MAX_LOADED_CHATS,
                idle_ttl_seconds=CLAUDE_SDK_IDLE_TTL_SECONDS,
                connect_timeout_seconds=CLAUDE_SDK_CONNECT_TIMEOUT_SECONDS,
                control_timeout_seconds=CLAUDE_MCP_CONTROL_TIMEOUT_SECONDS,
                usage_observer=observe_claude_provider_usage,
            )
            PROVIDERS.claude_sdk_manager = manager
        return manager


async def close_claude_sdk_manager() -> None:
    """Resolve pending UI requests and stop every chat-owned Claude process."""

    async with CLAUDE_SDK_MANAGER_LOCK:
        manager = PROVIDERS.claude_sdk_manager
        # Invalidate ownership before resolving existing cards. A late SDK
        # callback from a cancellation-hostile process must fail the manager
        # ownership check instead of registering after this cancellation pass.
        PROVIDERS.claude_sdk_manager = None
    await cancel_claude_interactions(resolution="server_closed")
    interaction_tasks = [
        task
        for tasks in tuple(CLAUDE_INTERACTION_HANDLER_TASKS.values())
        for task in tuple(tasks)
        if not task.done()
    ]
    if interaction_tasks:
        _done, pending = await asyncio.wait(
            interaction_tasks,
            timeout=max(0.01, CODEX_SESSION_CLEANUP_TIMEOUT_SECONDS),
        )
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
    if manager is not None:
        await manager.close_all()


async def cancel_claude_stop_fence_retry(
    session_id: str,
    *,
    clear_fence: bool,
) -> bool:
    """Cancel and join one retry owner during deletion or explicit cleanup."""

    current = asyncio.current_task()
    retry_task = CLAUDE_STOP_FENCE_RETRY_TASKS.pop(session_id, None)
    tasks = {
        task
        for task in (
            retry_task,
            *tuple(CLAUDE_STOP_FENCE_ATTEMPT_TASKS.get(session_id) or ()),
        )
        if task is not None and task is not current and not task.done()
    }
    for task in tasks:
        task.cancel()
    pending: set[asyncio.Task[Any]] = set()
    if tasks:
        _done, pending = await asyncio.wait(
            tasks,
            timeout=max(0.01, STOP_CONFIRM_TIMEOUT_SECONDS),
        )
    if clear_fence:
        async with RUNS.active_lock:
            CLAUDE_STOP_FENCE_SESSIONS.discard(session_id)
            RUNS.maintenance.discard(session_id)
        if not pending:
            CLAUDE_STOP_FENCE_ATTEMPT_LOCKS.pop(session_id, None)
    return not pending


async def interrupt_claude_sdk_run_bounded(
    run: Any,
    *,
    timeout: float | None = None,
) -> bool:
    """Interrupt one SDK run without trusting third-party cancellation.

    ``asyncio.wait_for`` can itself remain blocked when the awaited SDK call
    suppresses cancellation. Use a detached, result-consuming task so Stop,
    Delete, and Force Send retain a hard local bound; callers then retire the
    owning chat supervisor when delivery state is no longer trustworthy.
    """

    limit = max(
        0.01,
        float(
            STOP_CONFIRM_TIMEOUT_SECONDS
            if timeout is None
            else timeout
        ),
    )
    task = asyncio.create_task(run.interrupt())

    def consume_result(completed: asyncio.Task[Any]) -> None:
        if not completed.cancelled():
            with suppress(BaseException):
                completed.exception()

    try:
        done, _pending = await asyncio.wait({task}, timeout=limit)
    except asyncio.CancelledError:
        task.cancel()
        task.add_done_callback(consume_result)
        raise
    if task not in done:
        task.cancel()
        task.add_done_callback(consume_result)
        raise asyncio.TimeoutError("Claude SDK interrupt did not settle in time")
    return bool(task.result())


def claude_supports_no_session_persistence() -> bool:
    """Probe once before using Claude's standalone transcript suppression."""

    global CLAUDE_NO_SESSION_PERSISTENCE_SUPPORTED
    if CLAUDE_NO_SESSION_PERSISTENCE_SUPPORTED is not None:
        return CLAUDE_NO_SESSION_PERSISTENCE_SUPPORTED
    try:
        help_text = run_catalog_command([CLAUDE_BIN, "--help"])
        supported = "--no-session-persistence" in help_text
    except Exception as exc:
        logger.debug("Claude session-persistence probe failed: %s", exc)
        supported = False
    CLAUDE_NO_SESSION_PERSISTENCE_SUPPORTED = supported
    return supported


def claude_sdk_configuration_key(
    sess: dict[str, Any],
    cwd: str,
    cli_path: str,
    system_prompt: str,
) -> str:
    """Hash only process configuration, not the evolving provider session ID."""

    payload = {
        "version": CLAUDE_SDK_CONFIGURATION_VERSION,
        "cwd": cwd,
        "cli_path": cli_path,
        "model": str(sess.get("model") or ""),
        "effort": str(sess.get("effort") or ""),
        "system_prompt": system_prompt,
        "permission_mode": effective_claude_permission_mode(sess),
        "setting_sources": ["user", "project", "local"],
        "background_tracking_hooks": 3,
        "allow_dangerously_skip_permissions": True,
        "replay_user_messages": True,
        "disallowed_tools": list(CLAUDE_NON_DURABLE_SCHEDULER_TOOLS),
        "allowed_tools": [CLAUDE_PROVIDER_MCP_TOOL_NAME],
        "thinking": {"type": "adaptive", "display": "summarized"},
        "hyadex_provider_tool": 1,
    }
    subagent_limit = sess.get("subagent_limit")
    if type(subagent_limit) is int and subagent_limit > 0:
        payload["subagent_limit"] = subagent_limit
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def claude_result_error(event: dict[str, Any]) -> str | None:
    if event.get("type") != "result":
        return None
    errors = event.get("errors")
    if event.get("subtype") == "error_during_execution" or errors:
        if isinstance(errors, list) and errors:
            visible_errors = [
                str(item).strip()
                for item in errors
                if str(item or "").strip() and not is_claude_internal_diagnostic(item)
            ]
            if visible_errors:
                return "; ".join(visible_errors)
            return "Claude stopped before completing the turn."
        result = event.get("result")
        return str(result or "Claude execution failed")
    return None


def is_claude_internal_diagnostic(value: Any) -> bool:
    return str(value or "").strip().startswith("[ede_diagnostic]")


def is_expected_claude_interruption_result(event: dict[str, Any]) -> bool:
    # This classifier is only used after the run has been placed in
    # STOPPED_RUNS. Claude has emitted multiple terminal shapes for that same
    # intentional interruption (including aborted_tools/tool_use and
    # aborted_streaming/null). The internal-only diagnostic is the stable
    # signal; keep any real provider error visible.
    errors = event.get("errors")
    return (
        event.get("type") == "result"
        and event.get("subtype") == "error_during_execution"
        and isinstance(errors, list)
        and bool(errors)
        and all(is_claude_internal_diagnostic(item) for item in errors)
    )


CLAUDE_EMPTY_TURN_ERROR = (
    "Claude returned no response for a non-empty prompt and did not run any "
    "tools. The turn was marked failed; retry the message."
)


def claude_empty_turn_failure_message(
    *,
    prompt: str,
    result_text: str,
    had_tool_activity: bool,
    stopped: bool,
    terminal_result_received: bool,
    existing_error: bool,
) -> str:
    """Return a visible failure for an otherwise silent Claude turn."""

    if (
        terminal_result_received
        and str(prompt or "").strip()
        and not clean_assistant_text(result_text)
        and not had_tool_activity
        and not stopped
        and not existing_error
    ):
        return CLAUDE_EMPTY_TURN_ERROR
    return ""


def claude_event_file_fingerprints(
    session_ids: tuple[str, ...],
) -> tuple[tuple[Any, ...], ...]:
    """Return bounded metadata proving the scanned event inputs are unchanged."""

    fingerprints: list[tuple[Any, ...]] = []
    for session_id in session_ids:
        try:
            stat = events_path(session_id).stat()
        except FileNotFoundError:
            fingerprints.append((session_id, "missing"))
        except OSError:
            fingerprints.append((session_id, "unreadable"))
        else:
            fingerprints.append((
                session_id,
                "file",
                int(stat.st_dev),
                int(stat.st_ino),
                int(stat.st_size),
                int(stat.st_mtime_ns),
                int(stat.st_ctime_ns),
            ))
    return tuple(fingerprints)


async def claude_runtime_snapshot(session_id: str) -> dict[str, Any]:
    session = STORE.sessions.get(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="session not found")
    is_claude = str(session.get("backend") or DEFAULT_BACKEND) == BACKEND_CLAUDE
    sdk_available = claude_sdk_dependency_available()
    configured = CLAUDE_TRANSPORT != CLAUDE_TRANSPORT_PRINT
    manager = PROVIDERS.claude_sdk_manager
    loaded = False
    if manager is not None:
        checker = getattr(manager, "is_loaded", None)
        if callable(checker):
            with suppress(Exception):
                loaded = bool(checker(session_id))
    async with CLAUDE_PENDING_INTERACTIONS_LOCK:
        pending = [
            public_claude_interaction(item)
            for item in CLAUDE_PENDING_INTERACTIONS.values()
            if item.get("session_id") == session_id
            and not item.get("responded")
        ]
    async with RUNS.active_lock:
        active_record = RUNS.active.get(session_id)
        active = bool(
            active_record
            and active_record.get("backend") == BACKEND_CLAUDE
            and active_record.get("transport") == CLAUDE_TRANSPORT_AGENT_SDK
        )
    if active:
        status = {
            "type": "active",
            "activeFlags": ["waitingOnApproval"] if pending else [],
        }
    elif loaded:
        status = {"type": "idle"}
    else:
        status = {"type": "notLoaded"}
    goal = await refresh_claude_goal(session_id, notify=False) if is_claude else None
    return {
        "available": bool(is_claude and configured and sdk_available),
        "transport": CLAUDE_TRANSPORT,
        "interactive_capability": CLAUDE_SDK_INTERACTIVE_CLIENT_CAPABILITY,
        "persisted_session": bool(claude_provider_id_for_session(session)),
        "session_loaded": loaded,
        "goal": goal,
        "goal_starting": session_id in CLAUDE_GOAL_PENDING,
        "goal_loading": bool(is_claude and claude_provider_id_for_session(session)
                             and not getattr(CLAUDE_GOAL_PROJECTIONS.get(session_id), "caught_up", False)),
        "stop_fence_pending": session_id in CLAUDE_STOP_FENCE_SESSIONS,
        "status": status,
        "pending_interactions": pending,
        "policy": {
            "permission_mode": effective_claude_permission_mode(session),
        },
        "permission_modes": list(CLAUDE_PERMISSION_MODE_OPTIONS),
        **context_usage_runtime_fields(session, backend=BACKEND_CLAUDE),
        "features": {
            "native_steer": False,
            "force_send": True,
            "interrupt": True,
            "approvals": True,
            "questions": True,
            "permission_mode_control": True,
            "permission_modes": True,
            "context_usage_refresh": True,
            "goals": bool(is_claude and configured and sdk_available),
        },
        "fallback_transport": CLAUDE_TRANSPORT_PRINT,
    }
