"""Claude context usage: sampling, recording and refreshing idle chats."""
from __future__ import annotations

import asyncio
from contextlib import suppress
import importlib.util
import logging.handlers
import math
import signal
from typing import Any

from fastapi import HTTPException

from hyadex.core.config import (
    BACKEND_CLAUDE,
    CLAUDE_CONTEXT_USAGE_TIMEOUT_SECONDS,
    CLAUDE_TRANSPORT,
    CLAUDE_TRANSPORT_AGENT_SDK,
    CLAUDE_TRANSPORT_PRINT,
    DEFAULT_BACKEND,
)
from hyadex.core.paths import now_iso
from hyadex.core.runs import RUNS
from hyadex.core.server_logging import concise_error_message
from hyadex.core.sessions import ensure_session_not_deleting, session_lifecycle_lock, STORE
from hyadex.core.state import DELETED_SESSION_TOMBSTONES, DELETING_SESSIONS
from hyadex.features.usage import (
    broadcast_provider_runtime_changed,
    clear_provider_context_usage_locked,
    current_provider_context_usage_generation,
    join_task_despite_caller_cancellation,
    next_provider_context_usage_generation,
    provider_context_usage_signal,
)
from hyadex.providers.state import PROVIDERS

logger = logging.getLogger("hyadex-server")


def claude_provider_id_for_session(sess: dict[str, Any]) -> str | None:
    provider_id = sess.get("claude_session_id") or (
        sess.get("session_id") if sess.get("backend") == BACKEND_CLAUDE else None
    )
    if sess.get("fork_from"):
        provider_id = sess["fork_from"]
    provider_id = str(provider_id or "").strip()
    return provider_id or None


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


def claude_sdk_dependency_available() -> bool:
    return importlib.util.find_spec("claude_agent_sdk") is not None
