"""Forking chats: copying history, files and provider threads into a new chat."""
from __future__ import annotations

import asyncio
from contextlib import suppress
import logging.handlers
from typing import Any

from fastapi import HTTPException
from pydantic import BaseModel

from hyadex.core.config import (
    BACKEND_CLAUDE,
    BACKEND_CODEX,
    DEFAULT_BACKEND,
    FORK_INTERNAL_PURPOSES,
)
from hyadex.core.paths import validated_fork_cwd
from hyadex.core.queue import claude_completed_fork_boundary
from hyadex.core.server_logging import concise_error_message
from hyadex.core.sessions import (
    ABANDONED_FORK_PROVIDER_THREADS,
    append_event,
    CODEX_SUBAGENT_INDEX_LOCK,
    CODEX_SUBAGENT_LIVE_GENERATIONS,
    CODEX_SUBAGENT_LIVE_MANAGERS,
    CODEX_SUBAGENT_SESSION_INDEX,
    CODEX_SUBAGENT_STATE,
    CODEX_THREAD_SESSION_INDEX,
    CreateSessionRequest,
    effective_claude_permission_mode,
    effective_provider_jobs_access,
    event_delivery_lock,
    public_session,
    sorted_sessions,
    STORE,
    write_abandoned_fork_thread_ids,
)
from hyadex.core.state import DELETED_SESSION_TOMBSTONES, DELETING_SESSIONS
from hyadex.features.history import copy_fork_history
from hyadex.features.usage import join_task_despite_caller_cancellation
from hyadex.providers.claude.runtime import validated_claude_fork_provider_id
from hyadex.providers.codex.app_server import (
    CodexAppServerDisconnected,
    CodexAppServerRequestError,
    CodexAppServerTimeout,
)
from hyadex.providers.codex.runtime import (
    ABANDONED_FORK_PROVIDER_THREADS_LOCK,
    bind_forked_codex_thread,
    build_fork_memory,
    CODEX_GOAL_SYNC_GENERATIONS,
    CODEX_QUARANTINED_GOAL_THREADS,
    CodexForkCleanupError,
    forget_abandoned_fork_provider_thread,
    fork_codex_thread,
    retire_failed_codex_fork,
    retire_or_record_failed_codex_fork,
    session_references_codex_thread,
)

logger = logging.getLogger("hyadex-server")


class ForkSessionRequest(BaseModel):
    title: str | None = None


async def save_staged_fork_provider_reference(
    session_id: str,
    thread_id: str,
) -> None:
    """Persist the provider ID before the cleanup ledger can be released."""

    clean_thread_id = str(thread_id or "").strip()
    if not clean_thread_id:
        raise ValueError("missing staged provider fork thread")
    async with STORE.transaction():
        session = STORE.sessions.get(session_id)
        if not session or not session.get("_fork_initializing"):
            raise RuntimeError("staged fork session is unavailable")
        previous_thread_id = session.get("codex_thread_id")
        session["codex_thread_id"] = clean_thread_id
        try:
            await STORE.save()
        except BaseException:
            session["codex_thread_id"] = previous_thread_id
            raise


async def clear_staged_fork_provider_reference(
    session_id: str,
    thread_id: str,
) -> None:
    """Release staged ownership only after cleanup is independently durable."""

    clean_thread_id = str(thread_id or "").strip()
    async with STORE.transaction():
        session = STORE.sessions.get(session_id)
        if not session or not session.get("_fork_initializing"):
            raise RuntimeError("staged fork session is unavailable")
        current_thread_id = str(session.get("codex_thread_id") or "").strip()
        if current_thread_id and current_thread_id != clean_thread_id:
            raise RuntimeError("staged fork provider identity changed unexpectedly")
        previous_session_id = session.get("session_id")
        previous_thread_id = session.get("codex_thread_id")
        if str(session.get("session_id") or "").strip() == clean_thread_id:
            session["session_id"] = None
        session["codex_thread_id"] = None
        try:
            await STORE.save()
        except BaseException:
            session["session_id"] = previous_session_id
            session["codex_thread_id"] = previous_thread_id
            raise


async def cleanup_abandoned_fork_provider_threads(
    startup_thread_ids: set[str] | None = None,
) -> None:
    """Retire durable provider orphans without delaying server startup."""

    async with ABANDONED_FORK_PROVIDER_THREADS_LOCK:
        pending = set(ABANDONED_FORK_PROVIDER_THREADS)
        if startup_thread_ids is not None:
            # The startup worker runs after the server begins accepting
            # requests. Never let it consume a journal entry created by a new
            # in-flight fork after STORE.load() completed.
            pending.intersection_update(startup_thread_ids)
    if not pending:
        return

    resolved: set[str] = set()
    for thread_id in sorted(pending):
        # The ledger is recovery state, but never trust it over current session
        # ownership. A manually resumed/imported thread must not be deleted.
        if any(
            session_references_codex_thread(session, thread_id)
            for session in STORE.sessions.values()
            if isinstance(session, dict)
            and not session.get("_fork_initializing")
        ):
            logger.warning(
                "discarding stale abandoned-fork cleanup entry for owned thread=%s",
                thread_id,
            )
            resolved.add(thread_id)
            continue
        try:
            if await retire_failed_codex_fork(thread_id):
                resolved.add(thread_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "abandoned provider fork cleanup failed thread=%s: %s",
                thread_id,
                concise_error_message(exc),
            )

    if not resolved:
        return
    async with ABANDONED_FORK_PROVIDER_THREADS_LOCK:
        remaining = ABANDONED_FORK_PROVIDER_THREADS - resolved
        try:
            await asyncio.to_thread(
                write_abandoned_fork_thread_ids,
                remaining,
            )
        except OSError as exc:
            # Keep the in-memory entries too; a later retry or restart must
            # still know that durable cleanup was not committed.
            logger.warning(
                "could not update abandoned fork cleanup ledger: %s",
                concise_error_message(exc),
            )
            return
        ABANDONED_FORK_PROVIDER_THREADS.clear()
        ABANDONED_FORK_PROVIDER_THREADS.update(remaining)


async def cleanup_aborted_session_fork(
    cleanup_state: dict[str, str | None],
) -> None:
    """Best-effort cleanup that survives cancellation of the HTTP request."""

    provider_thread_id = str(
        cleanup_state.get("provider_thread_id") or ""
    ).strip()
    child_session_id = str(cleanup_state.get("child_session_id") or "").strip()

    # Fence synchronously, before the first await. A provider callback may
    # already be queued when the HTTP task is cancelled; it must neither route
    # back to this staged child nor recreate its directory via append_event.
    if child_session_id:
        DELETING_SESSIONS.add(child_session_id)
        DELETED_SESSION_TOMBSTONES.add(child_session_id)
        for thread_id, owner_session_id in tuple(
            CODEX_THREAD_SESSION_INDEX.items()
        ):
            if owner_session_id == child_session_id:
                CODEX_THREAD_SESSION_INDEX.pop(thread_id, None)
        with CODEX_SUBAGENT_INDEX_LOCK:
            owned_child_threads = [
                thread_id
                for thread_id, owner_session_id
                in CODEX_SUBAGENT_SESSION_INDEX.items()
                if owner_session_id == child_session_id
            ]
            for thread_id in owned_child_threads:
                CODEX_SUBAGENT_SESSION_INDEX.pop(thread_id, None)
                CODEX_SUBAGENT_STATE.pop(thread_id, None)
                CODEX_SUBAGENT_LIVE_GENERATIONS.pop(thread_id, None)
                CODEX_SUBAGENT_LIVE_MANAGERS.pop(thread_id, None)
        for thread_id, owner_session_id in tuple(
            CODEX_QUARANTINED_GOAL_THREADS.items()
        ):
            if owner_session_id == child_session_id:
                CODEX_QUARANTINED_GOAL_THREADS.pop(thread_id, None)
        CODEX_GOAL_SYNC_GENERATIONS.pop(child_session_id, None)

    async def delete_local_child() -> None:
        # An append that passed its second deletion check already owns this
        # lock. Join it before removing the directory; all later appends see
        # the tombstone and return without touching the filesystem.
        async with event_delivery_lock(child_session_id):
            await STORE.delete(child_session_id)

    async def cleanup_fork_resources() -> None:
        provider_reference_is_durable = not provider_thread_id
        if provider_thread_id:
            try:
                provider_reference_is_durable = await retire_or_record_failed_codex_fork(
                    provider_thread_id
                )
            except BaseException as exc:
                if isinstance(exc, asyncio.CancelledError):
                    raise
                logger.warning(
                    "aborted provider fork retirement failed thread=%s: %s",
                    provider_thread_id,
                    concise_error_message(exc),
                )
        if child_session_id and provider_reference_is_durable:
            await delete_local_child()
        elif child_session_id:
            logger.warning(
                "retaining hidden staged fork after cleanup persistence failure "
                "session=%s thread=%s",
                child_session_id,
                provider_thread_id,
            )

    cleanup_task = asyncio.create_task(cleanup_fork_resources())
    try:
        with suppress(BaseException):
            await join_task_despite_caller_cancellation(cleanup_task)
    finally:
        if child_session_id:
            # The durable runtime fence is the tombstone. "Deleting" only
            # describes the period while local/provider cleanup is joining.
            DELETING_SESSIONS.discard(child_session_id)


def completed_fork_events(
    events: list[dict[str, Any]],
    *,
    active_run_id: str,
    through_sequence: int | None,
) -> list[dict[str, Any]]:
    """Freeze the visible completed prefix, never a partially observed turn."""
    prefix: list[dict[str, Any]] = []
    completed_length = 0
    for event in events:
        if through_sequence is not None and int(event.get("seq") or 0) > through_sequence:
            break
        if active_run_id and str(event.get("run_id") or "") == active_run_id:
            break
        prefix.append(event)
        if (
            event.get("type") == "turn_finished"
            and (event.get("exit_code") == 0 or event.get("imported") is True)
            and not event.get("stopped")
            and not event.get("is_error")
            # Replay bookkeeping is not a newly completed provider turn. In
            # particular, metadata-only repair batches carry no native turn
            # identity and must not replace an already resumable boundary.
            and not (event.get("imported") is True and event.get("metadata_only") is True)
            and event.get("purpose") not in FORK_INTERNAL_PURPOSES
        ):
            completed_length = len(prefix)
    return prefix[:completed_length]


async def _fork_session_locked(
    session_id: str,
    req: ForkSessionRequest,
    *,
    cleanup_state: dict[str, str | None] | None = None,
    completed_snapshot: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if cleanup_state is None:
        cleanup_state = {}
    parent_record = STORE.sessions.get(session_id)
    parent = dict(parent_record) if isinstance(parent_record, dict) else None
    if not parent:
        raise HTTPException(status_code=404, detail="session not found")
    fork_cwd = validated_fork_cwd(parent)
    parent["cwd"] = fork_cwd
    parent_backend = (parent.get("backend") or DEFAULT_BACKEND).lower()
    live_snapshot = completed_snapshot is not None
    empty_snapshot = live_snapshot and not completed_snapshot
    parent_codex_thread_id = parent.get("codex_thread_id") or (
        parent.get("session_id") if parent_backend == BACKEND_CODEX else None
    )
    parent_claude_session_id = (
        validated_claude_fork_provider_id(parent, session_id, fork_cwd)
        if parent_backend == BACKEND_CLAUDE and not empty_snapshot
        else None
    )
    # A not-yet-started Claude child still resumes its ancestor at this exact
    # cutoff. Forking that child again must not lose the inherited boundary.
    claude_cutoff = parent.get("fork_resume_session_at") if parent.get("fork_from") and not empty_snapshot else None
    codex_cutoff = None
    if live_snapshot and not empty_snapshot:
        terminal = completed_snapshot[-1]
        if parent_backend == BACKEND_CLAUDE and not parent_claude_session_id:
            raise HTTPException(status_code=409, detail="The completed Claude turn has no resumable provider snapshot. The running chat was left unchanged.")
        if parent_backend == BACKEND_CLAUDE and parent_claude_session_id:
            try:
                claude_cutoff = await asyncio.to_thread(
                    claude_completed_fork_boundary, parent, parent_claude_session_id, completed_snapshot,
                )
            except (OSError, ValueError) as exc:
                raise HTTPException(status_code=409, detail="The completed Claude transcript snapshot is unavailable. The running chat was left unchanged.") from exc
        elif parent_backend == BACKEND_CODEX:
            codex_cutoff = str(terminal.get("provider_turn_id") or "")
            if not parent_codex_thread_id or not codex_cutoff or str(terminal.get("provider_thread_id") or "") != str(parent_codex_thread_id):
                raise HTTPException(status_code=409, detail="The last completed Codex turn has no verifiable native snapshot. The running chat was left unchanged; retry after it finishes.")
    forked_codex_thread_id: str | None = None
    codex_fork_error: str | None = None

    if parent_backend == BACKEND_CODEX and parent_codex_thread_id and not empty_snapshot:
        try:
            forked_codex_thread_id = await fork_codex_thread(
                str(parent_codex_thread_id), parent,
                **({"last_turn_id": codex_cutoff} if codex_cutoff else {}),
            )
            cleanup_state["provider_thread_id"] = forked_codex_thread_id
            logger.info(
                "forked codex thread parent_session=%s source_thread=%s forked_thread=%s",
                session_id,
                parent_codex_thread_id,
                forked_codex_thread_id,
            )
        except CodexForkCleanupError as e:
            # The low-level fork could neither delete nor journal its provider
            # object. Preserve the ID for the endpoint's cancellation-safe
            # cleanup and fail closed instead of publishing a memory fallback.
            cleanup_state["provider_thread_id"] = e.thread_id
            raise
        except Exception as e:
            reason = {
                "thread/fork ancestry could not be verified": "Codex returned a fork with unverifiable ancestry.",
                "thread/fork working directory could not be verified": "Codex returned a fork in a different working directory.",
                "thread/fork completed-turn boundary could not be verified": "Codex returned a fork that did not end at the last completed turn.",
                "thread/fork cleanup journal could not be persisted": "The fork recovery record could not be saved.",
                "thread/fork returned the source thread id": "Codex did not create a separate conversation.",
                "thread/fork returned a thread with a different source id": "Codex returned a fork from a different conversation.",
            }.get(str(e), "Codex could not create or verify the completed-turn fork.")
            if isinstance(e, CodexAppServerTimeout):
                reason = "Codex timed out while creating or verifying the fork."
            elif isinstance(e, CodexAppServerDisconnected):
                reason = "The Codex connection closed while creating or verifying the fork."
            elif isinstance(e, CodexAppServerRequestError):
                reason = "Codex rejected the request to create or verify the fork."
            logger.warning(
                "codex fork failed parent_session=%s source_thread=%s cutoff=%s error_type=%s: %s",
                session_id,
                parent_codex_thread_id,
                codex_cutoff,
                type(e).__name__,
                reason,
            )
            if live_snapshot:
                raise HTTPException(status_code=409, detail=f"{reason} The running chat was left unchanged.") from e
            codex_fork_error = str(e)
    elif parent_backend == BACKEND_CODEX and not empty_snapshot:
        # A goal-reconciliation rollover can deliberately detach an unsafe
        # provider thread. A UI fork must still inherit bounded conversational
        # memory; silently creating an empty Codex thread makes the fork look
        # correct while the model has none of the displayed context.
        codex_fork_error = (
            "the parent Codex provider thread is unavailable; "
            "using a bounded memory fork"
        )

    child = await STORE.create(
        CreateSessionRequest(
            title=req.title or f"Fork of {parent.get('title') or session_id}",
            folder=parent.get("folder"),
            cwd=parent.get("cwd"),
            backend=parent_backend,
            model=parent.get("model"),
            effort=parent.get("effort"),
            subagent_limit=parent.get("subagent_limit"),
            system_prompt=parent.get("system_prompt"),
            claude_permission_mode=effective_claude_permission_mode(parent),
            codex_approval_policy=parent.get("codex_approval_policy"),
            codex_sandbox_mode=parent.get("codex_sandbox_mode"),
            codex_permission_profile=parent.get("codex_permission_profile"),
            codex_approvals_reviewer=parent.get("codex_approvals_reviewer"),
            provider_jobs_access=effective_provider_jobs_access(parent),
            pinned=bool(parent.get("pinned")),
            archived=bool(parent.get("archived")),
            # Bind a native Codex fork only after the child session ID
            # exists, because manifest/jobs/terminal policy is chat-scoped.
            provider_session_id=None,
            codex_thread_id=None,
        ),
        parent_id=session_id,
        initializing_fork=True,
    )
    cleanup_state["child_session_id"] = child["id"]

    if forked_codex_thread_id:
        # Transfer crash-recovery ownership from the cleanup ledger to the
        # hidden staged child before binding. Startup can reconstruct the
        # ledger from this child if the process dies at any later point.
        await save_staged_fork_provider_reference(
            child["id"],
            forked_codex_thread_id,
        )
        if not await forget_abandoned_fork_provider_thread(
            forked_codex_thread_id
        ):
            raise RuntimeError(
                "could not commit native fork cleanup ownership transfer"
            )
        child = STORE.sessions[child["id"]]

    if str(child.get("cwd") or "") != fork_cwd:
        raise HTTPException(
            status_code=409,
            detail="the forked chat did not preserve the parent working directory",
        )

    parent_goal = parent.get("codex_goal")
    parent_goal_active = bool(
        isinstance(parent_goal, dict)
        and str(parent_goal.get("status") or "") == "active"
    )
    if parent_backend == BACKEND_CODEX and isinstance(parent_goal, dict):
        async with STORE.transaction():
            current = STORE.sessions.get(child["id"])
            if current:
                current["codex_goal"] = {
                    **parent_goal,
                    "status": "paused" if parent_goal_active else parent_goal.get("status"),
                }
                if parent.get("codex_goal_time_budget_seconds") is not None:
                    current["codex_goal_time_budget_seconds"] = parent.get(
                        "codex_goal_time_budget_seconds"
                    )
                await STORE.save()
                child = current
    if forked_codex_thread_id:
        try:
            forked_codex_thread_id, _instruction_hash = (
                await bind_forked_codex_thread(
                    child["id"],
                    forked_codex_thread_id,
                    child,
                    require_goal_support=isinstance(parent_goal, dict),
                    expected_goal=(
                        dict(parent_goal)
                        if isinstance(parent_goal, dict)
                        else None
                    ),
                )
            )
            cleanup_state["provider_thread_id"] = forked_codex_thread_id
            child = STORE.sessions[child["id"]]
        except Exception as exc:
            logger.warning(
                "codex fork policy bind failed parent_session=%s child_session=%s "
                "forked_thread=%s: %s",
                session_id,
                child["id"],
                forked_codex_thread_id,
                exc,
            )
            cleanup_is_durable = await retire_or_record_failed_codex_fork(
                forked_codex_thread_id
            )
            if not cleanup_is_durable:
                # bind_forked_codex_thread deliberately restores the provider
                # ID on the hidden staged child. Leave both cleanup references
                # intact and fail closed; the outer cancellation-safe cleanup
                # will retry and retain the child if persistence remains down.
                raise RuntimeError(
                    "failed Codex fork could not be retired or journaled"
                ) from exc
            await clear_staged_fork_provider_reference(
                child["id"],
                forked_codex_thread_id,
            )
            cleanup_state["provider_thread_id"] = None
            if live_snapshot:
                raise HTTPException(status_code=409, detail="The completed-turn fork could not be bound safely. The running chat was left unchanged.") from exc
            codex_fork_error = str(exc)
            forked_codex_thread_id = None
    ordered_sessions = await STORE.reorder(child["id"], target_id=session_id, placement="after")
    child = STORE.sessions[child["id"]]
    if parent_backend == BACKEND_CODEX and codex_fork_error:
        child["memory_seed"] = await asyncio.to_thread(
            build_fork_memory,
            parent,
            session_id,
            reason=codex_fork_error,
        )
        child["memory_seed_used"] = False
        child["memory_forked"] = True
        child["memory_fork_reason"] = codex_fork_error[:2000]
        async with STORE.transaction():
            STORE.sessions[child["id"]] = child
            await STORE.save()
    if parent_claude_session_id:
        child["fork_from"] = parent_claude_session_id
        if claude_cutoff:
            child["fork_resume_session_at"] = claude_cutoff
        async with STORE.transaction():
            STORE.sessions[child["id"]] = child
            await STORE.save()
    # Individual missing/corrupt attachments degrade to visible artifact_error
    # rows inside copy_fork_history. Any exception escaping that function is a
    # transactional clone failure, so fail closed and let the outer wrapper
    # remove both the local child and its provider fork.
    copied = await copy_fork_history(
        session_id, child["id"],
        **({"source_events": completed_snapshot} if live_snapshot else {}),
    )
    history_copy_error: str | None = None
    if forked_codex_thread_id:
        await append_event(
            child["id"],
            "provider_session",
            {
                "backend": BACKEND_CODEX,
                "provider_session_id": forked_codex_thread_id,
                "forked_from_provider_id": parent_codex_thread_id,
                "copied_events": copied,
                "history_copy_error": history_copy_error,
            },
        )
    elif codex_fork_error:
        await append_event(
            child["id"],
            "history_imported",
            {
                "backend": BACKEND_CODEX,
                "provider_session_id": parent_codex_thread_id,
                "message": "The native Codex provider thread could not be forked, so this chat uses bounded rough history. The first turn will seed a fresh Codex thread with that memory.",
                "error": codex_fork_error[:4000],
                "copied_events": copied,
                "history_copy_error": history_copy_error,
            },
        )
    else:
        # Claude and any future backend still need one current event so
        # copied source timestamps do not become the child's latest activity.
        await append_event(
            child["id"],
            "history_imported",
            {
                "backend": parent_backend,
                "message": (
                    "Forked "
                    f"{copied} conversation events from the parent chat. "
                ).strip(),
                "copied_events": copied,
                "history_copy_error": history_copy_error,
            },
        )
    # Copied events retain source timestamps for truthful chronology. Keep the
    # newly created chat itself at the current edge of the sidebar ordering and
    # persist any memory-fork metadata after the clone completes.
    child = await STORE.update(child["id"], {})
    async with STORE.transaction():
        current = STORE.sessions.get(child["id"])
        if current is None:
            raise HTTPException(status_code=409, detail="forked session disappeared before publish")
        initializing_marker = current.pop("_fork_initializing", None)
        try:
            await STORE.save()
        except BaseException:
            # Visibility is committed by the same durable save that removes
            # the marker. Restore the in-memory fence if persistence fails so
            # list/get/turn routes cannot observe a half-published child.
            if initializing_marker is not None:
                current["_fork_initializing"] = initializing_marker
            raise
        child = current
    # The fork is now complete and durable. Cancellation after this commit
    # point must leave the usable child in place rather than rolling it back.
    cleanup_state["provider_thread_id"] = None
    cleanup_state["child_session_id"] = None
    try:
        await append_event(
            session_id,
            "session_forked",
            {
                "child_id": child["id"],
                "copied_events": copied,
                "history_copy_error": history_copy_error,
            },
        )
    except asyncio.CancelledError:
        # The child is already committed and must survive request
        # cancellation. Preserve cancellation semantics for the caller.
        raise
    except Exception:
        # A parent-side audit marker is best effort and must never roll back a
        # fully committed, independently usable child.
        logger.exception(
            "fork parent audit event failed parent_session=%s child_session=%s",
            session_id,
            child["id"],
        )
    ordered_sessions = sorted_sessions([
        session
        for session in STORE.sessions.values()
        if not session.get("_fork_initializing")
    ])
    return {"session": public_session(child), "sessions": [public_session(sess) for sess in ordered_sessions]}
