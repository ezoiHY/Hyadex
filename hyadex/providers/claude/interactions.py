"""Claude tool-permission requests and their answers."""
from __future__ import annotations

import asyncio
from contextlib import suppress
from typing import Any
import uuid

from fastapi import HTTPException
from pydantic import BaseModel

from hyadex.core.config import (
    BACKEND_CLAUDE,
    CLAUDE_PERMISSION_MODES,
    CLAUDE_TRANSPORT_AGENT_SDK,
    MAX_CODEX_PENDING_INTERACTIONS,
)
from hyadex.core.paths import now_iso
from hyadex.core.runs import RUNS
from hyadex.core.session_settings import effective_claude_permission_mode
from hyadex.core.sessions import append_event, register_session_task, session_lifecycle_lock, STORE
from hyadex.core.state import DELETED_SESSION_TOMBSTONES, DELETING_SESSIONS
from hyadex.features.usage import bounded_codex_interaction_value
from hyadex.providers.claude.history import session_provider_id
from hyadex.providers.claude.sdk import claude_nondurable_scheduler_reason
from hyadex.providers.state import PROVIDERS


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


CLAUDE_PENDING_INTERACTIONS: dict[str, dict[str, Any]] = {}
CLAUDE_PENDING_INTERACTIONS_LOCK = asyncio.Lock()


CLAUDE_INTERACTION_HANDLER_TASKS: dict[str, set[asyncio.Task[Any]]] = {}


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
