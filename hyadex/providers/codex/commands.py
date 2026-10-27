"""Provider commands and per-turn option building for Codex and Claude."""
from __future__ import annotations

import asyncio
from contextlib import suppress
import hashlib
import json
import logging.handlers
import os
from pathlib import Path
import time
from typing import Any
import uuid
import weakref

from fastapi import HTTPException

import codex_auth
from hyadex.core.config import (
    BACKEND_CLAUDE,
    BACKEND_CODEX,
    CLAUDE_SDK_INTERACTIVE_CLIENT_CAPABILITY,
    CLAUDE_TRANSPORT,
    CLAUDE_TRANSPORT_PRINT,
    CODEX_APP_SERVER_JSONL_LIMIT_BYTES,
    CODEX_APP_SERVER_LIFECYCLE_TIMEOUT_SECONDS,
    CODEX_APP_SERVER_NOTIFICATION_QUEUE_LIMIT,
    CODEX_APP_SERVER_TIMEOUT_SECONDS,
    CODEX_APPROVAL_POLICIES,
    CODEX_APPROVAL_REVIEWERS,
    CODEX_BIN,
    CODEX_DEFAULT_APPROVAL_POLICY,
    CODEX_DEFAULT_APPROVALS_REVIEWER,
    CODEX_DEFAULT_PERMISSION_PROFILE,
    CODEX_DEFAULT_SANDBOX_MODE,
    CODEX_GOALS_ENABLED,
    CODEX_INTERACTIVE_CLIENT_CAPABILITY,
    CODEX_PROVIDER_MCP_NAME,
    CODEX_SANDBOX_MODES,
    CODEX_SANDBOX_POLICY_TYPES,
    CODEX_TRANSPORT,
    CODEX_TRANSPORT_APP_SERVER,
    CODEX_TRANSPORT_EXEC,
    DEFAULT_BACKEND,
    DEFAULT_CWD,
    PROCESS_STREAM_LIMIT,
    SERVER_VERSION,
    STATE_DIR,
)
from hyadex.core.paths import codex_manifest_path, existing_cwd, now_iso
from hyadex.core.runs import RUNS
from hyadex.core.server import SERVER
from hyadex.core.server_logging import concise_error_message
from hyadex.core.sessions import (
    append_event,
    ensure_session_not_deleting,
    native_session_available,
    register_session_task,
    session_lifecycle_lock,
    session_system_prompt,
    STORE,
)
from hyadex.core.session_titles import server_lifecycle_admission_blocker
from hyadex.core.session_settings import codex_runtime_settings, effective_claude_permission_mode
from hyadex.core.session_codex import (
    codex_app_server_env,
    codex_app_server_managers,
    CODEX_RETIRED_APP_SERVER_MANAGERS,
    CODEX_SESSION_APP_SERVER_MANAGERS,
    CODEX_THREAD_SESSION_INDEX,
    existing_codex_app_server_manager,
    session_codex_thread_id,
)
from hyadex.core.util import (
    compact_memory_text,
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
from hyadex.features.usage import HUB, join_task_despite_caller_cancellation
from hyadex.providers.authority import (
    agent_runner_env,
    provider_tool_call_is_read_only,
    ProviderToolError,
    validate_provider_tool_input,
)
from hyadex.providers.claude.history import session_provider_id
from hyadex.providers.claude.runtime import (
    claude_sdk_cli_path,
    claude_sdk_configuration_key,
    claude_sdk_manager,
    ensure_provider_manager_factory_admission,
    handle_claude_tool_permission,
    resolve_claude_resume_provider,
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
    CodexAppServerManager,
    CodexAppServerProtocolError,
    CodexAppServerRequestError,
)
from hyadex.providers.codex.config import (
    codex_effective_thread_config,
    sanitize_codex_thread_config,
)
from hyadex.providers.codex.forks import (
    codex_raw_developer_message,
    CodexForkCleanupError,
    forget_abandoned_fork_provider_thread,
    persist_abandoned_fork_provider_thread,
)
from hyadex.providers.codex.goals import CODEX_GOAL_SYNC_GENERATIONS, reconcile_codex_thread_goal
from hyadex.providers.codex.interactions import (
    cache_codex_approval_item,
    CODEX_INTERACTION_HANDLER_TASKS,
    CODEX_PENDING_INTERACTIONS,
    effective_codex_approval_policy,
    handle_codex_server_request,
)
from hyadex.providers.codex.managers import (
    acquire_codex_interactive_control_lease,
    bind_codex_control_reservation,
    CODEX_APP_SERVER_MANAGER_LOCK,
    CODEX_APP_SERVER_THREAD_PIN_COUNTS,
    codex_binary_identity,
    CODEX_BINARY_PROBE_LOCK,
    codex_control_http_error,
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
    codex_session_id_for_thread,
    codex_thread_instruction_hash,
    codex_thread_instructions,
)
from hyadex.providers.codex.tools import (
    codex_thread_params,
    execute_provider_tool,
    execute_provider_tool_once,
)
from hyadex.providers.instructions import (
    PROVIDER_TOOL_DESCRIPTION,
    PROVIDER_TOOL_INPUT_SCHEMA,
    PROVIDER_TOOL_MAX_CONCURRENT_PER_CLAUDE_CHAT,
)
from hyadex.providers.registry import (
    PROVIDER_COMMAND_EMPTY_FALLBACK_SECRET,
    provider_command_prompt_matches,
    provider_command_selector_secret,
    provider_command_snapshot,
    provider_command_support,
    ProviderCommandSelectionInvalid,
    ProviderCommandSelectionUnavailable,
)
from hyadex.providers.runtime import (
    is_scrubbed_provider_runtime_env_name,
    PROVIDER_SECRET_ENV_NAMES,
    validate_provider_runtime_env,
)
from hyadex.providers.state import PROVIDERS
from hyadex.providers.subagents import (
    CODEX_GOALS_CONFIG_LOCK,
    codex_session_has_live_subagents,
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


async def codex_app_server_manager_for_thread(thread_id: str) -> CodexAppServerManager:
    session_id = codex_session_id_for_thread(thread_id)
    if session_id and session_id in STORE.sessions:
        return await codex_app_server_manager(STORE.sessions[session_id], allow_retired_login=True)
    owner = existing_codex_app_server_manager_for_thread(thread_id)
    if owner is not None:
        retain_codex_manager_caller(owner, "")
        return owner
    return await codex_app_server_manager(None)


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
