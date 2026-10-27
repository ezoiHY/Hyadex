"""Provider subagents: native status, limits and their projection into chats."""
from __future__ import annotations

import asyncio
from collections import OrderedDict
from contextlib import suppress
import inspect
import json
import logging.handlers
import re
from typing import Any
import uuid
import weakref

from fastapi import HTTPException
from pydantic import BaseModel, Field

from claude_background_reconciliation import normalize_task_receipts
from hyadex.core.config import (
    BACKEND_CLAUDE,
    BACKEND_CODEX,
    CODEX_SETTINGS_FILE,
    CODEX_SUBAGENT_RECONCILE_LIMIT,
    CODEX_TRANSPORT,
    CODEX_TRANSPORT_EXEC,
    hyadex_setting,
    SERVER_INSTANCE_ID,
    STOP_CONFIRM_TIMEOUT_SECONDS,
    SUBAGENT_SNAPSHOT_LOG_LIMIT,
    SUBAGENT_SNAPSHOT_STATE_LIMIT,
    SUBAGENT_SNAPSHOT_TEXT_LIMIT,
)
from hyadex.core.paths import events_path, now_iso
from hyadex.core.runs import RUNS
from hyadex.core.server_logging import concise_error_message
from hyadex.core.sessions import (
    append_event,
    codex_app_server_managers,
    CODEX_SUBAGENT_INDEX_LOCK,
    CODEX_SUBAGENT_LIVE_GENERATIONS,
    CODEX_SUBAGENT_LIVE_MANAGERS,
    CODEX_SUBAGENT_SESSION_INDEX,
    CODEX_SUBAGENT_STATE,
    CODEX_THREAD_SESSION_INDEX,
    durable_event_seq,
    existing_codex_app_server_manager,
    session_codex_thread_id,
    STORE,
)
from hyadex.core.util import utc_timestamp
from hyadex.providers.claude.sdk import ClaudeSDKSupervisorManager
from hyadex.providers.codex.app_server import CodexAppServerManager
from hyadex.providers.codex.config import (
    _codex_config_positive_int,
    codex_effective_thread_config,
    CODEX_THREAD_CONFIG_LEGACY_MAX_THREADS_KEY,
    sanitize_codex_thread_config,
)
from hyadex.providers.codex.history import codex_runtime_user_item_kind
from hyadex.providers.state import PROVIDERS
from hyadex.store.files import atomic_json

logger = logging.getLogger("hyadex-server")


class CodexSubagentsAdminRequest(BaseModel):
    model_config = {"extra": "forbid"}
    # Bound only by lossless JSON/desktop integer transport, not an app quota.
    max_concurrent_threads_per_session: int | None = Field(
        ..., strict=True, ge=1, le=9007199254740991,
    )


CLAUDE_STOP_FENCE_ATTEMPT_LOCKS: dict[str, asyncio.Lock] = {}
CLAUDE_STOP_FENCE_ATTEMPT_TASKS: dict[str, set[asyncio.Task[Any]]] = {}


CODEX_GOALS_CONFIG_LOCK = asyncio.Lock()


CODEX_SUBAGENT_TRANSITION_LOCKS: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()


def compact_subagent_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:SUBAGENT_SNAPSHOT_TEXT_LIMIT]


def useful_subagent_identity_text(value: Any) -> str:
    """Return a compact identity label, excluding generated prompt wrappers."""

    clean = compact_subagent_text(value)
    if (
        not clean
        or clean.casefold() == "codex subagent"
        or re.match(r"^\[Hyadex context\](?:\s|$)", clean, re.IGNORECASE)
    ):
        return ""
    return clean


def codex_subagent_thread_identity(
    thread: dict[str, Any],
) -> tuple[str, str, str]:
    """Read authoritative nickname/path/parent fields from a Codex thread."""

    source = thread.get("source") or thread.get("threadSource")
    source = source if isinstance(source, dict) else {}
    subagent_source = source.get("subagent") or source.get("subAgent")
    subagent_source = subagent_source if isinstance(subagent_source, dict) else {}
    spawn = (
        subagent_source.get("thread_spawn")
        or subagent_source.get("threadSpawn")
        or source.get("thread_spawn")
        or source.get("threadSpawn")
    )
    spawn = spawn if isinstance(spawn, dict) else {}
    nickname = useful_subagent_identity_text(
        thread.get("agentNickname")
        or thread.get("agent_nickname")
        or spawn.get("agentNickname")
        or spawn.get("agent_nickname")
    )
    agent_path = useful_subagent_identity_text(
        thread.get("agentPath")
        or thread.get("agent_path")
        or spawn.get("agentPath")
        or spawn.get("agent_path")
    )
    parent_thread_id = str(
        thread.get("parentThreadId")
        or thread.get("parent_thread_id")
        or spawn.get("parentThreadId")
        or spawn.get("parent_thread_id")
        or ""
    ).strip()
    return nickname, agent_path, parent_thread_id


def normalize_subagent_status(value: Any) -> str:
    status = str(value or "").strip().lower()
    if status == "tracking_lost":
        return "tracking_lost"
    if status in {"completed", "complete", "done"}:
        return "completed"
    if status in {"failed", "error", "errored", "systemerror", "notfound"}:
        return "failed"
    if status in {
        "stopped",
        "cancelled",
        "canceled",
        "killed",
        "interrupted",
        "shutdown",
        "closed",
    }:
        return "stopped"
    if status in {"starting", "pending", "pendinginit", "queued"}:
        return "starting"
    return "running"


async def emit_codex_subagent_state(
    session_id: str,
    child_thread_id: str,
    status: Any,
    **kwargs: Any,
) -> dict[str, Any] | None:
    """Serialize one child's complete read/append/publish transition only.

    Parent collaboration packets and child rename packets have separate
    transport callback lanes. Their child state must still commit in order.
    Weak ownership removes an idle lock as soon as its last caller exits.
    """
    child_thread_id = str(child_thread_id or "").strip()
    if not child_thread_id or session_id not in STORE.sessions:
        return None
    with CODEX_SUBAGENT_INDEX_LOCK:
        lock = CODEX_SUBAGENT_TRANSITION_LOCKS.get(child_thread_id)
        if lock is None:
            lock = asyncio.Lock()
            CODEX_SUBAGENT_TRANSITION_LOCKS[child_thread_id] = lock
    async with lock:
        return await _emit_codex_subagent_state_once(session_id, child_thread_id, status, **kwargs)


async def _emit_codex_subagent_state_once(
    session_id: str,
    child_thread_id: str,
    status: Any,
    *,
    parent_thread_id: str | None = None,
    run_id: str | None = None,
    tool_id: str | None = None,
    name: Any = None,
    title: Any = ...,
    nickname: Any = None,
    agent_path: Any = None,
    activity: Any = None,
    summary: Any = None,
    persist_event: bool = True,
    inherit_run_id: bool = True,
    identity_only: bool = False,
    reconcile_expected_state: Any = ...,
) -> dict[str, Any] | None:
    """Persist one authoritative Codex child lifecycle transition.

    With ``persist_event=False`` the child's state is recorded in memory only.
    Reconciliation uses that for children that are already terminal and whose
    terminal event was already written earlier, so re-learning the spawn tree
    after a restart does not append a duplicate event per child.

    With ``inherit_run_id=False`` an omitted ``run_id`` stays ``None`` instead
    of falling back to the child's previously recorded run. Child transitions
    that arrive after the parent's run finished must not be attributed to
    that finished run.
    """

    child_thread_id = str(child_thread_id or "").strip()
    if not child_thread_id or session_id not in STORE.sessions:
        return None
    if child_thread_id == str(
        session_codex_thread_id(STORE.sessions.get(session_id) or {})
    ).strip():
        # The chat's provider thread is the root, even when it appears as the
        # receiver of a child-to-parent collaboration message.
        with CODEX_SUBAGENT_INDEX_LOCK:
            CODEX_SUBAGENT_SESSION_INDEX.pop(child_thread_id, None)
            CODEX_SUBAGENT_STATE.pop(child_thread_id, None)
            CODEX_SUBAGENT_LIVE_GENERATIONS.pop(child_thread_id, None)
            CODEX_SUBAGENT_LIVE_MANAGERS.pop(child_thread_id, None)
        return None
    with CODEX_SUBAGENT_INDEX_LOCK:
        previous = dict(CODEX_SUBAGENT_STATE.get(child_thread_id) or {})
    if previous and str(previous.get("session_id") or "") != session_id:
        if reconcile_expected_state is not ...:
            return None
        previous = {}
    identity_previous = previous
    terminal_identity_only = False
    if reconcile_expected_state is not ...:
        indexed = (STORE.sessions.get(session_id) or {}).get("codex_subagents")
        indexed_state = indexed.get(child_thread_id) if isinstance(indexed, dict) else None
        indexed_state = dict(indexed_state) if (
            isinstance(indexed_state, dict)
            and indexed_state.get("session_id") == session_id
            and indexed_state.get("subagent_id") == child_thread_id
            and indexed_state.get("backend") in {None, BACKEND_CODEX}
            and indexed_state.get("type") in {None, "subagent_state"}
        ) else {}
        durable = indexed_state if (
            durable_event_seq(indexed_state) is not None
            and isinstance(indexed_state.get("id"), str)
            and bool(indexed_state["id"].strip())
            and indexed_state.get("backend") == BACKEND_CODEX
            and indexed_state.get("type") == "subagent_state"
        ) else {}
        # Legacy metadata can restore ownership/status without pretending to
        # be a durable Event. Snapshot filtering remains strict below.
        previous = previous or indexed_state
        # The provider read happens outside this child's transition lock. A
        # lifecycle/rename committed during that read must beat its stale view.
        if previous != (reconcile_expected_state or {}):
            return previous or None
        identity_previous = previous
        if not persist_event and durable:
            # A known terminal child's native identity can be newer than its
            # durable event. Earlier silent reconciliation may already have
            # replaced memory with corrected identity but no event ID/seq.
            # Retain that trusted identity fallback for omitted provider
            # fields, but borrow lifecycle fields from the durable baseline.
            previous = durable
            terminal_identity_only = True
            persist_event = True
            name = None  # A provider preview is not an identity correction.
    if identity_only and not previous:
        return None
    normalized = normalize_subagent_status(status or previous.get("subagent_status"))
    title_fields: dict[str, Any] = {}
    if isinstance(title, str) and title.strip() and not useful_subagent_identity_text(title):
        title = ...
    if title is None or isinstance(title, str):
        title_fields["subagent_title"] = useful_subagent_identity_text(title) or None
    elif "subagent_title" in identity_previous:
        previous_title = identity_previous.get("subagent_title")
        title_fields["subagent_title"] = (
            useful_subagent_identity_text(previous_title)
            if isinstance(previous_title, str) else None
        ) or None
    clean_nickname = (
        useful_subagent_identity_text(nickname)
        or useful_subagent_identity_text(identity_previous.get("subagent_nickname"))
    )
    clean_path = (
        useful_subagent_identity_text(agent_path)
        or useful_subagent_identity_text(identity_previous.get("subagent_path"))
    )
    clean_name = (
        clean_nickname
        or clean_path
        or useful_subagent_identity_text(identity_previous.get("subagent_name"))
        or useful_subagent_identity_text(name)
        or "Codex subagent"
    )
    clean_activity = compact_subagent_text(activity)
    clean_summary = compact_subagent_text(summary)
    resolved_parent = str(
        parent_thread_id
        or previous.get("subagent_parent_thread_id")
        or session_codex_thread_id(STORE.sessions.get(session_id) or {})
        or ""
    ).strip()
    resolved_run = (
        str(run_id or (previous.get("run_id") if inherit_run_id else None) or "")
        .strip()
        or None
    )
    started_at = str(previous.get("subagent_started_at") or previous.get("ts") or now_iso())
    log = list(previous.get("subagent_log") or [])[-SUBAGENT_SNAPSHOT_LOG_LIMIT:]
    if clean_activity and (not log or str(log[-1].get("text") or "") != clean_activity):
        log.append({"ts": now_iso(), "text": clean_activity})
        log = log[-SUBAGENT_SNAPSHOT_LOG_LIMIT:]

    payload: dict[str, Any] = {
        "run_id": resolved_run,
        "backend": BACKEND_CODEX,
        "subagent_id": child_thread_id,
        "subagent_tool_id": str(
            tool_id or previous.get("subagent_tool_id") or child_thread_id
        ),
        "subagent_name": clean_name,
        **title_fields,
        "subagent_nickname": clean_nickname or None,
        "subagent_path": clean_path or None,
        "subagent_kind": "collaborator",
        "subagent_status": normalized,
        "subagent_activity": clean_activity or previous.get("subagent_activity"),
        "subagent_summary": clean_summary or previous.get("subagent_summary"),
        "subagent_started_at": started_at,
        "subagent_provider_ref": child_thread_id,
        "subagent_parent_thread_id": resolved_parent or None,
        "subagent_log": log,
    }
    if identity_only or terminal_identity_only:
        # Preserve the exact stored lifecycle payload even if a late rename
        # overlaps a terminal transition or comes from another generation.
        identity_fields = dict(title_fields)
        if terminal_identity_only:
            identity_fields.update({key: payload[key] for key in (
                "subagent_name", "subagent_nickname", "subagent_path",
            )})
        payload = {key: previous.get(key) for key in payload if key not in identity_fields}
        payload.update(identity_fields)
        if isinstance(previous.get("ts"), str):
            payload["ts"] = previous["ts"]
    comparable_keys = (
        "run_id",
        "backend",
        "subagent_id",
        "subagent_tool_id",
        "subagent_name",
        "subagent_title",
        "subagent_nickname",
        "subagent_path",
        "subagent_status",
        "subagent_activity",
        "subagent_summary",
        "subagent_parent_thread_id",
        "subagent_log",
    )
    manager = existing_codex_app_server_manager(STORE.sessions.get(session_id))
    if not identity_only and not terminal_identity_only:
        with CODEX_SUBAGENT_INDEX_LOCK:
            if (
                normalized in {"starting", "running"}
                and manager is not None
                and getattr(manager, "ready", False) is True
                and isinstance(getattr(manager, "generation", None), int)
            ):
                CODEX_SUBAGENT_LIVE_GENERATIONS[child_thread_id] = manager.generation
                CODEX_SUBAGENT_LIVE_MANAGERS[child_thread_id] = manager
            else:
                CODEX_SUBAGENT_LIVE_GENERATIONS.pop(child_thread_id, None)
                CODEX_SUBAGENT_LIVE_MANAGERS.pop(child_thread_id, None)
    if previous and all(previous.get(key) == payload.get(key) for key in comparable_keys):
        with CODEX_SUBAGENT_INDEX_LOCK:
            CODEX_SUBAGENT_SESSION_INDEX[child_thread_id] = session_id
            if reconcile_expected_state is not ...:
                CODEX_SUBAGENT_STATE[child_thread_id] = previous
        return previous

    if not persist_event:
        stored = {
            "session_id": session_id,
            "type": "subagent_state",
            "ts": now_iso(),
            **payload,
        }
        with CODEX_SUBAGENT_INDEX_LOCK:
            CODEX_SUBAGENT_SESSION_INDEX[child_thread_id] = session_id
            CODEX_SUBAGENT_STATE[child_thread_id] = stored
        return stored

    event = await append_event(session_id, "subagent_state", payload)
    stored = {
        "session_id": session_id,
        "type": "subagent_state",
        "ts": now_iso(),
        **payload,
        **(event if isinstance(event, dict) else {}),
    }
    with CODEX_SUBAGENT_INDEX_LOCK:
        CODEX_SUBAGENT_SESSION_INDEX[child_thread_id] = session_id
        CODEX_SUBAGENT_STATE[child_thread_id] = stored
    async with STORE.transaction():
        session = STORE.sessions.get(session_id)
        if session is not None:
            indexed = session.get("codex_subagents")
            if not isinstance(indexed, dict):
                indexed = {}
            else:
                indexed = dict(indexed)
            indexed[child_thread_id] = stored
            if len(indexed) > SUBAGENT_SNAPSHOT_STATE_LIMIT:
                ordered = sorted(
                    indexed.items(),
                    key=lambda pair: int(pair[1].get("seq") or 0),
                    reverse=True,
                )[:SUBAGENT_SNAPSHOT_STATE_LIMIT]
                indexed = dict(reversed(ordered))
            session["codex_subagents"] = indexed
            await STORE.save()
    return stored


def build_subagent_snapshot(session_id: str, limit: int = 64) -> dict[str, Any]:
    """Merge provider-native snapshots without relying on transcript inference."""

    limit = max(1, min(int(limit or 64), SUBAGENT_SNAPSHOT_STATE_LIMIT))
    merged: OrderedDict[str, dict[str, Any]] = OrderedDict()
    session = STORE.sessions.get(session_id) or {}
    if str(session.get("backend") or "") != BACKEND_CODEX:
        claude = build_claude_subagent_snapshot(
            session_id,
            SUBAGENT_SNAPSHOT_STATE_LIMIT,
        )
        for state in claude.get("subagents") or []:
            key = f"{str(state.get('backend') or BACKEND_CLAUDE)}:{state.get('subagent_id')}"
            merged[key] = dict(state)
    with CODEX_SUBAGENT_INDEX_LOCK:
        codex_states = [dict(state) for state in CODEX_SUBAGENT_STATE.values()]
    for state in codex_states:
        if str(state.get("session_id") or "") != session_id:
            continue
        key = f"{str(state.get('backend') or '')}:{state.get('subagent_id')}"
        merged[key] = dict(state)

    active_statuses = {"starting", "running"}
    retained = list(merged.values())
    retained.sort(key=lambda state: (
        0 if normalize_subagent_status(state.get("subagent_status")) in active_statuses else 1,
        -(durable_event_seq(state) or 0),
    ))
    # The desktop persists this array as timeline events. Reconciliation can
    # intentionally learn an old terminal child without appending a duplicate
    # event; that in-memory state has no durable event ID or sequence and must
    # not be advertised as an Event. Older responses that leaked those states
    # caused SQLite binding failures in desktop clients.
    persistable = [
        state
        for state in retained
        if durable_event_seq(state) is not None
        and isinstance(state.get("id"), str)
        and bool(str(state.get("id") or "").strip())
        and str(state.get("session_id") or "") == session_id
        and str(state.get("type") or "") == "subagent_state"
    ]
    selected = persistable[:limit]
    sequences = [seq for state in retained if (seq := durable_event_seq(state)) is not None]
    return {
        "session_id": session_id,
        "subagents": selected,
        "count": len(retained),
        "active_count": sum(
            normalize_subagent_status(state.get("subagent_status")) in active_statuses
            for state in retained
        ),
        "latest_seq": max(sequences, default=0),
    }


def codex_child_status_from_turn(turn: Any) -> str | None:
    if not isinstance(turn, dict):
        return None
    status = str(turn.get("status") or "").strip()
    if status == "inProgress":
        return "running"
    if status in {"completed", "interrupted", "failed"}:
        return normalize_subagent_status(status)
    return None


async def reconcile_codex_subagents(
    session_id: str,
    manager: CodexAppServerManager | None = None,
) -> dict[str, Any]:
    """Refresh durable child states from app-server's persisted spawn tree."""

    session = STORE.sessions.get(session_id) or {}
    root_thread_id = session_codex_thread_id(session)
    manager = manager if manager is not None else existing_codex_app_server_manager(session)
    list_descendants = getattr(manager, "list_descendant_threads", None)
    if not root_thread_id or manager is None or not callable(list_descendants):
        return {"reconciled": 0, "descendants": 0}
    durable_children = session.get("codex_subagents")
    expected_states = {
        child_id: dict(state)
        for child_id, state in (
            durable_children.items() if isinstance(durable_children, dict) else []
        )
        if isinstance(state, dict) and state.get("session_id") == session_id
    }
    with CODEX_SUBAGENT_INDEX_LOCK:
        expected_states.update({
            child_id: dict(state)
            for child_id, state in CODEX_SUBAGENT_STATE.items()
            if state.get("session_id") == session_id
        })
    try:
        descendants = await list_descendants(root_thread_id)
    except Exception as exc:
        logger.warning(
            "Codex subagent reconciliation failed session=%s thread=%s error=%s",
            session_id,
            root_thread_id,
            concise_error_message(exc),
        )
        return {"reconciled": 0, "descendants": 0, "error": concise_error_message(exc)}
    if not isinstance(descendants, list):
        return {"reconciled": 0, "descendants": 0}

    # A long-lived chat can have well over a thousand persisted descendants
    # (1,415 on 2026-09-04). Reconciling all of them on every chat open, and
    # emitting a fresh subagent_state event for each after a restart, appended
    # ~1,150 events in seconds and forced full timeline-index rebuilds. Only
    # the most recently updated children can still be active; older ones are
    # terminal and already have their terminal event.
    ordered = sorted(
        (thread for thread in descendants if isinstance(thread, dict)),
        key=lambda thread: str(
            thread.get("updatedAt") or thread.get("updated_at") or ""
        ),
        reverse=True,
    )
    skipped = max(0, len(ordered) - CODEX_SUBAGENT_RECONCILE_LIMIT)
    if skipped:
        logger.info(
            "Codex subagent reconciliation capped session=%s descendants=%d "
            "reconciled_limit=%d",
            session_id,
            len(ordered),
            CODEX_SUBAGENT_RECONCILE_LIMIT,
        )
    reconciled = 0
    silent = 0
    for thread in ordered[:CODEX_SUBAGENT_RECONCILE_LIMIT]:
        child_thread_id = str(thread.get("id") or "").strip()
        if not child_thread_id:
            continue
        previous = expected_states.get(child_thread_id) or {}
        status = codex_child_status_from_thread(thread.get("status"))
        turns = thread.get("turns") if isinstance(thread.get("turns"), list) else []
        latest_turn = turns[0] if turns else None
        status = codex_child_status_from_turn(latest_turn) or status
        thread_status_type = str(
            (thread.get("status") or {}).get("type")
            if isinstance(thread.get("status"), dict)
            else ""
        )
        if status is None and thread_status_type == "notLoaded":
            previous_status = normalize_subagent_status(previous.get("subagent_status"))
            status = previous_status if previous_status not in {"starting", "running"} else "completed"
        if status is None:
            # Unknown states must not manufacture an immortal active card.
            status = "completed"
        nickname, agent_path, source_parent_thread_id = codex_subagent_thread_identity(thread)
        # Active/status transitions are durable. The locked emitter also
        # persists a changed native identity for an already-durable terminal
        # child, without manufacturing another lifecycle transition. Unknown
        # historical terminal children remain memory-only.
        normalized_status = normalize_subagent_status(status)
        previous_status = (
            normalize_subagent_status(previous.get("subagent_status"))
            if previous
            else None
        )
        persist_event = normalized_status in {"starting", "running"} or (
            previous_status is not None and previous_status != normalized_status
        )
        await emit_codex_subagent_state(
            session_id,
            child_thread_id,
            status,
            parent_thread_id=source_parent_thread_id or root_thread_id,
            name=thread.get("preview"),
            title=thread.get("name", ...),
            nickname=nickname,
            agent_path=agent_path,
            activity=f"Subagent {status}",
            persist_event=persist_event,
            reconcile_expected_state=previous,
        )
        reconciled += 1
        if not persist_event:
            silent += 1
    return {
        "reconciled": reconciled,
        "descendants": len(descendants),
        "silent": silent,
        "skipped": skipped,
    }


CODEX_SUBAGENT_FINALIZE_TIMEOUT_SECONDS = float(
    hyadex_setting("CODEX_SUBAGENT_FINALIZE_TIMEOUT_SECONDS", "20")
)
CODEX_SUBAGENT_FINALIZE_TASKS: dict[str, asyncio.Task[Any]] = {}


def schedule_codex_subagent_finalization(
    session_id: str,
    manager: "CodexAppServerManager | None",
    *,
    run_id: str | None = None,
) -> None:
    """Run post-run child finalization detached, bounded, at most once per chat."""

    existing = CODEX_SUBAGENT_FINALIZE_TASKS.get(session_id)
    if existing is not None and not existing.done():
        return

    async def finalize() -> None:
        try:
            await asyncio.wait_for(
                finalize_codex_subagents_after_run(session_id, manager),
                timeout=max(1.0, CODEX_SUBAGENT_FINALIZE_TIMEOUT_SECONDS),
            )
        except asyncio.TimeoutError:
            logger.warning(
                "Codex subagent finalization timed out session=%s run=%s after %.0fs",
                session_id,
                run_id,
                CODEX_SUBAGENT_FINALIZE_TIMEOUT_SECONDS,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "Codex subagent finalization failed session=%s run=%s: %s",
                session_id,
                run_id,
                concise_error_message(exc),
            )

    task = asyncio.create_task(finalize())
    CODEX_SUBAGENT_FINALIZE_TASKS[session_id] = task

    def release(completed: asyncio.Task[Any]) -> None:
        if CODEX_SUBAGENT_FINALIZE_TASKS.get(session_id) is completed:
            CODEX_SUBAGENT_FINALIZE_TASKS.pop(session_id, None)

    task.add_done_callback(release)


async def finalize_codex_subagents_after_run(
    session_id: str,
    manager: CodexAppServerManager | None = None,
) -> dict[str, Any]:
    """Unload finished native children once the parent's run has completed.

    Codex keeps every spawned child rollout loaded in the app-server process
    after the parent turn finishes; nothing ever unsubscribed them, so a chat
    that forked many children kept all of their history resident. After the
    parent run, refresh child states from the persisted spawn tree, then
    unsubscribe every child this chat owns whose latest state is terminal and
    that the manager still reports loaded. Children that are still starting or
    running are left alone. Never raises.
    """

    summary: dict[str, Any] = {"reconciled": 0, "unloaded": [], "active": []}
    manager = manager if manager is not None else existing_codex_app_server_manager(
        STORE.sessions.get(session_id),
    )
    if manager is None:
        return summary
    try:
        reconciled = await reconcile_codex_subagents(session_id, manager)
    except Exception as exc:
        logger.warning(
            "Codex subagent post-run reconciliation failed session=%s error=%s",
            session_id,
            concise_error_message(exc),
        )
        reconciled = {}
    summary["reconciled"] = (
        int(reconciled.get("reconciled") or 0) if isinstance(reconciled, dict) else 0
    )
    is_loaded = getattr(manager, "is_thread_loaded", None)
    unsubscribe = getattr(manager, "unsubscribe_thread", None)
    if not callable(is_loaded) or not callable(unsubscribe):
        return summary
    root_thread_id = session_codex_thread_id(STORE.sessions.get(session_id) or {})
    with CODEX_SUBAGENT_INDEX_LOCK:
        children = [
            (child_thread_id, dict(CODEX_SUBAGENT_STATE.get(child_thread_id) or {}))
            for child_thread_id, owner in CODEX_SUBAGENT_SESSION_INDEX.items()
            if owner == session_id
        ]
    for child_thread_id, state in children:
        if not child_thread_id or child_thread_id == root_thread_id:
            continue
        if CODEX_THREAD_SESSION_INDEX.get(child_thread_id):
            # A chat's own provider thread is never a disposable child, even
            # when the spawn tree lists it (an adopted fork, for example).
            continue
        if not state:
            # Indexed without a known state: nothing authoritative says it is
            # finished, so leave it loaded rather than guess.
            continue
        status = normalize_subagent_status(state.get("subagent_status"))
        if status in {"starting", "running"}:
            summary["active"].append(child_thread_id)
            continue
        try:
            loaded = is_loaded(child_thread_id)
            if inspect.isawaitable(loaded):
                loaded = await loaded
        except Exception:
            loaded = False
        if not loaded:
            continue
        with suppress(Exception):
            await unsubscribe(child_thread_id)
        summary["unloaded"].append(child_thread_id)
    if summary["unloaded"]:
        logger.info(
            "unloaded %d finished Codex subagent thread(s) session=%s active=%d",
            len(summary["unloaded"]),
            session_id,
            len(summary["active"]),
        )
    return summary


async def stop_codex_descendant_subagents(
    session_id: str,
    root_thread_id: str,
    *,
    manager: CodexAppServerManager | None = None,
    target_thread_ids: set[str] | None = None,
) -> dict[str, Any]:
    """Interrupt active descendant turns without touching the parent turn."""

    manager = manager if manager is not None else existing_codex_app_server_manager(
        STORE.sessions.get(session_id),
    )
    list_descendants = getattr(manager, "list_descendant_threads", None)
    list_turns = getattr(manager, "list_turns", None)
    interrupt_turn = getattr(manager, "interrupt_turn", None)
    if (
        manager is None
        or not root_thread_id
        or not callable(list_descendants)
        or not callable(list_turns)
        or not callable(interrupt_turn)
    ):
        return {
            "descendants": 0,
            "requested": [],
            "interrupted": [],
            "pending": [],
            "errors": [],
        }
    try:
        descendants = await list_descendants(root_thread_id)
    except Exception as exc:
        return {
            "descendants": 0,
            "requested": [],
            "interrupted": [],
            "pending": [],
            "errors": [concise_error_message(exc)],
        }

    requested: list[str] = []
    interrupted: list[str] = []
    errors: list[str] = []
    pending: dict[str, dict[str, Any]] = {}
    targets = {str(value) for value in target_thread_ids} if target_thread_ids else None
    for thread in descendants if isinstance(descendants, list) else []:
        if not isinstance(thread, dict):
            continue
        child_thread_id = str(thread.get("id") or "").strip()
        if not child_thread_id or (targets is not None and child_thread_id not in targets):
            continue
        with CODEX_SUBAGENT_INDEX_LOCK:
            CODEX_SUBAGENT_SESSION_INDEX[child_thread_id] = session_id
        try:
            turns = await list_turns(
                child_thread_id,
                limit=4,
                items_view="summary",
                sort_direction="desc",
            )
            active_turns = [
                turn for turn in turns
                if isinstance(turn, dict)
                and str(turn.get("status") or "") == "inProgress"
                and str(turn.get("id") or "").strip()
            ]
            for turn in active_turns:
                await interrupt_turn(child_thread_id, str(turn["id"]))
            if active_turns:
                requested.append(child_thread_id)
                pending[child_thread_id] = thread
                with CODEX_SUBAGENT_INDEX_LOCK:
                    previous_status_value = (
                        CODEX_SUBAGENT_STATE.get(child_thread_id) or {}
                    ).get("subagent_status")
                previous_status = normalize_subagent_status(previous_status_value)
                await emit_codex_subagent_state(
                    session_id,
                    child_thread_id,
                    previous_status if previous_status in {"starting", "running"} else "running",
                    parent_thread_id=str(thread.get("parentThreadId") or root_thread_id),
                    name=thread.get("preview"),
                    activity="Stopping subagent…",
                )
        except Exception as exc:
            errors.append(f"{child_thread_id}: {concise_error_message(exc)}")

    deadline = asyncio.get_running_loop().time() + STOP_CONFIRM_TIMEOUT_SECONDS
    while pending and asyncio.get_running_loop().time() < deadline:
        for child_thread_id, thread in list(pending.items()):
            try:
                turns = await list_turns(
                    child_thread_id,
                    limit=1,
                    items_view="summary",
                    sort_direction="desc",
                )
            except Exception as exc:
                errors.append(f"{child_thread_id}: {concise_error_message(exc)}")
                pending.pop(child_thread_id, None)
                continue
            latest_turn = turns[0] if turns else None
            status = codex_child_status_from_turn(latest_turn)
            if status in {None, "running"}:
                continue
            await emit_codex_subagent_state(
                session_id,
                child_thread_id,
                status,
                parent_thread_id=str(thread.get("parentThreadId") or root_thread_id),
                name=thread.get("preview"),
                activity=f"Subagent {status}",
            )
            if status == "stopped":
                interrupted.append(child_thread_id)
            pending.pop(child_thread_id, None)
        if pending:
            await asyncio.sleep(0.05)
    return {
        "descendants": len(descendants) if isinstance(descendants, list) else 0,
        "requested": requested,
        "interrupted": interrupted,
        "pending": list(pending),
        "errors": errors,
    }


def claude_subagent_child_activity(raw: dict[str, Any]) -> str:
    """Return a safe, compact activity label without exposing child output."""
    if raw.get("type") != "assistant":
        return ""
    message = raw.get("message") if isinstance(raw.get("message"), dict) else {}
    content = message.get("content") if isinstance(message.get("content"), list) else []
    for block in content:
        if not isinstance(block, dict) or block.get("type") not in {"tool_use", "server_tool_use"}:
            continue
        tool_input = block.get("input") if isinstance(block.get("input"), dict) else {}
        description = compact_subagent_text(tool_input.get("description"))
        if description:
            return description
        tool_name = compact_subagent_text(block.get("name")) or "tool"
        return f"Using {tool_name}"
    return ""


def claude_subagent_progress_activity(*records: dict[str, Any]) -> str:
    for record in records:
        progress = record.get("progress")
        if isinstance(progress, dict):
            for key in ("summary", "description", "message"):
                text = compact_subagent_text(progress.get(key))
                if text:
                    return text
        elif progress is not None:
            text = compact_subagent_text(progress)
            if text:
                return text
        for key in ("summary", "description", "message"):
            text = compact_subagent_text(record.get(key))
            if text:
                return text
    for record in records:
        tool_name = compact_subagent_text(record.get("last_tool_name"))
        if tool_name:
            return f"Using {tool_name}"
    return ""


def build_claude_subagent_snapshot(session_id: str, limit: int = 64) -> dict[str, Any]:
    """Fold Claude's provider events into a bounded, durable subagent snapshot.

    Only explicitly selected lifecycle fields are projected. Provider prompts,
    tool-result output, commands, output files, and the raw event are never
    copied into the response.
    """
    limit = max(1, min(int(limit or 64), SUBAGENT_SNAPSHOT_STATE_LIMIT))
    states: OrderedDict[str, dict[str, Any]] = OrderedDict()
    task_keys: dict[str, str] = {}
    tool_keys: dict[str, str] = {}
    latest_seq = 0

    def scoped(run_id: str, provider_id: str) -> str:
        return f"{run_id}\x00{provider_id}"

    def forget(state_key: str) -> None:
        states.pop(state_key, None)
        for index in (task_keys, tool_keys):
            for provider_key, candidate in list(index.items()):
                if candidate == state_key:
                    index.pop(provider_key, None)

    def prune() -> None:
        while len(states) > SUBAGENT_SNAPSHOT_STATE_LIMIT:
            terminal_key = next((
                key for key, state in states.items()
                if state["status"] not in {"starting", "running"}
            ), None)
            forget(terminal_key or next(iter(states)))

    def find(run_id: str, task_id: str = "", tool_id: str = "") -> tuple[str | None, dict[str, Any] | None]:
        state_key = task_keys.get(scoped(run_id, task_id)) if task_id else None
        if state_key is None and tool_id:
            state_key = tool_keys.get(scoped(run_id, tool_id))
        return state_key, states.get(state_key) if state_key else None

    def merge_states(primary_key: str, secondary_key: str) -> dict[str, Any]:
        primary = states[primary_key]
        secondary = states[secondary_key]
        if primary["name"] == "Claude subagent" and secondary["name"] != "Claude subagent":
            primary["name"] = secondary["name"]
        if primary["kind"] == "agent" and secondary["kind"] != "agent":
            primary["kind"] = secondary["kind"]
        started = [value for value in (primary["started_at"], secondary["started_at"]) if value]
        if started:
            primary["started_at"] = min(started)
        if int(secondary["seq"]) > int(primary["seq"]):
            primary["updated_at"] = secondary["updated_at"]
            primary["activity"] = secondary["activity"] or primary["activity"]
            primary["summary"] = secondary["summary"] or primary["summary"]
            primary["status"] = secondary["status"]
        primary["seq"] = max(int(primary["seq"]), int(secondary["seq"]))
        primary["background"] = bool(primary["background"] or secondary["background"])
        primary["saw_task_started"] = bool(primary["saw_task_started"] or secondary["saw_task_started"])
        combined_log = sorted(
            [*primary["log"], *secondary["log"]],
            key=lambda entry: str(entry.get("ts") or ""),
        )
        primary["log"] = []
        for entry in combined_log:
            if primary["log"] and primary["log"][-1]["text"] == entry["text"]:
                continue
            primary["log"].append(entry)
        primary["log"] = primary["log"][-SUBAGENT_SNAPSHOT_LOG_LIMIT:]
        forget(secondary_key)
        states.move_to_end(primary_key)
        return primary

    def ensure(
        event: dict[str, Any],
        *,
        task_id: str = "",
        tool_id: str = "",
        name: Any = "",
        kind: Any = "",
        status: str = "running",
        background: bool | None = None,
    ) -> dict[str, Any]:
        run_id = str(event.get("run_id") or "")
        task_state_key = task_keys.get(scoped(run_id, task_id)) if task_id else None
        tool_state_key = tool_keys.get(scoped(run_id, tool_id)) if tool_id else None
        if task_state_key and tool_state_key and task_state_key != tool_state_key:
            merge_states(task_state_key, tool_state_key)
        state_key, state = find(run_id, task_id, tool_id)
        if state is None:
            provider_id = task_id or tool_id
            state_key = f"{run_id}\x00{provider_id}\x00{event.get('seq', 0)}"
            state = {
                "task_id": task_id or tool_id,
                "tool_id": tool_id,
                "run_id": run_id,
                "name": compact_subagent_text(name) or "Claude subagent",
                "kind": compact_subagent_text(kind) or "agent",
                "status": normalize_subagent_status(status),
                "started_at": str(event.get("ts") or ""),
                "updated_at": str(event.get("ts") or ""),
                "seq": int(event.get("seq") or 0),
                "summary": "",
                "activity": "",
                "log": [],
                "background": bool(background),
                "saw_task_started": False,
            }
            states[state_key] = state
        if task_id:
            state["task_id"] = task_id
            task_keys[scoped(run_id, task_id)] = state_key
        if tool_id:
            state["tool_id"] = tool_id
            tool_keys[scoped(run_id, tool_id)] = state_key
        clean_name = compact_subagent_text(name)
        clean_kind = compact_subagent_text(kind)
        if clean_name:
            state["name"] = clean_name
        if clean_kind:
            state["kind"] = clean_kind
        if background is not None:
            state["background"] = bool(background)
        state["status"] = normalize_subagent_status(status)
        state["updated_at"] = str(event.get("ts") or state["updated_at"])
        state["seq"] = max(int(state["seq"]), int(event.get("seq") or 0))
        states.move_to_end(state_key)
        prune()
        return state

    def note(state: dict[str, Any], event: dict[str, Any], value: Any) -> None:
        text = compact_subagent_text(value)
        state["updated_at"] = str(event.get("ts") or state["updated_at"])
        state["seq"] = max(int(state["seq"]), int(event.get("seq") or 0))
        if not text:
            return
        state["activity"] = text
        if not state["log"] or state["log"][-1]["text"] != text:
            state["log"].append({"ts": state["updated_at"], "text": text})
            del state["log"][:-SUBAGENT_SNAPSHOT_LOG_LIMIT]

    path = events_path(session_id)
    if path.exists():
        with path.open("r", encoding="utf-8", errors="ignore") as source:
            for line in source:
                if not line.strip():
                    continue
                try:
                    event = json.loads(line)
                except Exception:
                    continue
                seq = int(event.get("seq") or 0)
                latest_seq = max(latest_seq, seq)
                event_type = str(event.get("type") or "")
                run_id = str(event.get("run_id") or "")

                if event_type == "claude_background_tasks_reconciled" and event.get("backend") == BACKEND_CLAUDE and event.get("imported") is not True:
                    for receipt in normalize_task_receipts(
                        event.get("tasks"), run_id=run_id,
                        provider_session_id=event.get("provider_session_id"),
                    ):
                        _, previous = find(run_id, receipt["task_id"], receipt.get("tool_use_id", ""))
                        state = ensure(
                            event, task_id=receipt["task_id"],
                            tool_id=receipt.get("tool_use_id", ""),
                            name="" if previous is not None else "Claude background task",
                            kind="" if previous is not None else receipt["task_type"],
                            status=receipt["status"], background=True,
                        )
                        note(state, event, (
                            "Background task tracking lost; completion is not confirmed"
                            if receipt["status"] == "tracking_lost"
                            else f"Background task {state['status']}"
                        ))
                    continue

                if event_type == "tool_started":
                    tool = event.get("tool") if isinstance(event.get("tool"), dict) else {}
                    if str(tool.get("name") or "").strip().lower() != "agent":
                        continue
                    tool_id = str(tool.get("id") or "")
                    tool_input = tool.get("input") if isinstance(tool.get("input"), dict) else {}
                    state = ensure(
                        event,
                        tool_id=tool_id,
                        name=tool_input.get("description") or tool_input.get("subagent_type"),
                        kind=tool_input.get("subagent_type"),
                        status="starting",
                        background=tool_input.get("run_in_background") is True,
                    )
                    note(state, event, f"Starting {state['name']}")
                    continue

                if event_type == "tool_finished":
                    tool = event.get("tool") if isinstance(event.get("tool"), dict) else {}
                    tool_id = str(event.get("tool_id") or tool.get("id") or "")
                    _, state = find(run_id, tool_id=tool_id)
                    if state is None:
                        continue
                    if event.get("is_error") is True:
                        state["status"] = "failed"
                        note(state, event, "Subagent failed")
                    elif state["background"] or state["saw_task_started"]:
                        if state["status"] == "starting":
                            state["status"] = "running"
                        note(state, event, state["activity"])
                    elif state["status"] in {"starting", "running"}:
                        state["status"] = "completed"
                        note(state, event, "Subagent completed")
                    continue

                if event_type == "raw_event" and event.get("backend") == BACKEND_CLAUDE:
                    try:
                        raw = json.loads(str(event.get("raw") or ""))
                    except Exception:
                        continue
                    if not isinstance(raw, dict):
                        continue
                    subtype = str(raw.get("subtype") or "")
                    task_id = str(raw.get("task_id") or "")
                    tool_id = str(raw.get("tool_use_id") or "")

                    if raw.get("type") == "user":
                        tool_result = raw.get("tool_use_result") if isinstance(raw.get("tool_use_result"), dict) else {}
                        result_status = str(tool_result.get("status") or "").strip().lower()
                        is_async = tool_result.get("isAsync") is True or result_status in {"async_launched", "running", "pending"}
                        if is_async:
                            message = raw.get("message") if isinstance(raw.get("message"), dict) else {}
                            content = message.get("content") if isinstance(message.get("content"), list) else []
                            result_tool_id = next((
                                str(block.get("tool_use_id") or "")
                                for block in content
                                if isinstance(block, dict) and block.get("type") == "tool_result" and block.get("tool_use_id")
                            ), "")
                            async_task_id = str(tool_result.get("agentId") or tool_result.get("task_id") or "")
                            _, state = find(run_id, async_task_id, result_tool_id)
                            if state is not None:
                                state = ensure(
                                    event,
                                    task_id=async_task_id,
                                    tool_id=result_tool_id,
                                    status="running",
                                    background=True,
                                )
                                note(state, event, "Subagent running")
                        continue

                    if raw.get("type") == "system" and subtype == "background_tasks_changed":
                        tasks = raw.get("tasks") if isinstance(raw.get("tasks"), list) else []
                        for task in tasks:
                            if not isinstance(task, dict) or task.get("task_type") != "local_agent":
                                continue
                            background_task_id = str(task.get("task_id") or "")
                            if not background_task_id:
                                continue
                            state = ensure(
                                event,
                                task_id=background_task_id,
                                name=task.get("description"),
                                status=normalize_subagent_status(task.get("status")),
                                background=True,
                            )
                            note(state, event, task.get("description") or f"Subagent {state['status']}")
                        continue

                    if raw.get("type") == "system" and subtype == "task_started" and raw.get("task_type") == "local_agent" and task_id:
                        state = ensure(
                            event,
                            task_id=task_id,
                            tool_id=tool_id,
                            name=raw.get("description") or raw.get("subagent_type"),
                            kind=raw.get("subagent_type"),
                            status="running",
                        )
                        state["saw_task_started"] = True
                        note(state, event, raw.get("description") or "Subagent started")
                        continue

                    if raw.get("type") == "system" and subtype == "task_progress" and task_id:
                        _, state = find(run_id, task_id, tool_id)
                        is_agent = state is not None or raw.get("task_type") == "local_agent" or bool(raw.get("subagent_type"))
                        if not is_agent:
                            continue
                        state = ensure(
                            event,
                            task_id=task_id,
                            tool_id=tool_id,
                            kind=raw.get("subagent_type"),
                            status="running",
                        )
                        state["status"] = "running"
                        note(state, event, claude_subagent_progress_activity(raw) or "Working")
                        continue

                    if raw.get("type") == "system" and subtype == "task_updated" and task_id:
                        patch = raw.get("patch") if isinstance(raw.get("patch"), dict) else {}
                        _, state = find(run_id, task_id, tool_id)
                        is_agent = state is not None or raw.get("task_type") == "local_agent" or bool(raw.get("subagent_type"))
                        if not is_agent:
                            continue
                        status_value = patch.get("status") or raw.get("status") or (state["status"] if state else "running")
                        state = ensure(
                            event,
                            task_id=task_id,
                            tool_id=tool_id,
                            kind=raw.get("subagent_type"),
                            status=normalize_subagent_status(status_value),
                            background=True if patch.get("is_backgrounded") is True else None,
                        )
                        state["status"] = normalize_subagent_status(status_value)
                        summary = compact_subagent_text(patch.get("summary") or raw.get("summary"))
                        if summary:
                            state["summary"] = summary
                        activity = claude_subagent_progress_activity(patch, raw)
                        note(state, event, activity or f"Subagent {state['status']}")
                        continue

                    if raw.get("type") == "system" and subtype == "task_notification" and task_id:
                        _, state = find(run_id, task_id, tool_id)
                        is_agent = state is not None or raw.get("task_type") == "local_agent" or bool(raw.get("subagent_type"))
                        if not is_agent:
                            continue
                        state = ensure(
                            event,
                            task_id=task_id,
                            tool_id=tool_id,
                            kind=raw.get("subagent_type"),
                            status=normalize_subagent_status(raw.get("status")),
                        )
                        state["status"] = normalize_subagent_status(raw.get("status"))
                        state["summary"] = compact_subagent_text(raw.get("summary"))
                        note(state, event, state["summary"] or f"Subagent {state['status']}")
                        continue

                    parent_tool_id = str(raw.get("parent_tool_use_id") or "")
                    if parent_tool_id:
                        _, state = find(run_id, tool_id=parent_tool_id)
                        activity = claude_subagent_child_activity(raw)
                        if state is not None and activity:
                            note(state, event, activity)
                    continue

                if event_type in {"turn_finished", "turn_stopped", "error"}:
                    if event_type == "turn_stopped":
                        terminal_status, activity = "stopped", "Parent turn stopped"
                    elif event_type == "error":
                        terminal_status, activity = "failed", "Parent turn ended with an error"
                    else:
                        exit_code = event.get("exit_code")
                        terminal_status = "failed" if exit_code not in (None, 0) else "completed"
                        activity = "Parent turn ended with an error" if terminal_status == "failed" else "Parent turn completed"
                    for state in list(states.values()):
                        if state["run_id"] != run_id or state["status"] not in {"starting", "running"}:
                            continue
                        if event_type == "turn_finished" and state["background"]:
                            continue
                        state["status"] = terminal_status
                        note(state, event, activity)
                    continue

                if event_type == "claude_subagents_stopped":
                    target_run_ids = {
                        str(value or "").strip()
                        for value in event.get("run_ids") or []
                        if str(value or "").strip()
                    }
                    target_task_ids = {
                        str(value or "").strip()
                        for value in event.get("subagent_ids") or []
                        if str(value or "").strip()
                    }
                    stop_all = event.get("all") is True
                    for state in list(states.values()):
                        if state["status"] not in {"starting", "running"}:
                            continue
                        if not stop_all and (
                            state["run_id"] not in target_run_ids
                            and state["task_id"] not in target_task_ids
                        ):
                            continue
                        state["status"] = "stopped"
                        note(state, event, "Stopped with parent chat")

    active_statuses = {"starting", "running"}
    retained = list(states.values())
    retained.sort(key=lambda state: (
        0 if state["status"] in active_statuses else 1,
        -int(state["seq"]),
    ))
    selected = retained[:limit]
    projected = [{
        "seq": int(state["seq"]),
        "id": f"subagent:{session_id}:{state['run_id']}:{state['task_id']}",
        "session_id": session_id,
        "run_id": state["run_id"],
        "backend": BACKEND_CLAUDE,
        "type": "subagent_state",
        "ts": state["updated_at"],
        "subagent_id": state["task_id"],
        "subagent_tool_id": state["tool_id"] or None,
        "subagent_name": state["name"],
        "subagent_kind": state["kind"],
        "subagent_status": state["status"],
        "subagent_activity": state["activity"] or None,
        "subagent_summary": state["summary"] or None,
        "subagent_started_at": state["started_at"],
        "subagent_log": list(state["log"]),
    } for state in selected]
    return {
        "session_id": session_id,
        "subagents": projected,
        "count": len(retained),
        "active_count": sum(state["status"] in active_statuses for state in retained),
        "latest_seq": latest_seq,
    }


def is_codex_subagent_notification_user_item(item: dict[str, Any], text: str) -> bool:
    return codex_runtime_user_item_kind(item, text) == "subagent_notification"


def codex_collaboration_thread_ids(item: dict[str, Any]) -> list[str]:
    """Normalize legacy/current collaboration item receiver shapes."""

    values = list(item.get("receiverThreadIds") or [])
    for key in ("receiverThreadId", "newThreadId"):
        if item.get(key):
            values.append(item.get(key))
    return list(dict.fromkeys(
        str(value).strip() for value in values if str(value or "").strip()
    ))


def codex_collaboration_states(item: dict[str, Any]) -> dict[str, dict[str, Any]]:
    states: dict[str, dict[str, Any]] = {}
    raw_states = item.get("agentsStates")
    if isinstance(raw_states, dict):
        for thread_id, state in raw_states.items():
            clean_id = str(thread_id or "").strip()
            if not clean_id:
                continue
            states[clean_id] = dict(state) if isinstance(state, dict) else {"status": state}
    singular_status = item.get("agentStatus")
    for thread_id in codex_collaboration_thread_ids(item):
        if thread_id not in states and singular_status is not None:
            states[thread_id] = (
                dict(singular_status)
                if isinstance(singular_status, dict)
                else {"status": singular_status}
            )
    return states


async def project_codex_subagent_item(
    session_id: str,
    parent_thread_id: str,
    item: dict[str, Any],
    *,
    run_id: str | None,
    completed: bool,
) -> None:
    item_type = str(item.get("type") or "")
    if item_type == "subAgentActivity":
        child_thread_id = str(item.get("agentThreadId") or "").strip()
        kind = str(item.get("kind") or "").strip().lower()
        status = "stopped" if kind == "interrupted" else "running"
        activity = {
            "started": "Subagent started",
            "interacted": "Subagent activity",
            "interrupted": "Subagent stopped",
        }.get(kind, "Subagent activity")
        await emit_codex_subagent_state(
            session_id,
            child_thread_id,
            status,
            parent_thread_id=parent_thread_id,
            run_id=run_id,
            tool_id=str(item.get("id") or "") or None,
            name=item.get("agentPath"),
            nickname=item.get("agentNickname"),
            agent_path=item.get("agentPath"),
            activity=activity,
        )
        return
    if item_type not in {"collabAgentToolCall", "collabToolCall"}:
        return

    operation = re.sub(r"[^a-z0-9]", "", str(item.get("tool") or "").lower())
    states = codex_collaboration_states(item)
    receiver_ids = codex_collaboration_thread_ids(item)
    child_ids = list(dict.fromkeys([*receiver_ids, *states.keys()]))
    item_failed = str(item.get("status") or "").lower() in {
        "failed", "declined", "cancelled", "canceled", "errored",
    }
    for child_thread_id in child_ids:
        state = states.get(child_thread_id) or {}
        state_status = state.get("status")
        if state_status is None:
            if item_failed:
                state_status = "failed"
            elif operation == "spawnagent":
                state_status = "running" if completed else "starting"
            elif operation in {"interruptagent", "closeagent"} and completed:
                state_status = "stopped"
            else:
                state_status = (
                    (CODEX_SUBAGENT_STATE.get(child_thread_id) or {}).get("subagent_status")
                    or "running"
                )
        activity = compact_subagent_text(state.get("message"))
        if not activity:
            if operation == "spawnagent":
                activity = "Subagent attached" if completed else "Starting subagent"
            elif operation == "interruptagent":
                activity = "Subagent stopped" if completed else "Stopping subagent"
            elif operation == "closeagent":
                activity = "Subagent closed" if completed else "Closing subagent"
            elif operation in {"sendinput", "sendmessage", "followuptask", "resumeagent"}:
                activity = "Subagent received follow-up"
            else:
                activity = "Subagent status updated"
        await emit_codex_subagent_state(
            session_id,
            child_thread_id,
            state_status,
            parent_thread_id=str(item.get("senderThreadId") or parent_thread_id),
            run_id=run_id,
            tool_id=str(item.get("id") or "") or None,
            nickname=state.get("agentNickname") or item.get("agentNickname"),
            agent_path=state.get("agentPath") or item.get("agentPath"),
            activity=activity,
            summary=state.get("message"),
        )


def codex_child_status_from_thread(status: Any) -> str | None:
    if not isinstance(status, dict):
        return None
    status_type = str(status.get("type") or "").strip()
    if status_type == "active":
        return "running"
    if status_type == "idle":
        return "completed"
    if status_type == "systemError":
        return "failed"
    return None


async def evict_claude_sdk_chat(
    session_id: str,
    *,
    force: bool,
    manager: ClaudeSDKSupervisorManager | None = None,
    ownership_token: str | None = None,
) -> bool:
    """Return when one chat is absent/closed, without touching other clients."""

    selected_manager = manager or PROVIDERS.claude_sdk_manager
    if selected_manager is None:
        return True
    try:
        evict_kwargs: dict[str, Any] = {"force": force}
        if ownership_token is not None:
            evict_kwargs["ownership_token"] = ownership_token
        await asyncio.wait_for(
            selected_manager.evict(session_id, **evict_kwargs),
            timeout=STOP_CONFIRM_TIMEOUT_SECONDS,
        )
        # ``False`` means there was no supervisor, which is already the
        # desired settled state.
        return True
    except asyncio.TimeoutError:
        logger.error(
            "Claude SDK chat eviction timed out session=%s force=%s",
            session_id,
            force,
        )
        return False
    except Exception as exc:
        logger.error(
            "Claude SDK chat eviction failed session=%s force=%s: %s",
            session_id,
            force,
            concise_error_message(exc),
        )
        return False


def empty_subagent_stop_result() -> dict[str, Any]:
    return {
        "descendants": 0,
        "requested": [],
        "interrupted": [],
        "pending": [],
        "errors": [],
        "fence_committed": True,
    }


def claude_stop_fence_attempt_lock(session_id: str) -> asyncio.Lock:
    lock = CLAUDE_STOP_FENCE_ATTEMPT_LOCKS.get(session_id)
    if lock is None:
        lock = asyncio.Lock()
        CLAUDE_STOP_FENCE_ATTEMPT_LOCKS[session_id] = lock
    return lock


def discard_unused_claude_stop_fence_attempt_lock(session_id: str) -> None:
    if CLAUDE_STOP_FENCE_ATTEMPT_TASKS.get(session_id):
        return
    lock = CLAUDE_STOP_FENCE_ATTEMPT_LOCKS.get(session_id)
    if lock is not None and not lock.locked():
        CLAUDE_STOP_FENCE_ATTEMPT_LOCKS.pop(session_id, None)


async def record_codex_subagent_limit_application(
    manager: CodexAppServerManager, session_id: str, requested: dict[str, Any],
) -> None:
    """Track only this setting's application; never retire a shared process."""
    if not any(key in requested for key in (
        "_codex_subagent_limit_applied", "_codex_subagent_limit_reset_pending",
    )) and not _codex_config_positive_int(requested.get("subagent_limit")):
        return
    manager_identity = getattr(manager, "_subagent_limit_instance", None)
    if manager_identity is None:
        manager_identity = uuid.uuid4().hex
        manager._subagent_limit_instance = manager_identity
    process = [SERVER_INSTANCE_ID, manager_identity, manager.generation]
    inherited = codex_effective_thread_config({**requested, "subagent_limit": None}).get("agents", {}).get("max_concurrent_threads_per_session")
    async with STORE.transaction():
        current = STORE.sessions.get(session_id)
        if current is None:
            return
        before = (current.get("_codex_subagent_limit_applied"), current.get("_codex_subagent_limit_reset_pending"))
        pending = current.get("_codex_subagent_limit_reset_pending")
        applied = current.get("_codex_subagent_limit_applied") or {}
        if not _codex_config_positive_int(requested.get("subagent_limit")) and (
            applied.get("process") != process or _codex_config_positive_int(inherited)
        ):
            current.pop("_codex_subagent_limit_applied", None)
        if pending and (pending != process or _codex_config_positive_int(inherited)):
            current.pop("_codex_subagent_limit_reset_pending", None)
            current.pop("_codex_subagent_limit_applied", None)
        limit = requested.get("subagent_limit")
        if _codex_config_positive_int(limit):
            current["_codex_subagent_limit_applied"] = {"process": process, "limit": limit}
            # Saving null while start/resume awaited cannot change the captured
            # native request. Preserve that pending reset instead of claiming it applied.
            if current.get("subagent_limit") is None and not _codex_config_positive_int(inherited):
                current["_codex_subagent_limit_reset_pending"] = process
        after = (current.get("_codex_subagent_limit_applied"), current.get("_codex_subagent_limit_reset_pending"))
        if before != after:
            await STORE.save()


def claude_sdk_field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


def claude_sdk_type(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("type") or "")
    return type(value).__name__


def claude_sdk_subagent_lifecycle_raw(message: Any) -> dict[str, Any] | None:
    """Return the minimum legacy-shaped record needed by subagent folding.

    ``claude -p`` stores the provider's complete stream record as ``raw_event``.
    The typed SDK must not regress background-agent status, but it also should
    not persist prompts, child output, or arbitrary provider payloads merely to
    feed that fold.  Project only the lifecycle identifiers and short labels
    consumed by :func:`build_claude_subagent_snapshot`.
    """

    message_type = claude_sdk_type(message)
    if message_type == "UserMessage":
        result = claude_sdk_field(message, "tool_use_result")
        if not isinstance(result, dict):
            return None
        status = str(result.get("status") or "").strip().lower()
        is_async = result.get("isAsync") is True or status in {
            "async_launched", "running", "pending",
        }
        if not is_async:
            return None
        tool_results = []
        content = claude_sdk_field(message, "content", [])
        for block in content if isinstance(content, list) else []:
            if claude_sdk_type(block) not in {
                "ToolResultBlock", "ServerToolResultBlock",
                "tool_result", "server_tool_result", "advisor_tool_result",
            }:
                continue
            tool_id = str(claude_sdk_field(block, "tool_use_id") or "")[:512]
            if tool_id:
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tool_id,
                })
            if len(tool_results) >= 32:
                break
        return {
            "type": "user",
            "tool_use_result": {
                "status": status[:64],
                "isAsync": True,
                "agentId": str(result.get("agentId") or "")[:512],
                "task_id": str(result.get("task_id") or "")[:512],
            },
            "message": {"content": tool_results},
        }

    if message_type == "AssistantMessage":
        parent_tool_id = str(
            claude_sdk_field(message, "parent_tool_use_id") or ""
        )[:512]
        if not parent_tool_id:
            return None
        safe_blocks: list[dict[str, Any]] = []
        content = claude_sdk_field(message, "content", [])
        for block in content if isinstance(content, list) else []:
            if claude_sdk_type(block) not in {
                "ToolUseBlock", "ServerToolUseBlock",
                "tool_use", "server_tool_use",
            }:
                continue
            raw_input = claude_sdk_field(block, "input", {})
            raw_input = raw_input if isinstance(raw_input, dict) else {}
            safe_input = {
                key: compact_subagent_text(raw_input.get(key))
                for key in ("description", "subagent_type")
                if compact_subagent_text(raw_input.get(key))
            }
            safe_blocks.append({
                "type": "tool_use",
                "name": compact_subagent_text(
                    claude_sdk_field(block, "name") or "tool"
                ),
                "input": safe_input,
            })
            # The snapshot uses only the first child tool for its activity.
            break
        if not safe_blocks:
            return None
        return {
            "type": "assistant",
            "parent_tool_use_id": parent_tool_id,
            "message": {"content": safe_blocks},
        }

    data = claude_sdk_field(message, "data", {})
    if not isinstance(data, dict):
        return None
    subtype = str(
        data.get("subtype") or claude_sdk_field(message, "subtype") or ""
    )
    if subtype == "background_tasks_changed":
        tasks: list[dict[str, Any]] = []
        for task in data.get("tasks") if isinstance(data.get("tasks"), list) else []:
            if not isinstance(task, dict):
                continue
            tasks.append({
                "task_id": str(task.get("task_id") or "")[:512],
                "task_type": compact_subagent_text(task.get("task_type")),
                "description": compact_subagent_text(task.get("description")),
                "status": compact_subagent_text(task.get("status")),
            })
            if len(tasks) >= SUBAGENT_SNAPSHOT_STATE_LIMIT:
                break
        return {
            "type": "system",
            "subtype": subtype,
            "tasks": tasks,
        }
    if subtype not in {
        "task_started", "task_progress", "task_updated", "task_notification",
    }:
        return None

    raw_patch = data.get("patch") if isinstance(data.get("patch"), dict) else {}
    patch = {
        key: (
            value
            if isinstance(value, bool)
            else compact_subagent_text(value)
        )
        for key in (
            "status", "is_backgrounded", "summary", "description", "message",
        )
        if (value := raw_patch.get(key)) not in (None, "")
    }
    record: dict[str, Any] = {
        "type": "system",
        "subtype": subtype,
        "task_id": str(
            data.get("task_id") or claude_sdk_field(message, "task_id") or ""
        )[:512],
        "tool_use_id": str(
            data.get("tool_use_id")
            or claude_sdk_field(message, "tool_use_id")
            or ""
        )[:512],
        "task_type": compact_subagent_text(
            data.get("task_type") or claude_sdk_field(message, "task_type")
        ),
        "subagent_type": compact_subagent_text(data.get("subagent_type")),
        "description": compact_subagent_text(
            data.get("description") or claude_sdk_field(message, "description")
        ),
        "last_tool_name": compact_subagent_text(
            data.get("last_tool_name")
            or claude_sdk_field(message, "last_tool_name")
        ),
        "status": compact_subagent_text(
            data.get("status") or claude_sdk_field(message, "status")
        ),
        "summary": compact_subagent_text(
            data.get("summary") or claude_sdk_field(message, "summary")
        ),
    }
    if patch:
        record["patch"] = patch
    return record


def read_codex_admin_settings() -> dict[str, Any]:
    """Read the whole document before a leaf edit; never erase unreadable data."""
    try:
        value = json.loads(CODEX_SETTINGS_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        raise HTTPException(
            status_code=503, detail="Codex settings could not be read safely",
        ) from exc
    if not isinstance(value, dict):
        raise HTTPException(status_code=503, detail="Codex settings must be a JSON object")
    return value


def codex_subagents_admin_status() -> dict[str, Any]:
    settings = read_codex_admin_settings()
    config = settings.get("thread_config", {})
    if not isinstance(config, dict) or not isinstance(config.get("agents", {}), dict):
        raise HTTPException(status_code=503, detail="Codex thread settings must be JSON objects")
    config = sanitize_codex_thread_config(config, source=str(CODEX_SETTINGS_FILE))
    agents = config.get("agents") or {}
    return {
        "max_concurrent_threads_per_session": agents.get("max_concurrent_threads_per_session"),
        "configurable": CODEX_TRANSPORT != CODEX_TRANSPORT_EXEC,
        "reason": "unsupported_transport" if CODEX_TRANSPORT == CODEX_TRANSPORT_EXEC else None,
        "scope": "server",
        "provider_config_key": "agents.max_concurrent_threads_per_session",
        "applies_to": "new_or_reloaded_threads",
        "message": (
            "Applies to new or reloaded Codex threads. A chat-specific override takes "
            "precedence. No override means Codex chooses its default, not unlimited."
        ),
    }


async def get_codex_subagents_admin() -> dict[str, Any]:
    return codex_subagents_admin_status()


async def put_codex_subagents_admin(req: CodexSubagentsAdminRequest) -> dict[str, Any]:
    if CODEX_TRANSPORT == CODEX_TRANSPORT_EXEC:
        raise HTTPException(
            status_code=409, detail="Subagent configuration requires native Codex app-server transport",
        )
    # Share the goals writer lock: both controls own leaves of the same file.
    # There are no provider calls, unloads, or modifications of running work.
    async with CODEX_GOALS_CONFIG_LOCK:
        settings = read_codex_admin_settings()
        config = settings.get("thread_config", {})
        if not isinstance(config, dict) or not isinstance(config.get("agents", {}), dict):
            raise HTTPException(status_code=503, detail="Codex thread settings must be JSON objects")
        config = dict(config)
        agents = dict(config.get("agents", {}))
        agents.pop(CODEX_THREAD_CONFIG_LEGACY_MAX_THREADS_KEY, None)
        agents.pop("max_concurrent_threads_per_session", None)
        if req.max_concurrent_threads_per_session is not None:
            agents["max_concurrent_threads_per_session"] = req.max_concurrent_threads_per_session
        if agents:
            config["agents"] = agents
        else:
            config.pop("agents", None)
        if config:
            settings["thread_config"] = config
        else:
            settings.pop("thread_config", None)
        settings["updated_at"] = utc_timestamp()
        try:
            atomic_json(CODEX_SETTINGS_FILE, settings)
        except OSError as exc:
            raise HTTPException(status_code=503, detail="Codex settings could not be saved") from exc
        return codex_subagents_admin_status()


def codex_subagent_has_live_owner(
    thread_id: str,
    state: dict[str, Any],
) -> bool:
    """Distinguish a live child from a persisted pre-restart running card."""

    status = normalize_subagent_status(
        state.get("subagent_status") or state.get("status")
    )
    if status not in {"starting", "running"}:
        return False
    with CODEX_SUBAGENT_INDEX_LOCK:
        indexed_session_id = CODEX_SUBAGENT_SESSION_INDEX.get(thread_id)
        generation = CODEX_SUBAGENT_LIVE_GENERATIONS.get(thread_id)
        generation_manager = CODEX_SUBAGENT_LIVE_MANAGERS.get(thread_id)
    session_id = str(
        state.get("session_id") or indexed_session_id or ""
    ).strip()
    if session_id and session_id in RUNS.busy:
        owner = RUNS.active.get(session_id) or RUNS.current_turns.get(session_id) or {}
        state_run_id = str(state.get("run_id") or "").strip()
        owner_run_id = str(owner.get("run_id") or "").strip()
        if not state_run_id or not owner_run_id or state_run_id == owner_run_id:
            return True

    manager = existing_codex_app_server_manager(STORE.sessions.get(session_id))
    if manager is None and generation_manager is not None and any(
        current is generation_manager for current in codex_app_server_managers()
    ):
        manager = generation_manager
    if manager is None:
        return False
    with suppress(Exception):
        if manager.active_turn(thread_id) is not None:
            return True
    return bool(
        generation is not None
        and (generation_manager is manager
             or (generation_manager is None and manager is PROVIDERS.codex_manager))
        and getattr(manager, "ready", False) is True
        and getattr(manager, "generation", None) == generation
    )


CODEX_SUBAGENT_SCAN_LIMIT = SUBAGENT_SNAPSHOT_STATE_LIMIT
CODEX_SUBAGENT_SCAN_TIMEOUT_SECONDS = 5.0


CODEX_SUBAGENT_NATIVE_STATUS_CACHES: dict[
    int, tuple[Any, int | None, dict[tuple[Any, ...], tuple[str | None, int]]],
] = {}
CODEX_SUBAGENT_NATIVE_STATUS_CACHE_CLOCK = 0


def codex_subagent_native_turn_candidates(manager: Any = None) -> tuple[
    Any,
    int | None,
    tuple[tuple[Any, ...], ...],
]:
    """Return generation-owned child states that need native turn proof.

    ``CODEX_SUBAGENT_LIVE_GENERATIONS`` is deliberately conservative: a
    collaboration item observed in the current app-server generation marks a
    child as live even when that item was replayed after the child's turn had
    already stopped.  Busy owning chats and turns started through this server
    have direct runtime ownership; only the generation-only fallback needs an
    authoritative ``thread/turns/list`` check before restart/update admission.
    """

    manager = manager if manager is not None else PROVIDERS.codex_manager
    generation = (
        getattr(manager, "generation", None) if manager is not None else None
    )
    if (
        manager is None
        or getattr(manager, "ready", False) is not True
        or not isinstance(generation, int)
        or not callable(getattr(manager, "list_turns", None))
    ):
        return manager, generation, ()

    with CODEX_SUBAGENT_INDEX_LOCK:
        states = [
            (
                thread_id,
                dict(state),
                CODEX_SUBAGENT_SESSION_INDEX.get(thread_id),
                CODEX_SUBAGENT_LIVE_GENERATIONS.get(thread_id),
                CODEX_SUBAGENT_LIVE_MANAGERS.get(thread_id),
            )
            for thread_id, state in CODEX_SUBAGENT_STATE.items()
        ]

    candidates: list[tuple[Any, ...]] = []
    for thread_id, state, indexed_session_id, live_generation, generation_manager in states:
        status = normalize_subagent_status(
            state.get("subagent_status") or state.get("status")
        )
        if status not in {"starting", "running"}:
            continue
        session_id = str(
            state.get("session_id") or indexed_session_id or ""
        ).strip()
        owner_manager = existing_codex_app_server_manager(STORE.sessions.get(session_id))
        if owner_manager is None and generation_manager is manager:
            owner_manager = manager
        if owner_manager is not manager:
            continue
        state_run_id = str(state.get("run_id") or "").strip()
        if session_id and session_id in RUNS.busy:
            owner = RUNS.active.get(session_id) or RUNS.current_turns.get(session_id) or {}
            owner_run_id = str(owner.get("run_id") or "").strip()
            if (
                not state_run_id
                or not owner_run_id
                or state_run_id == owner_run_id
            ):
                continue
        direct_turn = None
        with suppress(Exception):
            direct_turn = manager.active_turn(thread_id)
        direct_turn_id = str(
            getattr(direct_turn, "turn_id", None) or ""
        ).strip()
        if direct_turn is None and (live_generation != generation or not (
            generation_manager is manager
            or (generation_manager is None and manager is PROVIDERS.codex_manager)
        )):
            continue
        if direct_turn is not None and not direct_turn_id:
            # A provisional locally-owned turn has not received an ID that can
            # be matched to native history yet. Its local handle remains the
            # stronger, conservative proof that work may have been accepted.
            continue
        candidates.append(
            (
                thread_id,
                session_id,
                state_run_id,
                status,
                live_generation,
                direct_turn_id,
                str(state.get("id") or ""),
                int(durable_event_seq(state) or 0),
                str(state.get("ts") or ""),
                id(generation_manager) if generation_manager is not None else 0,
            )
        )
    candidates.sort()
    return manager, generation, tuple(candidates)


def cached_codex_subagent_native_statuses(
    manager: Any,
    generation: int | None,
    candidates: tuple[tuple[Any, ...], ...],
) -> dict[tuple[Any, ...], tuple[str | None, int]]:
    """Return proofs for this exact process manager, generation, and state."""
    candidate_set = set(candidates)
    with CODEX_SUBAGENT_INDEX_LOCK:
        live_managers = {id(item): item for item in codex_app_server_managers()}
        for key, (owner, _generation, _cache) in tuple(CODEX_SUBAGENT_NATIVE_STATUS_CACHES.items()):
            if live_managers.get(key) is not owner:
                CODEX_SUBAGENT_NATIVE_STATUS_CACHES.pop(key, None)
        entry = CODEX_SUBAGENT_NATIVE_STATUS_CACHES.get(id(manager))
        if entry is None or entry[0] is not manager or entry[1] != generation:
            entry = (manager, generation, {})
            CODEX_SUBAGENT_NATIVE_STATUS_CACHES[id(manager)] = entry
        cache = entry[2]
        for candidate in tuple(cache):
            if candidate not in candidate_set:
                cache.pop(candidate, None)
        return dict(cache)


def cache_codex_subagent_native_statuses(
    manager: Any,
    generation: int | None,
    candidates: tuple[tuple[Any, ...], ...],
    results: list[tuple[tuple[Any, ...], str | None]],
) -> dict[tuple[Any, ...], tuple[str | None, int]]:
    """Commit one bounded batch without overwriting another manager's proof."""
    global CODEX_SUBAGENT_NATIVE_STATUS_CACHE_CLOCK
    cached_codex_subagent_native_statuses(manager, generation, candidates)
    candidate_set = set(candidates)
    with CODEX_SUBAGENT_INDEX_LOCK:
        owner, current_generation, cache = CODEX_SUBAGENT_NATIVE_STATUS_CACHES[id(manager)]
        if owner is manager and current_generation == generation:
            for candidate, status in results:
                if candidate in candidate_set:
                    CODEX_SUBAGENT_NATIVE_STATUS_CACHE_CLOCK += 1
                    cache[candidate] = (status, CODEX_SUBAGENT_NATIVE_STATUS_CACHE_CLOCK)
        return dict(cache)


def codex_subagent_native_turn_scopes() -> tuple[tuple[Any, ...], ...]:
    """Capture all provider processes without starting or reconnecting one."""
    managers = codex_app_server_managers()
    return tuple(codex_subagent_native_turn_candidates(manager) for manager in managers)


async def prepare_codex_subagent_terminal_snapshot(
    *,
    timeout_seconds: float | None = None,
) -> dict[str, Any]:
    """Inspect at most one shared batch across all normal/custom managers.

    Proofs remain scoped to manager identity, process generation, and child
    revision. One shared inspection clock lets every finite backlog progress
    without multiplying the admission work limit by the number of endpoints.
    """
    scopes = codex_subagent_native_turn_scopes()
    caches = [cached_codex_subagent_native_statuses(*scope) for scope in scopes]
    terminal_statuses = {"completed", "stopped", "failed"}
    pending = []
    for index, (_manager, _generation, candidates) in enumerate(scopes):
        for candidate in candidates:
            prior = caches[index].get(candidate)
            if prior is None or prior[0] not in terminal_statuses:
                pending.append((-1 if prior is None else prior[1], index, candidate))
    pending.sort()
    inspection_batch = pending[:CODEX_SUBAGENT_SCAN_LIMIT]

    async def latest_turn_status(index: int, candidate: tuple[Any, ...]):
        manager = scopes[index][0]
        try:
            turns = await manager.list_turns(
                candidate[0], limit=1, items_view="summary", sort_direction="desc",
            )
            latest = turns[0] if turns else None
            latest_id = str(latest.get("id") if isinstance(latest, dict) else "").strip()
            status = codex_child_status_from_turn(latest)
            if candidate[5] and latest_id != candidate[5]:
                status = None
            return index, candidate, status, True
        except Exception:
            return index, candidate, None, False

    error = None
    results = []
    if inspection_batch:
        try:
            timeout = (CODEX_SUBAGENT_SCAN_TIMEOUT_SECONDS if timeout_seconds is None
                       else max(0.0, float(timeout_seconds)))
            results = await asyncio.wait_for(asyncio.gather(*(
                latest_turn_status(index, candidate) for _, index, candidate in inspection_batch
            )), timeout=timeout)
            if any(not inspected for _, _, _, inspected in results):
                error = "scan_failed"
        except (TimeoutError, asyncio.TimeoutError):
            error = "timeout"
        except Exception:
            error = "scan_failed"

    current_scopes = codex_subagent_native_turn_scopes()
    consistent = (len(scopes) == len(current_scopes) and all(
        before[0] is after[0] and before[1:] == after[1:]
        for before, after in zip(scopes, current_scopes)
    ))
    if not consistent:
        error = "state_changed"
    elif results:
        for index, scope in enumerate(scopes):
            caches[index] = cache_codex_subagent_native_statuses(*scope, [
                (candidate, status) for result_index, candidate, status, _ in results
                if result_index == index
            ])
    snapshots = tuple({
        "manager": manager, "generation": generation, "candidates": candidates,
        "terminal_thread_ids": tuple(sorted(candidate[0] for candidate, (status, _) in caches[index].items()
            if consistent and status in terminal_statuses)),
    } for index, (manager, generation, candidates) in enumerate(scopes))
    return {
        # Retain the single-manager projection for existing native consumers.
        **(snapshots[0] if len(snapshots) == 1 else {
            "manager": None, "generation": None, "candidates": (),
        }),
        "scopes": snapshots,
        "terminal_thread_ids": tuple(sorted({thread for scope in snapshots
                                              for thread in scope["terminal_thread_ids"]})),
        "consistent": consistent, "error": error,
    }


def codex_session_has_live_subagents(session_id: str) -> bool:
    """Return whether this chat owns a child in the current app-server."""

    with CODEX_SUBAGENT_INDEX_LOCK:
        states = [
            (thread_id, dict(state))
            for thread_id, state in CODEX_SUBAGENT_STATE.items()
            if str(state.get("session_id") or "") == session_id
        ]
    return any(
        codex_subagent_has_live_owner(thread_id, state)
        for thread_id, state in states
    )
