"""Codex interactions: approvals and permission requests awaiting the user."""
from __future__ import annotations

import asyncio
from collections import OrderedDict
from contextlib import suppress
from typing import Any
import uuid

from fastapi import HTTPException
from pydantic import BaseModel

from hyadex.core.config import (
    CODEX_APPROVAL_POLICIES,
    CODEX_DEFAULT_APPROVAL_POLICY,
    CODEX_INTERACTION_METHODS,
    CODEX_SESSION_CLEANUP_TIMEOUT_SECONDS,
    MAX_CODEX_APPROVAL_ITEM_CACHE,
    MAX_CODEX_PENDING_INTERACTIONS,
)
from hyadex.core.paths import now_iso
from hyadex.core.runs import RUNS
from hyadex.core.sessions import (
    append_event,
    codex_app_server_managers,
    CODEX_RETIRED_APP_SERVER_MANAGERS,
    CODEX_SESSION_APP_SERVER_MANAGERS,
    register_session_task,
    session_lifecycle_lock,
    STORE,
)
from hyadex.core.state import DELETED_SESSION_TOMBSTONES, DELETING_SESSIONS
from hyadex.features.usage import bounded_codex_interaction_value
from hyadex.providers.claude.runtime import CLAUDE_INTERACTION_HANDLER_TASKS
from hyadex.providers.codex.app_server import CodexAppServerManager, decline_server_request
from hyadex.providers.codex.goals import (
    CODEX_QUARANTINED_GOAL_THREADS,
    reset_codex_ephemeral_runtime_metadata,
)
from hyadex.providers.codex.managers import (
    cancel_codex_native_actions,
    CODEX_APP_SERVER_EVICTING_THREADS,
    CODEX_APP_SERVER_MANAGER_LOCK,
    CODEX_APP_SERVER_THREAD_PIN_COUNTS,
    CODEX_INTERACTIVE_CONTROL_THREAD_COUNTS,
    CODEX_INTERACTIVE_CONTROL_THREADS,
    existing_codex_app_server_manager_for_thread,
)
from hyadex.providers.codex.notifications import codex_manager_owns_notification
from hyadex.providers.codex.threads import (
    CODEX_APP_SERVER_INVALIDATED_THREADS,
    CODEX_APP_SERVER_PINNED_THREADS,
    CODEX_APP_SERVER_THREAD_LRU,
    CODEX_APP_SERVER_THREAD_LRU_LOCK,
    codex_session_id_for_thread,
)
from hyadex.providers.state import PROVIDERS


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


class CodexInteractionResponseRequest(BaseModel):
    response: dict[str, Any]


CODEX_PENDING_INTERACTIONS: dict[str, dict[str, Any]] = {}
CODEX_PENDING_INTERACTIONS_LOCK = asyncio.Lock()
CODEX_APPROVAL_ITEM_CACHE: OrderedDict[
    tuple[str, str],
    dict[str, Any],
] = OrderedDict()
CODEX_INTERACTION_HANDLER_TASKS: dict[str, set[asyncio.Task[Any]]] = {}


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
