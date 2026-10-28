"""Projecting Codex app-server notifications into chat events: messages, reasoning, tools and token usage."""
from __future__ import annotations

import asyncio
from contextlib import suppress
import json
import logging.handlers
import re
import signal
import time
from typing import Any
import uuid

from hyadex.core.config import (
    BACKEND_CODEX,
    CODEX_CONTEXT_WINDOW_RESERVE_TOKENS,
    CODEX_GOAL_CONTROL_TIMEOUT_SECONDS,
    CODEX_GOALS_ENABLED,
    CODEX_TRANSPORT_APP_SERVER,
    DEFAULT_BACKEND,
)
from hyadex.core.paths import now_iso
from hyadex.core.runs import RUNS
from hyadex.core.server_logging import concise_error_message
from hyadex.core.sessions import (
    append_event,
    remember_codex_native_compaction_terminal,
    session_lifecycle_lock,
    STORE,
)
from hyadex.core.session_settings import codex_goal_time_budget_is_exhausted
from hyadex.core.session_codex import (
    codex_app_server_managers,
    CODEX_RETIRED_APP_SERVER_MANAGERS,
    CODEX_SESSION_APP_SERVER_MANAGERS,
    CODEX_SUBAGENT_INDEX_LOCK,
    CODEX_SUBAGENT_SESSION_INDEX,
    CODEX_SUBAGENT_STATE,
    existing_codex_app_server_manager,
    native_codex_compaction_id,
    session_codex_thread_id,
)
from hyadex.core.util import clean_assistant_text
from hyadex.providers.token_totals import add_codex_thread_total_locked
from hyadex.features.usage import (
    bounded_codex_interaction_value,
    broadcast_provider_runtime_changed,
    broadcast_provider_usage_changed,
    next_provider_context_usage_generation,
    provider_context_usage_signal,
    PROVIDER_USAGE,
)
from hyadex.providers.claude.history import (
    native_session_title,
    session_provider_id,
    text_from_content,
)
from hyadex.providers.codex.app_server_manager import CodexAppServerManager
from hyadex.providers.codex.goals import (
    CODEX_QUARANTINED_GOAL_THREADS,
    persist_reconciled_codex_goal,
    quarantine_codex_goal_thread,
)
from hyadex.providers.codex.managers import (
    existing_codex_app_server_manager_for_thread,
    register_codex_native_action,
)
from hyadex.providers.codex.threads import (
    codex_native_compaction_was_terminal,
    codex_session_id_for_thread,
    codex_thread_has_local_run_owner,
    codex_thread_status_message,
)
from hyadex.providers.codex.subagents import (
    codex_child_status_from_thread,
    codex_collaboration_thread_ids,
    codex_subagent_thread_identity,
    emit_codex_subagent_state,
    project_codex_subagent_item,
)
from hyadex.providers.claude.subagents import normalize_subagent_status

logger = logging.getLogger("hyadex-server")


async def codex_session_has_active_run(session_id: str) -> bool:
    """Whether the chat currently holds a turn slot or an ACTIVE run record."""

    async with RUNS.active_lock:
        return session_id in RUNS.busy or bool(RUNS.active.get(session_id))


CODEX_TOKEN_USAGE_PERSIST_INTERVAL_SECONDS = 30.0


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
        add_codex_thread_total_locked(session, snapshot_thread_id,
                                      session["codex_token_usage"].get("total"))
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
