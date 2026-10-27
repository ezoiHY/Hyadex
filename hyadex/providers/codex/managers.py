"""Codex app-server managers: creation, binary and login refresh, thread pinning and control slots."""
from __future__ import annotations

import asyncio
from contextlib import suppress
import logging.handlers
from pathlib import Path
import shutil
import subprocess
import time
from typing import Any

from fastapi import HTTPException

import codex_auth
from hyadex.core.config import (
    BACKEND_CODEX,
    CODEX_APP_SERVER_MAX_LOADED_THREADS,
    CODEX_BIN,
    CODEX_SESSION_CLEANUP_TIMEOUT_SECONDS,
    DEFAULT_CWD,
)
from hyadex.core.paths import existing_cwd, now_iso
from hyadex.core.runs import RUNS
from hyadex.core.server_logging import concise_error_message
from hyadex.core.sessions import STORE
from hyadex.core.session_settings import RUNTIME_DIAGNOSTICS, RUNTIME_DIAGNOSTICS_LOCK
from hyadex.core.session_codex import (
    codex_app_server_env,
    codex_app_server_managers,
    CODEX_THREAD_SESSION_INDEX,
    existing_codex_app_server_manager,
)
from hyadex.providers.claude.history import session_provider_id
from hyadex.providers.claude.runtime import runtime_command
from hyadex.providers.codex.app_server import (
    CodexAppServerError,
    CodexAppServerManager,
    CodexAppServerRequestError,
)
from hyadex.providers.codex.config import sanitize_codex_thread_config
from hyadex.providers.codex.forks import codex_raw_developer_message
from hyadex.providers.codex.goals import reconcile_codex_thread_goal
from hyadex.providers.codex.threads import (
    CODEX_APP_SERVER_INVALIDATED_THREADS,
    CODEX_APP_SERVER_PINNED_THREADS,
    CODEX_APP_SERVER_THREAD_LRU,
    CODEX_APP_SERVER_THREAD_LRU_LOCK,
    codex_session_id_for_thread,
    codex_thread_instruction_hash,
    codex_thread_instructions,
    invalidated_codex_thread_error,
    schedule_codex_thread_hygiene_check,
)
from hyadex.providers.codex.tools import (
    codex_thread_params,
    start_standalone_codex_app_server_thread,
)
from hyadex.providers.registry import (
    persist_run_provider_session,
    register_provider_child,
    runtime_diagnostic_payload,
    safe_runtime_version,
    store_runtime_diagnostic,
    unregister_provider_child,
)
from hyadex.providers.state import PROVIDERS
from hyadex.providers.subagents import record_codex_subagent_limit_application

logger = logging.getLogger("hyadex-server")

CODEX_APP_SERVER_MANAGER_LOCK = asyncio.Lock()
CODEX_BINARY_PROBE_LOCK = asyncio.Lock()
CODEX_LOGIN_PROBE_LOCK = asyncio.Lock()
CODEX_APP_SERVER_THREAD_PIN_COUNTS: dict[str, int] = {}
CODEX_APP_SERVER_EVICTING_THREADS: dict[str, asyncio.Event] = {}


CODEX_INTERACTIVE_CONTROL_THREADS: set[str] = set()
CODEX_INTERACTIVE_CONTROL_THREAD_COUNTS: dict[str, int] = {}
CODEX_NATIVE_ACTION_TASKS: dict[tuple[str, str], asyncio.Task[Any]] = {}
# Exact reservation owner for a native-control terminal row that has released
# BUSY but still blocks new admission until publication cleanup completes.
CODEX_CONTROL_TERMINAL_FENCES: dict[str, str] = {}


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


def codex_manager_has_callers(manager: CodexAppServerManager, session_id: str | None = None, *, ignore_task=None) -> bool:
    return any(task is not ignore_task and not task.done() and (session_id is None or not owner or owner == session_id)
               for task, owner in tuple(getattr(manager, "_hyadex_callers", {}).items()))


def codex_manager_work_belongs_to_session(work: Any, session_id: str) -> bool:
    thread_id = getattr(work, "_codex_thread_id", "")
    owner = codex_session_id_for_thread(thread_id) if thread_id else None
    # Unknown ownership stays process-wide. Work for a known different chat
    # must not prevent an idle chat from releasing its own subscription.
    return owner is None or owner == session_id


def existing_codex_app_server_manager_for_thread(thread_id: str) -> CodexAppServerManager | None:
    session_id = codex_session_id_for_thread(thread_id)
    if session_id and session_id in STORE.sessions:
        return existing_codex_app_server_manager(STORE.sessions[session_id])
    return next((item for item in codex_app_server_managers() if item.is_thread_loaded(thread_id)), None)


async def prepare_codex_app_server_process(manager: CodexAppServerManager) -> None:
    manager._hyadex_binary_identity = await asyncio.to_thread(codex_binary_identity)
    manager._hyadex_login_revision = await asyncio.to_thread(codex_auth.native_login_revision,
        codex_app_server_env(), cwd=existing_cwd(DEFAULT_CWD))


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


def register_codex_app_server_child(pid: int, pgid: int | None) -> None:
    with suppress(Exception):
        register_provider_child(pid, pgid, kind="codex-app-server")


def unregister_codex_app_server_child(pid: int, _pgid: int | None) -> None:
    with suppress(Exception):
        unregister_provider_child(pid)
