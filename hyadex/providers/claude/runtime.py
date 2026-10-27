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
from hyadex.core.session_titles import server_lifecycle_admission_blocker
from hyadex.core.session_settings import effective_claude_permission_mode
from hyadex.core.session_files import iter_session_events
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
from hyadex.providers.claude.history import (
    claude_history_candidates,
    claude_transcript_matches_cwd,
    find_claude_history,
    session_provider_id,
)
from hyadex.providers.claude.sdk import (
    CLAUDE_NON_DURABLE_SCHEDULER_TOOLS,
    claude_nondurable_scheduler_reason,
    CLAUDE_PROVIDER_MCP_TOOL_NAME,
    ClaudeSDKSupervisorManager,
    ClaudeSDKUnavailable,
    create_claude_agent_options,
    default_claude_sdk_client_factory,
)
from hyadex.providers.instructions import CLAUDE_SDK_CONFIGURATION_VERSION
from hyadex.providers.runtime import (
    is_scrubbed_provider_runtime_env_name,
    PROVIDER_SECRET_ENV_NAMES,
)
from hyadex.providers.state import PROVIDERS
from hyadex.providers.subagents import (
    build_claude_subagent_snapshot,
    claude_sdk_field,
    claude_sdk_subagent_lifecycle_raw,
    claude_sdk_type,
    CLAUDE_STOP_FENCE_ATTEMPT_LOCKS,
    CLAUDE_STOP_FENCE_ATTEMPT_TASKS,
)
import provider_usage

logger = logging.getLogger("hyadex-server")


def tool_changed_paths(tool: dict[str, Any]) -> set[str]:
    """Return only paths explicitly owned by a mutating provider tool call."""
    name = normalized_tool_name(tool)
    tool_input = tool.get("input") if isinstance(tool.get("input"), dict) else {}
    paths: set[str] = set()
    direct_path_tools = {
        "edit", "write", "multiedit", "multi_edit", "notebookedit", "notebook_edit",
        "str_replace_editor", "create", "create_file", "delete", "delete_file",
        "move", "move_file", "rename", "rename_file",
    }
    if name in direct_path_tools:
        for key in (
            "file_path", "path", "notebook_path", "old_path", "new_path",
            "source_path", "destination_path", "from", "to",
        ):
            value = tool_input.get(key)
            if isinstance(value, str) and value.strip():
                paths.add(value.strip())
    if name in {"apply_patch", "applypatch", "patch"}:
        patch = tool_input.get("patch") or tool_input.get("input") or tool_input.get("value")
        if isinstance(patch, str):
            paths.update(_patch_changed_paths(patch))
    elif name in {"exec", "shell", "run_javascript", "javascript"}:
        for key in ("value", "code", "script", "input", "command"):
            source = tool_input.get(key)
            if isinstance(source, str) and "apply_patch" in source and "***" in source:
                paths.update(_patch_changed_paths(source))
    return paths


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


def active_claude_permission_mode(
    session_id: str,
    active: dict[str, Any] | None,
) -> str:
    """Return the Claude mode frozen when the current turn was admitted.

    Session settings may be edited during a turn for use by the next turn.
    Permission callbacks for the current turn must not consult that newer
    value, because switching to bypassPermissions would otherwise broaden an
    already-running provider process.
    """

    captured = str((active or {}).get("claude_permission_mode") or "")
    if captured in CLAUDE_PERMISSION_MODES:
        return captured
    # Compatibility for an in-memory owner created by an older development
    # build. New turns always carry the captured field below.
    return effective_claude_permission_mode(
        STORE.sessions.get(session_id) or {}
    )


class ClaudeInteractionResponseRequest(BaseModel):
    response: dict[str, Any]


# A durable stop marker can fail after the Claude supervisor is already gone.
# Keep one bounded, observable retry owner per chat instead of requiring the
# user to press Stop again to release update/turn admission.
CLAUDE_STOP_FENCE_RETRY_TASKS: dict[str, asyncio.Task[Any]] = {}
CLAUDE_STOP_FENCE_RETRY_DELAYS_SECONDS = (5.0, 15.0, 30.0, 60.0)


CLAUDE_SDK_MANAGER_LOCK = asyncio.Lock()


CLAUDE_PENDING_INTERACTIONS: dict[str, dict[str, Any]] = {}
CLAUDE_PENDING_INTERACTIONS_LOCK = asyncio.Lock()


CLAUDE_INTERACTION_HANDLER_TASKS: dict[str, set[asyncio.Task[Any]]] = {}
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


async def mark_provider_turn_ready(
    session_id: str,
    run_id: str,
    provider_session_id: str | None = None,
) -> None:
    """Mark that the provider accepted this turn before it can be interrupted."""
    if not provider_session_id:
        return
    async with RUNS.active_lock:
        active = RUNS.active.get(session_id)
        if not active or str(active.get("run_id") or "") != run_id:
            return
        active["provider_turn_ready"] = True
        active["provider_session_id"] = provider_session_id


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


def claude_provider_id_for_session(sess: dict[str, Any]) -> str | None:
    provider_id = sess.get("claude_session_id") or (
        sess.get("session_id") if sess.get("backend") == BACKEND_CLAUDE else None
    )
    if sess.get("fork_from"):
        provider_id = sess["fork_from"]
    provider_id = str(provider_id or "").strip()
    return provider_id or None


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


def claude_context_usage_snapshot(
    usage: Any,
    *,
    provider_session_id: str,
    run_id: str | None,
    provider_generation: int,
    snapshot_at: str | None = None,
) -> dict[str, Any] | None:
    if not isinstance(usage, dict):
        return None

    def token_count(value: Any) -> int | None:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        if not math.isfinite(float(value)):
            return None
        return max(0, int(value))

    context_tokens = token_count(usage.get("totalTokens"))
    effective_window = token_count(usage.get("maxTokens"))
    raw_window = token_count(usage.get("rawMaxTokens"))
    if context_tokens is None or not effective_window:
        return None
    percentage = usage.get("percentage")
    if (
        isinstance(percentage, bool)
        or not isinstance(percentage, (int, float))
        or not math.isfinite(float(percentage))
    ):
        context_percent = round(context_tokens * 100 / effective_window, 2)
    else:
        context_percent = round(max(0.0, min(100.0, float(percentage))), 2)
    snapshot: dict[str, Any] = {
        "totalTokens": context_tokens,
        "maxTokens": effective_window,
        "rawMaxTokens": raw_window or effective_window,
        "percentage": context_percent,
        "context_tokens": context_tokens,
        "context_window": effective_window,
        "effective_context_window": effective_window,
        "raw_context_window": raw_window or effective_window,
        "context_percent": context_percent,
        "model": str(usage.get("model") or "")[:240] or None,
        "snapshot_at": snapshot_at or now_iso(),
        "provider_session_id": provider_session_id,
        "run_id": run_id,
        "provider_generation": max(0, int(provider_generation)),
        "context_usage_state": "available",
    }
    return {key: value for key, value in snapshot.items() if value is not None}


async def evict_failed_idle_claude_context_sample(
    session_id: str,
    manager: Any,
) -> None:
    """Retire the sampled idle actor before releasing its lifecycle lock.

    A canceled context-usage request can leave the SDK actor waiting on a
    provider response. Remove only the manager instance that issued this
    sample. The caller owns the session lifecycle lock, so this helper must
    never leave a late eviction task that could race a replacement turn.
    """

    if PROVIDERS.claude_sdk_manager is not manager:
        return
    evict = getattr(manager, "evict", None)
    if not callable(evict):
        return
    try:
        task = asyncio.create_task(evict(session_id, force=True))
    except Exception:
        return
    try:
        await asyncio.shield(task)
    except asyncio.CancelledError:
        # The caller still owns session_lifecycle_lock. Finish retirement
        # before propagating cancellation so a queued start cannot bind a new
        # actor and then be killed by this stale cleanup.
        with suppress(BaseException):
            await join_task_despite_caller_cancellation(task)
        raise
    except Exception:
        # Cleanup is best-effort, but it is never detached.
        return


async def refresh_idle_claude_context_usage(session_id: str) -> bool:
    """Ask an already-loaded, idle Claude SDK client for fresh usage.

    This is deliberately observational: it never creates or resumes a Claude
    client, never runs beside a provider turn, and never clears the last good
    snapshot when the auxiliary SDK RPC fails.
    """

    async with session_lifecycle_lock(session_id):
        ensure_session_not_deleting(session_id)
        session = STORE.sessions.get(session_id)
        if not session:
            raise HTTPException(status_code=404, detail="session not found")
        if str(session.get("backend") or DEFAULT_BACKEND) != BACKEND_CLAUDE:
            raise HTTPException(
                status_code=409,
                detail="Claude context usage requires a Claude chat",
            )
        if (
            CLAUDE_TRANSPORT == CLAUDE_TRANSPORT_PRINT
            or not claude_sdk_dependency_available()
        ):
            raise HTTPException(
                status_code=503,
                detail="Claude context refresh requires the Agent SDK transport",
            )

        manager = PROVIDERS.claude_sdk_manager
        checker = getattr(manager, "is_loaded", None) if manager else None
        if not callable(checker):
            raise HTTPException(
                status_code=409,
                detail=(
                    "Claude is not loaded; context usage will refresh after "
                    "the next SDK turn"
                ),
            )
        try:
            loaded = bool(checker(session_id))
        except Exception as exc:
            raise HTTPException(
                status_code=503,
                detail="Claude context state could not be inspected",
            ) from exc
        if not loaded:
            raise HTTPException(
                status_code=409,
                detail=(
                    "Claude is not loaded; context usage will refresh after "
                    "the next SDK turn"
                ),
            )

        async with RUNS.active_lock:
            provider_starting = any(
                not task.done()
                for task in tuple(RUNS.turn_tasks.get(session_id) or ())
            )
            if (
                session_id in RUNS.busy
                or session_id in RUNS.maintenance
                or RUNS.active.get(session_id) is not None
                or RUNS.current_turns.get(session_id) is not None
                or provider_starting
            ):
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "wait for the active Claude turn to finish before "
                        "refreshing context usage"
                    ),
                )

        async with STORE.transaction():
            current = STORE.sessions.get(session_id)
            if not current:
                raise HTTPException(status_code=404, detail="session not found")
            provider_session_id = str(
                claude_provider_id_for_session(current) or ""
            ).strip()
            expected_usage_generation = current_provider_context_usage_generation(
                current
            )
        if not provider_session_id:
            raise HTTPException(
                status_code=409,
                detail=(
                    "Claude has no persisted SDK session yet; context usage "
                    "will refresh after the next turn"
                ),
            )

        getter = getattr(manager, "get_context_usage", None)
        if not callable(getter):
            raise HTTPException(
                status_code=503,
                detail="This HyadexServer cannot sample Claude context usage",
            )
        try:
            sample_task = asyncio.create_task(
                getter(session_id, ownership_token=None)
            )
        except Exception as exc:
            raise HTTPException(
                status_code=502,
                detail="Claude context sampling could not start",
            ) from exc
        try:
            done, _pending = await asyncio.wait(
                {sample_task},
                timeout=CLAUDE_CONTEXT_USAGE_TIMEOUT_SECONDS,
            )
        except asyncio.CancelledError:
            sample_task.cancel()
            sample_task.add_done_callback(
                lambda completed: (
                    None if completed.cancelled() else completed.exception()
                )
            )
            cleanup_task = asyncio.create_task(
                evict_failed_idle_claude_context_sample(session_id, manager)
            )
            with suppress(BaseException):
                await join_task_despite_caller_cancellation(cleanup_task)
            raise
        if sample_task not in done:
            sample_task.cancel()
            sample_task.add_done_callback(
                lambda completed: (
                    None if completed.cancelled() else completed.exception()
                )
            )
            await evict_failed_idle_claude_context_sample(session_id, manager)
            raise HTTPException(
                status_code=504,
                detail="Claude context sampling timed out; the idle SDK client was reset",
            )
        if sample_task.cancelled():
            await evict_failed_idle_claude_context_sample(session_id, manager)
            raise HTTPException(
                status_code=502,
                detail="Claude context sampling was canceled",
            )
        try:
            sampled = sample_task.result()
        except Exception as exc:
            await evict_failed_idle_claude_context_sample(session_id, manager)
            raise HTTPException(
                status_code=502,
                detail=f"Claude context sampling failed: {concise_error_message(exc)}",
            ) from exc
        if isinstance(sampled, tuple) and len(sampled) == 2:
            usage, provider_generation = sampled
        elif isinstance(sampled, dict):
            # Compatibility for early manager adapters.
            usage, provider_generation = sampled, 0
        else:
            await evict_failed_idle_claude_context_sample(session_id, manager)
            raise HTTPException(
                status_code=502,
                detail="Claude did not return context usage",
            )
        try:
            snapshot = claude_context_usage_snapshot(
                usage,
                provider_session_id=provider_session_id,
                run_id=None,
                provider_generation=int(provider_generation),
            )
        except (TypeError, ValueError, OverflowError):
            snapshot = None
        if snapshot is None:
            await evict_failed_idle_claude_context_sample(session_id, manager)
            raise HTTPException(
                status_code=502,
                detail="Claude returned invalid context usage",
            )

        signal: dict[str, Any] | None = None
        async with STORE.transaction():
            current = STORE.sessions.get(session_id)
            if (
                current is None
                or str(current.get("backend") or DEFAULT_BACKEND)
                != BACKEND_CLAUDE
                or str(claude_provider_id_for_session(current) or "")
                != provider_session_id
                or current_provider_context_usage_generation(current)
                != expected_usage_generation
            ):
                return False
            stored = dict(snapshot)
            generation = next_provider_context_usage_generation(current)
            stored["usage_generation"] = generation
            current["context_usage_state"] = "available"
            current["context_usage_snapshot"] = stored
            current["claude_context_usage_snapshot"] = stored
            await STORE.save()
            signal = provider_context_usage_signal(
                BACKEND_CLAUDE,
                state="available",
                generation=generation,
                provider_session_id=provider_session_id,
                snapshot=stored,
            )
        if signal is not None:
            await broadcast_provider_runtime_changed(session_id, signal)
        return signal is not None


async def record_claude_context_usage(
    session_id: str,
    snapshot: dict[str, Any],
    *,
    ownership_token: str,
) -> bool:
    signal: dict[str, Any] | None = None
    run_id = str(snapshot.get("run_id") or "")
    provider_id = str(snapshot.get("provider_session_id") or "")
    async with session_lifecycle_lock(session_id):
        if (
            session_id in DELETING_SESSIONS
            or session_id in DELETED_SESSION_TOMBSTONES
        ):
            return False
        async with RUNS.active_lock:
            active = RUNS.active.get(session_id)
            if (
                not active
                or str(active.get("run_id") or "") != run_id
                or active.get("backend") != BACKEND_CLAUDE
                or active.get("transport") != CLAUDE_TRANSPORT_AGENT_SDK
                or bool(active.get("stop_requested"))
                or str(active.get("claude_sdk_owner_token") or "")
                != ownership_token
            ):
                return False
        async with STORE.transaction():
            session = STORE.sessions.get(session_id)
            if (
                session is None
                or str(session.get("backend") or DEFAULT_BACKEND)
                != BACKEND_CLAUDE
                or str(claude_provider_id_for_session(session) or "")
                != provider_id
            ):
                return False
            stored = dict(snapshot)
            generation = next_provider_context_usage_generation(session)
            stored["usage_generation"] = generation
            session["context_usage_state"] = "available"
            session["context_usage_snapshot"] = stored
            session["claude_context_usage_snapshot"] = stored
            await STORE.save()
            signal = provider_context_usage_signal(
                BACKEND_CLAUDE,
                state="available",
                generation=generation,
                provider_session_id=provider_id,
                snapshot=stored,
            )
    if signal is not None:
        await broadcast_provider_runtime_changed(session_id, signal)
    return signal is not None


async def invalidate_failed_claude_context_usage_sample(
    session_id: str,
    run_id: str,
    provider_session_id: str,
    *,
    ownership_token: str,
    expected_usage_generation: int,
) -> bool:
    """Clear only the still-current snapshot rejected by one terminal sample."""

    signal: dict[str, Any] | None = None
    async with session_lifecycle_lock(session_id):
        if (
            session_id in DELETING_SESSIONS
            or session_id in DELETED_SESSION_TOMBSTONES
        ):
            return False
        async with RUNS.active_lock:
            active = RUNS.active.get(session_id)
            if (
                not active
                or str(active.get("run_id") or "") != run_id
                or active.get("backend") != BACKEND_CLAUDE
                or active.get("transport") != CLAUDE_TRANSPORT_AGENT_SDK
                or str(active.get("claude_sdk_owner_token") or "")
                != ownership_token
            ):
                return False
        async with STORE.transaction():
            session = STORE.sessions.get(session_id)
            if (
                session is None
                or str(session.get("backend") or DEFAULT_BACKEND)
                != BACKEND_CLAUDE
                or str(claude_provider_id_for_session(session) or "")
                != provider_session_id
                or current_provider_context_usage_generation(session)
                != expected_usage_generation
            ):
                return False
            signal = clear_provider_context_usage_locked(
                session,
                BACKEND_CLAUDE,
                provider_session_id=provider_session_id,
                state="unavailable",
            )
            await STORE.save()
    if signal is not None:
        await broadcast_provider_runtime_changed(session_id, signal)
    return signal is not None


async def sample_claude_context_usage(
    session_id: str,
    run_id: str,
    provider_session_id: str,
    manager: Any,
) -> bool:
    if not provider_session_id:
        return False
    async with RUNS.active_lock:
        active = RUNS.active.get(session_id)
        if (
            not active
            or str(active.get("run_id") or "") != run_id
            or active.get("backend") != BACKEND_CLAUDE
            or active.get("transport") != CLAUDE_TRANSPORT_AGENT_SDK
        ):
            return False
        ownership_token = str(active.get("claude_sdk_owner_token") or "")
        stop_requested = bool(active.get("stop_requested"))
    if not ownership_token:
        return False
    async with STORE.transaction():
        session = STORE.sessions.get(session_id)
        if (
            session is None
            or str(session.get("backend") or DEFAULT_BACKEND)
            != BACKEND_CLAUDE
            or str(claude_provider_id_for_session(session) or "")
            != provider_session_id
        ):
            return False
        expected_usage_generation = current_provider_context_usage_generation(
            session
        )

    async def invalidate_failure() -> bool:
        return await invalidate_failed_claude_context_usage_sample(
            session_id,
            run_id,
            provider_session_id,
            ownership_token=ownership_token,
            expected_usage_generation=expected_usage_generation,
        )

    if stop_requested:
        await invalidate_failure()
        return False

    getter = getattr(manager, "get_context_usage", None)
    if not callable(getter):
        await invalidate_failure()
        return False
    try:
        task = asyncio.create_task(
            getter(session_id, ownership_token=ownership_token)
        )
    except Exception:
        await invalidate_failure()
        return False
    done, _pending = await asyncio.wait(
        {task},
        timeout=CLAUDE_CONTEXT_USAGE_TIMEOUT_SECONDS,
    )
    if task not in done:
        task.cancel()
        task.add_done_callback(
            lambda completed: (
                None
                if completed.cancelled()
                else completed.exception()
            )
        )
        evict = getattr(manager, "evict", None)
        if callable(evict):
            eviction_task = asyncio.create_task(
                evict(session_id, force=True)
            )
            evicted, _still_evicting = await asyncio.wait(
                {eviction_task},
                timeout=min(0.5, CLAUDE_CONTEXT_USAGE_TIMEOUT_SECONDS),
            )
            if eviction_task in evicted:
                await asyncio.gather(eviction_task, return_exceptions=True)
            else:
                # Manager eviction removes this registry owner before awaiting
                # bounded SDK teardown. Let that cleanup finish independently;
                # telemetry must never delay turn finalization indefinitely.
                eviction_task.add_done_callback(
                    lambda completed: (
                        None
                        if completed.cancelled()
                        else completed.exception()
                    )
                )
        await invalidate_failure()
        return False
    try:
        sampled = task.result()
    except Exception as exc:
        logger.debug(
            "Claude context usage sampling failed session=%s: %s",
            session_id,
            concise_error_message(exc),
        )
        await invalidate_failure()
        return False
    if not sampled:
        await invalidate_failure()
        return False
    if isinstance(sampled, tuple) and len(sampled) == 2:
        usage, provider_generation = sampled
    elif isinstance(sampled, dict):
        # Compatibility for test/older manager adapters.
        usage, provider_generation = sampled, 0
    else:
        await invalidate_failure()
        return False
    try:
        snapshot = claude_context_usage_snapshot(
            usage,
            provider_session_id=provider_session_id,
            run_id=run_id,
            provider_generation=int(provider_generation),
        )
    except (TypeError, ValueError, OverflowError):
        snapshot = None
    if snapshot is None:
        await invalidate_failure()
        return False
    recorded = await record_claude_context_usage(
        session_id,
        snapshot,
        ownership_token=ownership_token,
    )
    if not recorded:
        # Stop may win after the RPC returns but before its success can be
        # stored. Clear only the pre-sample generation if this exact run still
        # owns the chat; all newer-run/provider/generation races fail closed.
        await invalidate_failure()
    return recorded


def public_claude_interaction(pending: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": pending["id"],
        "session_id": pending["session_id"],
        "thread_id": pending.get("thread_id") or pending["session_id"],
        "turn_id": pending.get("turn_id"),
        "item_id": pending.get("item_id"),
        "method": pending["method"],
        "params": pending["params"],
        "created_at": pending["created_at"],
        "auto_resolution_ms": None,
    }


async def claude_run_has_pending_interaction(
    session_id: str,
    run_id: str,
) -> bool:
    """Return whether this exact Claude run is waiting for the user.

    The interaction registry is the ownership authority. Reading it under its
    lock prevents a just-resolved approval from extending an unrelated or
    replacement run's watchdog lease.
    """

    async with CLAUDE_PENDING_INTERACTIONS_LOCK:
        return any(
            str(pending.get("session_id") or "") == session_id
            and str(pending.get("turn_id") or "") == run_id
            and not pending.get("responded")
            and isinstance(pending.get("future"), asyncio.Future)
            and not pending["future"].done()
            for pending in CLAUDE_PENDING_INTERACTIONS.values()
        )


async def update_claude_pending_session_metadata(session_id: str) -> None:
    async with CLAUDE_PENDING_INTERACTIONS_LOCK:
        count = sum(
            1
            for pending in CLAUDE_PENDING_INTERACTIONS.values()
            if pending.get("session_id") == session_id
            and not pending.get("responded")
        )
    async with STORE.transaction():
        session = STORE.sessions.get(session_id)
        if not session:
            return
        changed = (
            int(session.get("claude_pending_interaction_count") or 0) != count
            or bool(session.get("claude_needs_user_action")) != bool(count)
        )
        session["claude_pending_interaction_count"] = count
        session["claude_needs_user_action"] = bool(count)
        if changed:
            session["updated_at"] = now_iso()
            await STORE.save()


def validate_claude_interaction_response(
    pending: dict[str, Any],
    response: dict[str, Any],
) -> dict[str, Any]:
    method = str(pending.get("method") or "")
    params = pending.get("params") if isinstance(pending.get("params"), dict) else {}
    if method == "item/tool/requestUserInput":
        if set(response) != {"answers"} or not isinstance(response.get("answers"), dict):
            raise HTTPException(status_code=400, detail="question response requires an answers map")
        questions = {
            str(question.get("id") or ""): question
            for question in params.get("questions") or []
            if isinstance(question, dict) and question.get("id")
        }
        answers: dict[str, dict[str, list[str]]] = {}
        for question_id, answer in response["answers"].items():
            question = questions.get(str(question_id))
            if question is None:
                raise HTTPException(status_code=400, detail=f"unknown question id: {question_id}")
            values = answer.get("answers") if isinstance(answer, dict) else None
            if (
                not isinstance(values, list)
                or len(values) > 20
                or any(not isinstance(value, str) or len(value) > 8_000 for value in values)
                or (not bool(question.get("multiSelect")) and len(values) > 1)
            ):
                raise HTTPException(status_code=400, detail=f"invalid answer for {question_id}")
            answers[str(question_id)] = {"answers": list(values)}
        return {"answers": answers}

    if set(response) != {"decision"}:
        raise HTTPException(status_code=400, detail="approval response requires only decision")
    decision = str(response.get("decision") or "")
    allowed = {"accept", "decline", "cancel"}
    if pending.get("session_permission_suggestions"):
        allowed.add("acceptForSession")
    if decision not in allowed:
        raise HTTPException(status_code=400, detail="invalid or unavailable approval decision")
    return {"decision": decision}


async def resolve_claude_interaction(
    session_id: str,
    interaction_id: str,
    response: dict[str, Any],
) -> dict[str, Any]:
    async with CLAUDE_PENDING_INTERACTIONS_LOCK:
        pending = CLAUDE_PENDING_INTERACTIONS.get(interaction_id)
        if not pending or pending.get("session_id") != session_id:
            raise HTTPException(status_code=404, detail="pending Claude interaction not found")
        future = pending.get("future")
        if (
            pending.get("responded")
            or not isinstance(future, asyncio.Future)
            or future.done()
        ):
            raise HTTPException(status_code=409, detail="Claude interaction is already resolved")
        validated = validate_claude_interaction_response(pending, response)
        pending["responded"] = True
        pending["resolution"] = "answered"
        future.set_result(validated)
        interaction = public_claude_interaction(pending)
    return interaction


async def cancel_claude_interactions(
    session_id: str | None = None,
    *,
    resolution: str = "dismissed",
    expected_run_id: str | None = None,
    ownership_token: str | None = None,
) -> None:
    affected: set[str] = set()
    async with CLAUDE_PENDING_INTERACTIONS_LOCK:
        for pending in CLAUDE_PENDING_INTERACTIONS.values():
            if session_id is not None and pending.get("session_id") != session_id:
                continue
            if (
                expected_run_id is not None
                and str(pending.get("turn_id") or "") != expected_run_id
            ):
                continue
            if (
                ownership_token is not None
                and str(pending.get("owner_token") or "") != ownership_token
            ):
                continue
            future = pending.get("future")
            if (
                pending.get("responded")
                or not isinstance(future, asyncio.Future)
                or future.done()
            ):
                continue
            pending["responded"] = True
            pending["resolution"] = resolution
            future.set_result({"decision": "cancel"})
            affected.add(str(pending.get("session_id") or ""))
    for affected_session_id in affected:
        if affected_session_id:
            with suppress(Exception):
                await update_claude_pending_session_metadata(affected_session_id)


def claude_permission_context_value(context: Any, name: str) -> Any:
    if isinstance(context, dict):
        return context.get(name)
    return getattr(context, name, None)


def claude_permission_update_public_value(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        raw = dict(value)
    else:
        converter = getattr(value, "to_dict", None)
        if callable(converter):
            with suppress(Exception):
                raw = converter()
                if isinstance(raw, dict):
                    bounded = bounded_codex_interaction_value(raw)
                    return bounded if isinstance(bounded, dict) else None
        raw = {
            key: getattr(value, key, None)
            for key in (
                "type", "behavior", "mode", "directories", "destination"
            )
            if getattr(value, key, None) is not None
        }
    bounded = bounded_codex_interaction_value(raw)
    return bounded if isinstance(bounded, dict) else None


def claude_interaction_method(tool_name: str) -> str:
    if tool_name == "AskUserQuestion":
        return "item/tool/requestUserInput"
    if tool_name == "Bash":
        return "item/commandExecution/requestApproval"
    if tool_name in {"Write", "Edit", "MultiEdit", "NotebookEdit", "ApplyPatch"}:
        return "item/fileChange/requestApproval"
    return "item/tool/requestApproval"


def claude_question_params(input_data: dict[str, Any]) -> dict[str, Any]:
    questions: list[dict[str, Any]] = []
    for index, raw_question in enumerate(input_data.get("questions") or []):
        if not isinstance(raw_question, dict):
            continue
        question = bounded_codex_interaction_value(raw_question)
        if not isinstance(question, dict):
            continue
        question["id"] = f"question_{index + 1}"
        question["isOther"] = True
        questions.append(question)
    return {"questions": questions}


async def handle_claude_tool_permission(
    session_id: str,
    tool_name: str,
    input_data: dict[str, Any],
    context: Any,
    *,
    owner_token: str = "",
) -> Any:
    """Bridge one Agent SDK permission callback to its opted-in desktop chat."""
    from claude_agent_sdk.types import PermissionResultAllow, PermissionResultDeny

    scheduler_reason = claude_nondurable_scheduler_reason(tool_name)
    if scheduler_reason is not None:
        return PermissionResultDeny(
            message=scheduler_reason,
            interrupt=False,
        )

    completion_event: asyncio.Event | None = None
    completion_task: asyncio.Task[Any] | None = None
    pending: dict[str, Any] | None = None
    try:
        async with session_lifecycle_lock(session_id):
            async with RUNS.active_lock:
                active = RUNS.active.get(session_id)
                permission_run_id = str(
                    (active or {}).get("claude_permission_run_id") or ""
                )
                manager = PROVIDERS.claude_sdk_manager
                ownership_valid = bool(
                    active
                    and owner_token
                    and str(active.get("claude_sdk_owner_token") or "")
                    == owner_token
                    and permission_run_id
                    and manager is not None
                    and manager.owns_active_run(
                        session_id,
                        owner_token,
                        permission_run_id,
                    )
                )
                interactive = bool(
                    active
                    and active.get("backend") == BACKEND_CLAUDE
                    and active.get("transport") == CLAUDE_TRANSPORT_AGENT_SDK
                    and active.get("interactive_agent_sdk")
                    and not active.get("stop_requested")
                    and active.get("claude_permissions_open")
                    and ownership_valid
                )
                run_id = permission_run_id
            if (
                not interactive
                or session_id in DELETING_SESSIONS
                or session_id in DELETED_SESSION_TOMBSTONES
            ):
                return PermissionResultDeny(
                    message="Hyadex could not safely route this permission request.",
                )

            # The SDK normally shadows can_use_tool in bypassPermissions mode,
            # but the callback itself is not proof that approval is required.
            # Honor the captured mode at the ownership fence so an
            # unexpected callback cannot manufacture a desktop approval card.
            permission_mode = active_claude_permission_mode(session_id, active)
            if (
                tool_name == "AskUserQuestion"
                and permission_mode in {"bypassPermissions", "dontAsk"}
            ):
                return PermissionResultDeny(
                    message="This question was skipped because this turn is configured not to prompt the user.",
                    interrupt=False,
                )
            if (
                tool_name != "AskUserQuestion"
                and permission_mode == "bypassPermissions"
            ):
                return PermissionResultAllow(updated_input=dict(input_data))

            method = claude_interaction_method(tool_name)
            tool_use_id = str(
                claude_permission_context_value(context, "tool_use_id") or ""
            ) or None
            suggestions = list(
                claude_permission_context_value(context, "suggestions") or []
            )
            session_suggestions = [
                suggestion
                for suggestion in suggestions
                if (
                    getattr(suggestion, "destination", None)
                    if not isinstance(suggestion, dict)
                    else suggestion.get("destination")
                ) == "session"
            ]
            if method == "item/tool/requestUserInput":
                params = claude_question_params(input_data)
            else:
                bounded_input = bounded_codex_interaction_value(input_data)
                approval_item = {
                    "id": tool_use_id,
                    "type": "toolCall",
                    "name": tool_name,
                    "input": bounded_input if isinstance(bounded_input, dict) else {},
                }
                params = {
                    "toolName": tool_name,
                    "toolInput": approval_item["input"],
                    "approvalItem": approval_item,
                    "availableDecisions": [
                        "accept",
                        *( ["acceptForSession"] if session_suggestions else [] ),
                        "decline",
                        "cancel",
                    ],
                }
                if tool_name == "Bash" and isinstance(bounded_input, dict):
                    params["command"] = str(bounded_input.get("command") or "")
                if method == "item/fileChange/requestApproval" and isinstance(bounded_input, dict):
                    params["changes"] = [bounded_input]
                for source_name, target_name in (
                    ("decision_reason", "reason"),
                    ("blocked_path", "blockedPath"),
                    ("title", "title"),
                    ("display_name", "displayName"),
                    ("description", "description"),
                    ("agent_id", "agentId"),
                ):
                    value = claude_permission_context_value(context, source_name)
                    if value not in (None, ""):
                        params[target_name] = str(value)
                public_suggestions = [
                    value
                    for suggestion in session_suggestions
                    if (value := claude_permission_update_public_value(suggestion))
                ]
                if public_suggestions:
                    params["permissionSuggestions"] = public_suggestions

            loop = asyncio.get_running_loop()
            future: asyncio.Future[dict[str, Any]] = loop.create_future()
            interaction_id = f"claudereq_{uuid.uuid4().hex[:16]}"
            candidate = {
                "id": interaction_id,
                "session_id": session_id,
                "thread_id": str(
                    session_provider_id(STORE.sessions.get(session_id) or {})
                    or session_id
                ),
                "turn_id": run_id or None,
                "owner_token": owner_token,
                "item_id": tool_use_id,
                "method": method,
                "params": params,
                "created_at": now_iso(),
                "future": future,
                "responded": False,
                "resolution": "dismissed",
                "original_input": dict(input_data),
                "session_permission_suggestions": session_suggestions,
            }
            async with CLAUDE_PENDING_INTERACTIONS_LOCK:
                if len(CLAUDE_PENDING_INTERACTIONS) < MAX_CODEX_PENDING_INTERACTIONS:
                    CLAUDE_PENDING_INTERACTIONS[interaction_id] = candidate
                    pending = candidate
            if pending is not None:
                # The SDK may invoke callbacks on a long-lived receiver task.
                # Deletion must wait for this one interaction to settle, not
                # for that persistent provider task to terminate.
                completion_event = asyncio.Event()
                completion_task = asyncio.create_task(
                    completion_event.wait(),
                    name=f"claude-interaction:{session_id}:{interaction_id}",
                )
                register_session_task(
                    CLAUDE_INTERACTION_HANDLER_TASKS,
                    session_id,
                    completion_task,
                )

        if pending is None:
            return PermissionResultDeny(
                message="Hyadex has too many pending permission requests.",
            )

        interaction_id = str(pending["id"])
        await update_claude_pending_session_metadata(session_id)
        await append_event(
            session_id,
            "claude_interaction_requested",
            {"interaction": public_claude_interaction(pending)},
        )
        response = await asyncio.shield(pending["future"])
        # The UI response can race Stop, Delete, a native steering handoff, or
        # supervisor replacement after the card was rendered. Revalidate the
        # exact owner/run immediately before returning an allow decision so a
        # late click cannot authorize work on a fenced provider generation.
        async with RUNS.active_lock:
            active = RUNS.active.get(session_id)
            manager = PROVIDERS.claude_sdk_manager
            still_owned = bool(
                active
                and not active.get("stop_requested")
                and active.get("claude_permissions_open")
                and str(active.get("claude_sdk_owner_token") or "")
                == owner_token
                and str(active.get("claude_permission_run_id") or "")
                == run_id
                and manager is not None
                and manager.owns_active_run(
                    session_id,
                    owner_token,
                    run_id,
                )
            )
        if not still_owned:
            return PermissionResultDeny(
                message="This Claude turn is no longer active.",
                interrupt=True,
            )
        if pending["method"] == "item/tool/requestUserInput":
            if str(response.get("decision") or "") == "cancel":
                return PermissionResultDeny(
                    message="The user cancelled this question.",
                    interrupt=True,
                )
            if not any(
                isinstance(answer, dict) and bool(answer.get("answers"))
                for answer in response.get("answers", {}).values()
            ):
                return PermissionResultDeny(
                    message="The user skipped this question.",
                    interrupt=False,
                )
            answers_by_text: dict[str, Any] = {}
            by_id = {
                str(question.get("id") or ""): question
                for question in pending["params"].get("questions") or []
                if isinstance(question, dict)
            }
            for question_id, answer in response.get("answers", {}).items():
                question = by_id.get(str(question_id))
                values = answer.get("answers") if isinstance(answer, dict) else []
                if not question or not isinstance(values, list) or not values:
                    continue
                answers_by_text[str(question.get("question") or question_id)] = (
                    values if bool(question.get("multiSelect")) else values[0]
                )
            updated_input = {
                "questions": pending["original_input"].get("questions", []),
                "answers": answers_by_text,
            }
            return PermissionResultAllow(updated_input=updated_input)

        decision = str(response.get("decision") or "decline")
        if decision in {"accept", "acceptForSession"}:
            return PermissionResultAllow(
                updated_input=pending["original_input"],
                updated_permissions=(
                    pending["session_permission_suggestions"]
                    if decision == "acceptForSession"
                    else None
                ),
            )
        return PermissionResultDeny(
            message=(
                "The user cancelled this request."
                if decision == "cancel"
                else "The user denied this request."
            ),
            interrupt=decision == "cancel",
        )
    except asyncio.CancelledError:
        raise
    finally:
        if pending is not None:
            interaction_id = str(pending["id"])
            async with CLAUDE_PENDING_INTERACTIONS_LOCK:
                if CLAUDE_PENDING_INTERACTIONS.get(interaction_id) is pending:
                    CLAUDE_PENDING_INTERACTIONS.pop(interaction_id, None)
            with suppress(Exception):
                await asyncio.shield(
                    update_claude_pending_session_metadata(session_id)
                )
            with suppress(Exception):
                await asyncio.shield(
                    append_event(
                        session_id,
                        "claude_interaction_resolved",
                        {
                            "interaction_id": interaction_id,
                            "request_method": pending["method"],
                            "resolution": pending.get("resolution") or "dismissed",
                        },
                    )
                )
        if completion_event is not None:
            completion_event.set()
        if completion_task is not None:
            with suppress(asyncio.CancelledError):
                await asyncio.shield(completion_task)


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


CLAUDE_ACCOUNT_USAGE_TTL_SECONDS = 120.0
CLAUDE_ACCOUNT_USAGE_FAILURE_TTL_SECONDS = 30.0
CLAUDE_ACCOUNT_USAGE_TIMEOUT_SECONDS = 30.0
CLAUDE_ACCOUNT_USAGE: dict[str, Any] = {"at": 0.0, "value": None, "stale": False}
CLAUDE_ACCOUNT_USAGE_LOCK = asyncio.Lock()


async def probe_claude_account_usage() -> dict[str, Any]:
    """Ask Claude Code for its own ``/usage`` data through the Agent SDK.

    A short-lived CLI connection answers the native ``get_usage`` control
    request from the user's existing login. It sends no prompt, makes no
    model request, loads no project or user settings (so no hooks run), and
    skips the local transcript scan. Only plan windows are kept.
    """
    env = runner_env()
    for inherited_name in tuple(os.environ):
        if inherited_name in PROVIDER_SECRET_ENV_NAMES or is_scrubbed_provider_runtime_env_name(inherited_name):
            env[inherited_name] = ""
    cli_path = claude_sdk_cli_path(env)
    options = create_claude_agent_options(
        cwd=str(Path.home()), cli_path=cli_path, env=env, setting_sources=[],
        max_buffer_size=PROCESS_STREAM_LIMIT, stderr=lambda _line: None,
    )
    client = default_claude_sdk_client_factory(options)
    await client.connect()
    try:
        query = getattr(client, "_query", None)
        if query is None:
            raise ClaudeSDKUnavailable("Claude SDK client did not connect")
        response = await query._send_control_request(
            {"subtype": "get_usage", "skip_behaviors": True},
            timeout=CLAUDE_ACCOUNT_USAGE_TIMEOUT_SECONDS,
        )
    finally:
        with suppress(Exception):
            await client.disconnect()
    return provider_usage.claude_account_snapshot(response, provider_usage.timestamp())


async def read_claude_account_usage(*, refresh: bool = False) -> dict[str, Any]:
    async with CLAUDE_ACCOUNT_USAGE_LOCK:
        cached = CLAUDE_ACCOUNT_USAGE.get("value")
        age = time.monotonic() - float(CLAUDE_ACCOUNT_USAGE.get("at") or 0.0)
        ttl = (CLAUDE_ACCOUNT_USAGE_TTL_SECONDS
               if isinstance(cached, dict) and cached.get("status") == "available"
               else CLAUDE_ACCOUNT_USAGE_FAILURE_TTL_SECONDS)
        if cached is not None and not refresh and not CLAUDE_ACCOUNT_USAGE.get("stale") and age < ttl:
            return copy.deepcopy(cached)
        try:
            result = await asyncio.wait_for(
                probe_claude_account_usage(), CLAUDE_ACCOUNT_USAGE_TIMEOUT_SECONDS + 10,
            )
        except Exception:
            # Never surface provider diagnostics; an old value is not current.
            result = provider_usage.unavailable(BACKEND_CLAUDE, "temporarily_unavailable")
        CLAUDE_ACCOUNT_USAGE.update(at=time.monotonic(), value=result, stale=False)
        return copy.deepcopy(result)


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


def runtime_option(value: str, label: str | None = None, **extra: Any) -> dict[str, Any]:
    clean = str(value or "").strip()
    return {"value": clean, "label": str(label or clean or "Server default").strip(), **extra}


def server_default_runtime_option(label: str | None = None) -> dict[str, Any]:
    return runtime_option("", label or "Server default")


def title_model_label(value: str) -> str:
    clean = str(value or "").strip()
    if not clean:
        return "Server default"
    known = {
        "fable": "Fable",
        "claude-fable-5-1": "Fable 5.1",
        "claude-fable-5": "Fable 5",
        "claude-mythos-5-1": "Mythos 5.1",
        "claude-mythos-5": "Mythos 5",
        "claude-opus-5": "Opus 5",
        "opus[1m]": "Opus 1M",
        "claude-opus-4-8": "Opus 4.8",
        "claude-opus-4-8[1m]": "Opus 4.8 1M",
        "claude-sonnet-5": "Sonnet 5",
        "claude-haiku-4-5-20251001": "Haiku 4.5",
    }
    if clean.lower() in known:
        return known[clean.lower()]
    special = {
        "gpt": "GPT",
        "codex": "Codex",
        "claude": "Claude",
    }
    return " ".join(special.get(part.lower(), part.upper() if part.lower().startswith("gpt") else part.capitalize()) for part in re.split(r"[-_\s]+", clean) if part)


def title_effort_label(value: str) -> str:
    clean = str(value or "").strip()
    if not clean:
        return "Server default"
    if clean.lower() == "xhigh":
        return "XHigh"
    return clean.capitalize()


def unique_runtime_options(options: list[dict[str, Any]], default_label: str | None = None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for option in [server_default_runtime_option(default_label), *options]:
        value = str(option.get("value") or "").strip()
        if value in seen:
            continue
        seen.add(value)
        extra = {key: val for key, val in option.items() if key not in {"value", "label"}}
        out.append(runtime_option(value, option.get("label") or None, **extra))
    return out


def run_catalog_command(cmd: list[str]) -> str:
    result = runtime_command(cmd)
    if result.returncode != 0:
        stderr = result.stderr.strip()[:500]
        raise RuntimeError(f"{cmd[0]} exited {result.returncode}: {stderr}")
    return result.stdout


def claude_supports_effort(effort: str) -> bool:
    clean = str(effort or "").strip().lower()
    if not clean:
        return False
    try:
        result = runtime_command([CLAUDE_BIN, "--effort", clean, "--version"])
    except Exception as exc:
        logger.debug("claude effort probe failed effort=%s: %s", clean, exc)
        return False
    output = f"{result.stdout}\n{result.stderr}".lower()
    return result.returncode == 0 and "unknown --effort value" not in output


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


class RuntimeCatalogBudgetExpired(RuntimeError):
    """A refresh ran out of time; this is not evidence of a broken runtime."""


def runtime_catalog_budget_expired() -> bool:
    deadline = RUNTIME_CATALOG_DEADLINE.get()
    return deadline is not None and time.monotonic() >= deadline


def runtime_probe_timeout(timeout_seconds: float) -> float:
    deadline = RUNTIME_CATALOG_DEADLINE.get()
    if deadline is None:
        return timeout_seconds
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise RuntimeCatalogBudgetExpired("Runtime catalog refresh budget expired")
    return min(timeout_seconds, remaining)


def runtime_command(
    cmd: list[str], *, timeout_seconds: float | None = None,
) -> subprocess.CompletedProcess[str]:
    timeout = runtime_probe_timeout(
        RUNTIME_CATALOG_TIMEOUT_SECONDS if timeout_seconds is None else timeout_seconds
    )
    try:
        return subprocess.run(
            cmd,
            cwd=DEFAULT_CWD if Path(DEFAULT_CWD).exists() else str(Path.home()),
            env=runner_env(),
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        if runtime_catalog_budget_expired():
            raise RuntimeCatalogBudgetExpired("Runtime catalog refresh budget expired") from None
        raise


def parse_claude_help_model_option_groups(
    help_text: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Extract alias and full-name groups from Claude Code's public help.

    Claude Code currently documents evergreen aliases and one full-name
    example in the wrapped ``--model`` option. The old parser stopped at the
    first parenthesized clause, so it silently dropped the full model ID.
    Restrict parsing to that option block so quoted examples elsewhere in the
    help cannot become model choices.
    """

    match = re.search(
        r"(?ms)^[ \t]*(?:-[A-Za-z],\s*)?--model\s+<model>(?P<body>.*?)"
        r"(?=^[ \t]*(?:-[A-Za-z],\s*)?--[A-Za-z][A-Za-z0-9-]*(?:[ \t,<]|$)|\Z)",
        str(help_text or ""),
    )
    if match is None:
        return [], []

    body = match.group("body")
    full_name_marker = re.search(
        r"\bor\s+(?:a\s+)?model(?:'s)?\s+full\s+name\b",
        body,
        re.IGNORECASE,
    )
    token_re = re.compile(
        r"(?<![A-Za-z0-9])['\"`]([A-Za-z0-9][A-Za-z0-9._:/\-\[\]]{0,255})['\"`]"
    )

    def options_from(region: str) -> list[dict[str, Any]]:
        return [
            runtime_option(value, title_model_label(value))
            for value in token_re.findall(region)
            if CLAUDE_MODEL_VALUE_RE.fullmatch(value)
        ]

    if full_name_marker is not None:
        return (
            options_from(body[:full_name_marker.start()]),
            options_from(body[full_name_marker.end():]),
        )

    # Older help has no explicit separator. Short identifier-like values are
    # aliases; provider/platform IDs contain punctuation and remain full names.
    alias_options: list[dict[str, Any]] = []
    full_name_options: list[dict[str, Any]] = []
    for option in options_from(body):
        value = str(option.get("value") or "")
        target = (
            alias_options
            if re.fullmatch(r"[a-z][a-z0-9_]*(?:\[[a-z0-9]+\])?", value)
            else full_name_options
        )
        target.append(option)
    return alias_options, full_name_options


def parse_claude_help_model_options(help_text: str) -> list[dict[str, Any]]:
    aliases, full_names = parse_claude_help_model_option_groups(help_text)
    return [*aliases, *full_names]


def parse_claude_models_api_payload(payload: Any) -> list[dict[str, Any]]:
    """Normalize the account-scoped response from Anthropic's Models API."""

    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        return []
    options: list[dict[str, Any]] = []
    for raw_model in payload["data"]:
        if not isinstance(raw_model, dict):
            continue
        model_id = str(raw_model.get("id") or "").strip()
        if not CLAUDE_MODEL_VALUE_RE.fullmatch(model_id):
            continue
        display_name = str(raw_model.get("display_name") or "").strip()
        options.append(
            runtime_option(
                model_id,
                display_name[:160] or title_model_label(model_id),
            )
        )
    return options


def claude_models_api_base_url_allowed(parsed_base: Any) -> bool:
    """Allow credentials over HTTPS, or plain HTTP only to loopback."""

    if parsed_base.scheme == "https" and parsed_base.hostname:
        return True
    if parsed_base.scheme != "http" or not parsed_base.hostname:
        return False
    host = parsed_base.hostname.rstrip(".").lower()
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def discover_claude_provider_models(
    *, timeout_seconds: float = 6.0,
) -> tuple[list[dict[str, Any]], Literal["unavailable", "success", "failed"]]:
    """Return models available to the configured Anthropic API account.

    Claude subscription/OAuth installs fall through without network access
    here; their native picker comes from SDK initialization. API-key installs
    can use the official account-scoped Models API; failures remain non-fatal
    and never log the credential or a response body.
    """

    api_key = str(os.environ.get("ANTHROPIC_API_KEY") or "").strip()
    if not api_key:
        return [], "unavailable"
    if "\r" in api_key or "\n" in api_key:
        logger.warning("claude provider model discovery skipped: invalid credential format")
        return [], "failed"
    base_url = str(
        os.environ.get("ANTHROPIC_BASE_URL") or "https://api.anthropic.com"
    ).strip().rstrip("/")
    parsed_base = urlparse(base_url)
    if not claude_models_api_base_url_allowed(parsed_base):
        logger.warning("claude provider model discovery skipped: insecure or invalid base URL")
        return [], "failed"
    endpoint = (
        f"{base_url}/models?limit=1000"
        if parsed_base.path.rstrip("/").endswith("/v1")
        else f"{base_url}/v1/models?limit=1000"
    )
    curl_bin = shutil.which("curl", path=runner_env().get("PATH"))
    if not curl_bin:
        logger.warning("claude provider model discovery skipped: curl unavailable")
        return [], "unavailable"
    try:
        timeout_seconds = runtime_probe_timeout(
            max(0.5, min(RUNTIME_CATALOG_TIMEOUT_SECONDS, timeout_seconds))
        )
        # Keep the key out of argv, the child environment, and disk by feeding
        # curl's header-file syntax through stdin. curl's total-time limit is
        # resistant to a peer that trickles bytes forever; subprocess.run's
        # independent deadline kills and reaps curl if its own timer fails.
        header_bytes = (
            "anthropic-version: 2023-06-01\n"
            f"x-api-key: {api_key}\n"
            "accept: application/json\n"
        ).encode("utf-8")
        child_env = runner_env()
        for secret_name in CLAUDE_PROVIDER_SECRET_ENV_NAMES:
            child_env.pop(secret_name, None)
        curl_args = [curl_bin, "--disable"]
        if parsed_base.scheme == "http":
            # Plain HTTP is allowed only for loopback. Never let inherited
            # proxy settings send that credential-bearing request elsewhere.
            curl_args.extend(["--noproxy", "*"])
        curl_args.extend(
            [
                "--fail",
                "--silent",
                "--show-error",
                "--connect-timeout",
                f"{timeout_seconds:g}",
                "--max-time",
                f"{timeout_seconds:g}",
                "--max-filesize",
                str(CLAUDE_MODELS_API_MAX_BYTES),
                "--header",
                "@-",
                "--user-agent",
                f"HyadexServer/{SERVER_VERSION}",
                endpoint,
            ]
        )
        result = subprocess.run(
            curl_args,
            cwd=DEFAULT_CWD if Path(DEFAULT_CWD).exists() else str(Path.home()),
            env=child_env,
            input=header_bytes,
            text=False,
            capture_output=True,
            timeout=runtime_probe_timeout(timeout_seconds + 1.0),
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(f"curl exited {result.returncode}")
        raw = result.stdout
        if len(raw) > CLAUDE_MODELS_API_MAX_BYTES:
            raise ValueError("Claude Models API response exceeds size limit")
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            raise ValueError("Claude Models API returned an invalid payload")
        return parse_claude_models_api_payload(payload), "success"
    except Exception as exc:
        logger.warning(
            "claude provider model discovery failed error_type=%s",
            type(exc).__name__,
        )
        return [], "failed"


def claude_fallback_model_options() -> list[dict[str, Any]]:
    return [
        runtime_option(
            value,
            label,
            **({"availability": "limited"} if limited else {}),
        )
        for value, label, limited in (
            *CLAUDE_CURRENT_MODEL_FALLBACKS,
            *CLAUDE_LEGACY_MODEL_FALLBACKS,
        )
    ]


def discover_claude_native_models() -> tuple[list[dict[str, Any]], str]:
    from claude_model_catalog import cached_native_models

    try:
        models = cached_native_models(CLAUDE_BIN, env=runner_env())
        return (models, "success") if models is not None else ([], "unavailable")
    except Exception as exc:
        # Optional metadata must neither poison readiness nor leak the init
        # response (which contains account data), stderr, or subprocess args.
        logger.debug("claude native model discovery unavailable error_type=%s", type(exc).__name__)
        return [], "unavailable"


def parse_claude_help_catalog() -> dict[str, Any]:
    model_options: list[dict[str, Any]] = []
    effort_options: list[dict[str, Any]] = []
    default_model = (
        os.environ.get("CLAUDE_MODEL")
        or os.environ.get("ANTHROPIC_MODEL")
        or hyadex_setting("CLAUDE_MODEL", "")
        or "sonnet"
    )
    default_effort = os.environ.get("CLAUDE_EFFORT") or hyadex_setting("CLAUDE_EFFORT", "")
    supports_ultracode = claude_supports_effort("ultracode")
    # Optional candidate discovery must leave time for the native picker,
    # especially if a configured API endpoint is slow or unreachable.
    provider_options, provider_status = discover_claude_provider_models(timeout_seconds=2.0)
    native_options, native_status = discover_claude_native_models()
    help_text = ""
    help_available = False
    try:
        help_text = run_catalog_command([CLAUDE_BIN, "--help"])
        help_available = True
    except Exception as exc:
        logger.warning(
            "claude help model discovery failed error_type=%s",
            type(exc).__name__,
        )

    discovered_help_aliases, discovered_help_full_names = (
        parse_claude_help_model_option_groups(help_text)
    )
    if native_status == "success":
        # Native values preserve alias semantics; native labels identify the
        # actual version (e.g. Opus 5.5). Legacy candidates are included only
        # after the native picker applies account/organization restrictions.
        # Never append unfiltered static pins to a successful native result.
        model_options.extend(native_options)
    else:
        model_options.extend(discovered_help_aliases)
        model_options.extend(runtime_option(value, label) for value, label in CLAUDE_CLI_MODEL_ALIASES)
        if provider_status == "success":
            # The API is account-scoped; don't add static pins absent from it.
            model_options.extend(provider_options)
        else:
            model_options.extend(claude_fallback_model_options())
            model_options.extend(discovered_help_full_names)
    if native_status != "success" and provider_status != "success" and default_model and not any(
        str(option.get("value") or "") == default_model
        for option in model_options
    ):
        model_options.insert(0, runtime_option(default_model, title_model_label(default_model)))

    effort_match = re.search(r"--effort\s+<level>.*?\(([^)]+)\)", help_text, re.IGNORECASE | re.DOTALL)
    if effort_match:
        for effort in re.split(r"[,/\s]+", effort_match.group(1)):
            clean = effort.strip()
            if clean and re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", clean):
                effort_options.append(runtime_option(clean, title_effort_label(clean)))
    if not effort_options:
        effort_options.extend(
            runtime_option(effort, title_effort_label(effort))
            for effort in ("low", "medium", "high", "xhigh", "max")
        )
    if supports_ultracode:
        effort_options.append(runtime_option("ultracode", "Ultracode"))

    model_sources = []
    if native_status == "success":
        model_sources.append("Cached Claude SDK initialize")
    elif provider_status == "success":
        model_sources.append("Anthropic Models API")
    if native_status != "success":
        model_sources.append("claude --help" if help_available else "claude --help failed")
    if native_status != "success" and provider_status != "success":
        if provider_status == "failed":
            model_sources.append("Anthropic Models API failed")
        model_sources.append("current fallback")
    effort_source = "claude --help" if help_available else "claude --help failed"
    default_model_label = next(
        (option["label"] for option in model_options if option["value"] == default_model),
        title_model_label(default_model),
    )
    # Label the "" option as the server default, like Codex, so it is not
    # mistaken for the explicit model or effort of the same name.
    return {
        "models": unique_runtime_options(model_options, f"Server default ({default_model_label})"),
        "efforts": unique_runtime_options(
            effort_options,
            f"Server default ({title_effort_label(default_effort)})" if default_effort else "Server default",
        ),
        "model_source": " + ".join(model_sources),
        "effort_source": effort_source,
        "default_model": default_model,
        "default_effort": default_effort or None,
    }


def build_claude_cmd(
    session_id: str,
    sess: dict[str, Any],
    manifest_path: Path,
    *,
    provider_id: str | None = None,
    no_session_persistence: bool = False,
    disable_provider_subagents: bool = False,
) -> list[str]:
    if sess.get("fork_resume_session_at") and not (provider_id and sess.get("fork_from")):
        raise ValueError("The completed Claude fork snapshot is unavailable; refusing an empty resume.")
    system_prompt = session_system_prompt(
        session_id,
        sess,
        manifest_path,
    )
    cmd = [
        CLAUDE_BIN, "-p",
        "--output-format", "stream-json",
        "--verbose",
        "--dangerously-skip-permissions",
        "--append-system-prompt", system_prompt,
        "--system-prompt-snapshot", "off",
        "--disallowedTools",
        "AskUserQuestion",
        "EnterPlanMode",
        "ExitPlanMode",
        *CLAUDE_NON_DURABLE_SCHEDULER_TOOLS,
        *(["Agent", "Task"] if disable_provider_subagents else []),
    ]
    if sess.get("model"):
        cmd.extend(["--model", str(sess["model"])])
    if sess.get("effort"):
        cmd.extend(["--effort", str(sess["effort"])])
    subagent_limit = sess.get("subagent_limit")
    if type(subagent_limit) is int and subagent_limit > 0:
        # Flag settings override inherited user/project env without writing
        # shared Claude settings or changing another chat's process.
        cmd.extend(["--settings", json.dumps({"env": {
            "CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS": str(subagent_limit),
        }}, separators=(",", ":"))])
    if no_session_persistence and not provider_id:
        cmd.append("--no-session-persistence")
    if provider_id:
        cmd.extend(["--resume", provider_id])
        if sess.get("fork_from"):
            cmd.append("--fork-session")
            cmd.extend(["--name", f"Fork: {sess.get('title') or sess['id']}"])
            if sess.get("fork_resume_session_at"):
                cmd.extend(["--resume-session-at", str(sess["fork_resume_session_at"])])
    return cmd


def claude_sdk_cli_path(env: dict[str, str]) -> str:
    """Resolve the user's existing Claude CLI so SDK mode keeps its auth."""

    configured = str(CLAUDE_BIN or "claude").strip() or "claude"
    expanded = str(Path(configured).expanduser())
    if os.path.sep in expanded:
        if Path(expanded).is_file():
            return expanded
        raise ClaudeSDKUnavailable(
            f"Claude CLI was not found at the configured path: {expanded}"
        )
    resolved = shutil.which(configured, path=env.get("PATH"))
    if not resolved:
        raise ClaudeSDKUnavailable(
            "Claude CLI is not available in the HyadexServer runtime PATH"
        )
    return resolved


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


def claude_sdk_result_details(message: Any) -> dict[str, Any]:
    errors = claude_sdk_field(message, "errors")
    if isinstance(errors, list):
        error_text = "\n".join(str(value) for value in errors if str(value))
    else:
        error_text = ""
    result = clean_assistant_text(
        str(claude_sdk_field(message, "result") or "")
    )
    subtype = str(claude_sdk_field(message, "subtype") or "")
    terminal_reason = str(
        claude_sdk_field(message, "terminal_reason") or ""
    )
    is_error = bool(claude_sdk_field(message, "is_error", False))
    return {
        "session_id": str(claude_sdk_field(message, "session_id") or ""),
        "result_text": result,
        "is_error": is_error,
        "error": error_text or (result if is_error else "") or (
            f"Claude SDK ended with {subtype or 'an error'}" if is_error else ""
        ),
        "subtype": subtype,
        "terminal_reason": terminal_reason,
        "aborted": terminal_reason in {"aborted_streaming", "aborted_tools"},
    }


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


async def project_claude_sdk_message(
    session_id: str,
    run_id: str,
    message: Any,
    *,
    text_parts: list[str],
    current_tools: dict[str, dict[str, Any]],
    changed_paths: set[str],
    tool_activity_run_ids: set[str] | None = None,
    error_run_ids: set[str] | None = None,
) -> dict[str, Any] | None:
    """Project one typed SDK message into the existing durable timeline."""

    message_type = claude_sdk_type(message)
    provider_id = str(claude_sdk_field(message, "session_id") or "")
    if message_type == "SystemMessage":
        data = claude_sdk_field(message, "data", {})
        if isinstance(data, dict):
            provider_id = str(data.get("session_id") or provider_id)
    if provider_id:
        await mark_provider_turn_ready(session_id, run_id, provider_id)

    async def project_tool_result(block: Any) -> None:
        if tool_activity_run_ids is not None:
            tool_activity_run_ids.add(run_id)
        tool_id = str(claude_sdk_field(block, "tool_use_id") or "")
        await append_event(session_id, "tool_finished", {
            "run_id": run_id,
            "tool_id": tool_id or None,
            "tool": current_tools.pop(tool_id, None),
            "output": event_output_text(
                claude_sdk_field(block, "content", "")
            ),
            "is_error": claude_sdk_field(block, "is_error") is True,
        })

    async def project_subagent_lifecycle() -> None:
        raw = claude_sdk_subagent_lifecycle_raw(message)
        if raw is None:
            return
        await append_event(session_id, "raw_event", {
            "run_id": run_id,
            "backend": BACKEND_CLAUDE,
            "raw": json.dumps(raw, separators=(",", ":")),
        })

    if message_type == "AssistantMessage":
        assistant_error = str(claude_sdk_field(message, "error") or "")
        if assistant_error:
            if error_run_ids is not None:
                error_run_ids.add(run_id)
            await append_event(session_id, "error", {
                "run_id": run_id,
                "backend": BACKEND_CLAUDE,
                "transport": CLAUDE_TRANSPORT_AGENT_SDK,
                "message": f"Claude assistant error: {assistant_error}",
                **run_event_metadata(run_id),
            })
        content = claude_sdk_field(message, "content", [])
        for block in content if isinstance(content, list) else []:
            block_type = claude_sdk_type(block)
            if block_type in {"TextBlock", "text"}:
                text = clean_assistant_text(
                    str(claude_sdk_field(block, "text") or "")
                )
                if text:
                    text_parts.append(text)
                    await append_event(session_id, "assistant_text", {
                        "run_id": run_id,
                        "text": text,
                        "backend": BACKEND_CLAUDE,
                        **({"provider_message_id": message_uuid}
                           if (message_uuid := claude_sdk_field(message, "uuid")) else {}),
                        **run_event_metadata(run_id),
                    })
            elif block_type in {"ThinkingBlock", "thinking"}:
                thinking = str(claude_sdk_field(block, "thinking") or "")
                if thinking:
                    await append_event(session_id, "reasoning_summary", {
                        "run_id": run_id,
                        "text": thinking,
                    })
            elif block_type in {
                "ToolUseBlock",
                "ServerToolUseBlock",
                "tool_use",
                "server_tool_use",
            }:
                if tool_activity_run_ids is not None:
                    tool_activity_run_ids.add(run_id)
                tool_id = str(
                    claude_sdk_field(block, "id")
                    or f"tool_{uuid.uuid4().hex[:8]}"
                )
                tool_input = claude_sdk_field(block, "input", {})
                tool = {
                    "id": tool_id,
                    "name": str(claude_sdk_field(block, "name") or "tool"),
                    "input": tool_input if isinstance(tool_input, dict) else {},
                }
                current_tools[tool_id] = tool
                changed_paths.update(tool_changed_paths(tool))
                await append_event(session_id, "tool_started", {
                    "run_id": run_id,
                    "tool": tool,
                })
            elif block_type in {
                "ToolResultBlock",
                "ServerToolResultBlock",
                "tool_result",
                "server_tool_result",
                "advisor_tool_result",
            }:
                await project_tool_result(block)
        await project_subagent_lifecycle()
        return None

    if message_type == "UserMessage":
        content = claude_sdk_field(message, "content", [])
        for block in content if isinstance(content, list) else []:
            if claude_sdk_type(block) not in {
                "ToolResultBlock",
                "ServerToolResultBlock",
                "tool_result",
                "server_tool_result",
                "advisor_tool_result",
            }:
                continue
            await project_tool_result(block)
        await project_subagent_lifecycle()
        return None

    if message_type in {"ResultMessage", "result"}:
        return claude_sdk_result_details(message)
    await project_subagent_lifecycle()
    return None


async def persist_claude_background_task_receipts(
    session_id: str,
    run_id: str,
    provider_id: str,
    handle: Any,
    *,
    tracking_lost: bool = False,
) -> dict[str, Any] | None:
    """Seal exact provider task facts before handing off their logical owner."""
    tasks = normalize_task_receipts(
        getattr(handle, "background_task_receipts", ()),
        run_id=run_id,
        provider_session_id=provider_id,
        tracking_lost=tracking_lost,
    )
    overflow = getattr(handle, "background_task_overflow_count", 0)
    overflow = min(overflow, 1_000_000) if type(overflow) is int and overflow > 0 else 0
    if not provider_id or (not tasks and not overflow):
        return None
    batch = {
        "reconciliation_id": f"reconcile_{uuid.uuid4().hex}",
        "tasks": tasks,
        "overflow_count": overflow,
    }
    async with STORE.transaction():
        session = STORE.sessions.get(session_id)
        if session is None:
            return None
        previous = session.get(CLAUDE_BACKGROUND_SESSION_FIELD)
        pending = pending_reconciliation_state(previous, batch, provider_session_id=provider_id)
        session[CLAUDE_BACKGROUND_SESSION_FIELD] = pending
        # Observed task facts are not rollbackable actions. Keep this exact
        # checkpoint on failure; cancelled STORE.save has already committed.
        await STORE.save(durable=True)
    stored = await append_event(session_id, CLAUDE_BACKGROUND_RECONCILED_EVENT, {
        "run_id": run_id, "backend": BACKEND_CLAUDE,
        "provider_session_id": provider_id, **batch,
    })
    return None if stored.get("discarded") else pending


async def acknowledge_claude_background_reconciliation(
    session_id: str,
    run_id: str,
    provider_id: str,
    handle: Any,
    batches: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Query acceptance is not delivery: only the bound context hook consumes."""
    if not batches or getattr(handle, "background_task_reconciliation_consumed", False) is not True:
        return batches
    identifiers = [batch["reconciliation_id"] for batch in batches]
    stored = await append_event(session_id, CLAUDE_BACKGROUND_CONSUMED_EVENT, {
        "run_id": run_id, "backend": BACKEND_CLAUDE,
        "provider_session_id": provider_id,
        "reconciliation_ids": identifiers,
    })
    if stored.get("discarded"):
        return batches
    async with STORE.transaction():
        session = STORE.sessions.get(session_id)
        pending = session.get(CLAUDE_BACKGROUND_SESSION_FIELD) if session is not None else None
        if isinstance(pending, dict) and pending.get("provider_session_id") == provider_id and pending.get("reconciliation_id") in identifiers:
            session.pop(CLAUDE_BACKGROUND_SESSION_FIELD, None)
            try:
                await STORE.save(durable=True)
            except Exception:
                # A failed write retains pending facts for a later retry.
                # CancelledError is different: STORE.save already committed
                # its covering snapshot, so the clear must remain in memory.
                session[CLAUDE_BACKGROUND_SESSION_FIELD] = pending
                raise
    return []


PROVIDER_WORK_LABEL_LIMIT = 32
MAX_CLAUDE_BACKGROUND_EVENT_SCAN_BYTES = 64 * 1024 * 1024
CLAUDE_PROVIDER_INSPECTION_OVERFLOW_LABEL = (
    "Claude provider state exceeds safe inspection limit"
)


def loaded_claude_background_session_state(
    manager: Any,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Capture bounded loaded Claude ownership without missing overflow work.

    Event folding is intentionally capped, but the cap must not make a loaded
    supervisor beyond the first page invisible. Once more loaded sessions
    exist than can be inspected, retain a conservative blocker instead of
    treating the uninspected suffix as idle.
    """

    if manager is None:
        return (), ()
    loaded_session_ids: list[str] = []
    unknown_labels: list[str] = []
    for session_id, session in tuple(STORE.sessions.items()):
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
            if len(unknown_labels) < PROVIDER_WORK_LABEL_LIMIT:
                unknown_labels.append(
                    f"Claude provider state unknown in {session_id}"
                )
            continue
        if loaded:
            if len(loaded_session_ids) < PROVIDER_WORK_LABEL_LIMIT:
                loaded_session_ids.append(session_id)
                continue
            # Scanning arbitrary numbers of histories makes restart/update
            # admission unbounded. One extra loaded supervisor is sufficient
            # proof that the capped scan cannot establish an idle state.
            if len(unknown_labels) < PROVIDER_WORK_LABEL_LIMIT:
                unknown_labels.append(
                    CLAUDE_PROVIDER_INSPECTION_OVERFLOW_LABEL
                )
            elif unknown_labels:
                # Existing unknown labels already fail closed, but use a
                # stable overflow marker so revalidation records why the
                # enumeration stopped.
                unknown_labels[-1] = CLAUDE_PROVIDER_INSPECTION_OVERFLOW_LABEL
            break
    return tuple(loaded_session_ids), tuple(unknown_labels)


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


def claude_background_work_labels_from_events(
    session_ids: tuple[str, ...],
) -> list[str]:
    """Inspect bounded durable Claude child state off the asyncio thread."""

    labels: list[str] = []
    for session_id in session_ids:
        try:
            event_bytes = events_path(session_id).stat().st_size
        except FileNotFoundError:
            event_bytes = 0
        except OSError:
            labels.append(f"Claude provider state unknown in {session_id}")
            continue
        if event_bytes > MAX_CLAUDE_BACKGROUND_EVENT_SCAN_BYTES:
            # Never make restart/update admission parse an arbitrarily large
            # transcript. Failing closed is safe and the operator can Stop the
            # loaded chat (which evicts its provider supervisor) before retry.
            labels.append(f"Claude provider state too large in {session_id}")
            continue
        try:
            snapshot = build_claude_subagent_snapshot(session_id, limit=1)
            active_count = max(0, int(snapshot.get("active_count") or 0))
        except Exception:
            # A loaded provider process whose durable child state cannot be
            # inspected is not proof that it is safe to terminate.
            active_count = 1
        if active_count:
            labels.append(f"Claude background work in {session_id}")
    return labels


def claude_sdk_dependency_available() -> bool:
    return importlib.util.find_spec("claude_agent_sdk") is not None


CLAUDE_GOAL_PROJECTIONS: dict[str, ClaudeGoalProjection] = {}
CLAUDE_GOAL_PATHS: dict[str, Path] = {}
CLAUDE_GOAL_LOCKS: dict[str, asyncio.Lock] = {}
CLAUDE_GOAL_PENDING: dict[str, dict[str, Any]] = {}


async def refresh_claude_goal(
    session_id: str, *, provider_id: str | None = None, notify: bool = True,
) -> dict[str, Any] | None:
    """Project provider-owned goal records during existing runtime activity."""
    session = STORE.sessions.get(session_id)
    if not session or str(session.get("backend") or DEFAULT_BACKEND) != BACKEND_CLAUDE:
        return None
    provider_id = provider_id or claude_provider_id_for_session(session)
    if not provider_id:
        return None
    async with CLAUDE_GOAL_LOCKS.setdefault(session_id, asyncio.Lock()):
        projection = CLAUDE_GOAL_PROJECTIONS.get(session_id)
        if projection is None or projection.provider_session_id != provider_id:
            projection = ClaudeGoalProjection(provider_id)
            CLAUDE_GOAL_PROJECTIONS[session_id] = projection
            CLAUDE_GOAL_PATHS.pop(session_id, None)
        path = CLAUDE_GOAL_PATHS.get(session_id)
        if path is not None and not await asyncio.to_thread(path.exists):
            CLAUDE_GOAL_PATHS.pop(session_id, None)
            path = None
        if path is None:
            path = await asyncio.to_thread(find_claude_history, provider_id)
            if path is not None:
                CLAUDE_GOAL_PATHS[session_id] = path
        if path is None:
            return None
        was_caught_up = projection.caught_up
        changed = await asyncio.to_thread(projection.refresh, path)
        if not projection.caught_up:
            return None
        changed = changed or not was_caught_up
        goal = projection.goal
        pending = CLAUDE_GOAL_PENDING.get(session_id)
        if pending and goal and goal.get("set_at") != pending.get("previous_set_at"):
            CLAUDE_GOAL_PENDING.pop(session_id, None)
            changed = True
        if changed and notify:
            await append_event(session_id, "claude_goal_changed", {})
        return dict(goal) if goal else None


def require_claude_goal_session(session_id: str) -> dict[str, Any]:
    session = STORE.sessions.get(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="session not found")
    if str(session.get("backend") or DEFAULT_BACKEND) != BACKEND_CLAUDE:
        raise HTTPException(status_code=400, detail="Goals here require a Claude chat")
    if CLAUDE_TRANSPORT == CLAUDE_TRANSPORT_PRINT or not claude_sdk_dependency_available():
        raise HTTPException(status_code=409, detail="Claude Goals require the Claude Agent SDK")
    return session


class ClaudeGoalRequest(BaseModel):
    condition: str = Field(min_length=1, max_length=4000)


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
