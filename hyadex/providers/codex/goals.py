"""Codex native goals: reconciliation, budgets, steering and pausing."""
from __future__ import annotations

import asyncio
from contextlib import suppress
import inspect
import json
import logging.handlers
from pathlib import Path
from typing import Any, Literal
import uuid

from fastapi import HTTPException
from pydantic import BaseModel, Field

from hyadex.core.config import (
    BACKEND_CODEX,
    CODEX_BIN,
    CODEX_GOAL_CONTROL_TIMEOUT_SECONDS,
    CODEX_GOAL_STATUSES,
    CODEX_GOALS_DEFAULT_ENABLED,
    CODEX_GOALS_ENABLED,
    CODEX_TRANSPORT_APP_SERVER,
    DEFAULT_BACKEND,
    MAX_CODEX_GOAL_CHARS,
)
from hyadex.core.paths import now_iso
from hyadex.core.queue import request_fields_set
from hyadex.core.history_sync import append_durable_event_batch, fence_native_steer_delivery
from hyadex.core.steering import codex_goal_steer_selection_is_plain
from hyadex.core.runs import RUNS
from hyadex.core.server_logging import concise_error_message
from hyadex.core.sessions import append_event, session_lifecycle_lock, STORE
from hyadex.core.session_settings import (
    codex_goal_time_budget_is_exhausted,
    codex_runtime_settings,
)
from hyadex.core.session_codex import (
    CODEX_SUBAGENT_INDEX_LOCK,
    CODEX_SUBAGENT_LIVE_GENERATIONS,
    CODEX_SUBAGENT_LIVE_MANAGERS,
    CODEX_THREAD_SESSION_INDEX,
    existing_codex_app_server_manager,
)
from hyadex.core.state import DELETED_SESSION_TOMBSTONES, DELETING_SESSIONS
from hyadex.core.util import compact_memory_text
from hyadex.features.side_questions import CODEX_GOALS_RECONFIGURING
from hyadex.features.usage import (
    broadcast_provider_runtime_changed,
    clear_provider_context_usage_locked,
)
from hyadex.providers.authority import (
    NativeSteerHandoffError,
    provider_capability_is_attached_to_live_run,
)
from hyadex.providers.claude.history import session_provider_id
from hyadex.providers.codex.app_server import (
    CodexAppServerError,
    CodexAppServerManager,
    CodexAppServerProtocolError,
    CodexAppServerRequestError,
)
from hyadex.providers.codex.forks import build_fork_memory
from hyadex.providers.codex.threads import (
    codex_thread_has_local_run_owner,
    codex_thread_instructions,
)
from hyadex.providers.registry import build_user_provider_prompt

logger = logging.getLogger("hyadex-server")


class CodexGoalsAdminRequest(BaseModel):
    enabled: bool


class CodexGoalRequest(BaseModel):
    objective: str | None = Field(default=None, max_length=MAX_CODEX_GOAL_CHARS)
    status: Literal[
        "active",
        "paused",
        "blocked",
        "usageLimited",
        "budgetLimited",
        "complete",
    ] | None = None
    token_budget: int | None = Field(default=None, ge=1)
    time_budget_seconds: int | None = Field(default=None, ge=1)


# A persisted thread goal belongs to the provider thread, while Hyadex's
# session cache survives provider-process restarts. Remember which app-server
# generation has been reconciled so a resumed thread is checked exactly once
# per generation instead of trusting a stale local goal indefinitely.
CODEX_GOAL_SYNC_GENERATIONS: dict[str, tuple[int, int, str]] = {}
# Threads detached after a failed Stop remain fenced for the lifetime of the
# provider process. Any late native goal continuation is interrupted instead
# of being adopted as a new Hyadex run.
CODEX_QUARANTINED_GOAL_THREADS: dict[str, str] = {}


async def reset_codex_ephemeral_runtime_metadata() -> None:
    """Clear process-owned Codex state after transport shutdown or restart."""
    CODEX_GOAL_SYNC_GENERATIONS.clear()
    CODEX_QUARANTINED_GOAL_THREADS.clear()
    with CODEX_SUBAGENT_INDEX_LOCK:
        CODEX_SUBAGENT_LIVE_GENERATIONS.clear()
        CODEX_SUBAGENT_LIVE_MANAGERS.clear()
    changed = False
    async with STORE.transaction():
        for session in STORE.sessions.values():
            if str(session.get("backend") or DEFAULT_BACKEND) != BACKEND_CODEX:
                continue
            reset_values = {
                "codex_thread_status": {"type": "notLoaded"},
                "codex_pending_interaction_count": 0,
                "codex_needs_user_action": False,
            }
            for key, value in reset_values.items():
                if session.get(key) != value:
                    session[key] = value
                    changed = True
            cached_goal = session.get("codex_goal")
            if (
                isinstance(cached_goal, dict)
                and str(cached_goal.get("status") or "") == "active"
            ):
                session["codex_goal"] = {
                    **cached_goal,
                    "status": "paused",
                }
                session["_codex_goal_reconciliation_required"] = {
                    "thread_id": str(session_provider_id(session) or "") or None,
                    "recorded_at": now_iso(),
                    "reason": "Codex app-server stopped without a live run owner.",
                }
                changed = True
            if "_codex_token_usage_terminal" in session:
                session.pop("_codex_token_usage_terminal", None)
                changed = True
        if changed:
            await STORE.save()


def codex_native_goal_owner_matches(session_id: str, run_id: str, thread_id: str, turn_id: str) -> bool:
    """Whether a native Codex goal-resume turn still owns this run.

    Native goal continuation runs without an app-server active-turn record, so
    provider tool calls are matched against the reserved native operation.
    """
    active = RUNS.active.get(session_id) or {}
    current = RUNS.current_turns.get(session_id) or {}
    reservation = str(active.get("codex_control_reservation_id") or "")
    goal = (STORE.sessions.get(session_id) or {}).get("codex_goal") or {}
    return bool(
        thread_id and turn_id and reservation
        and active.get("codex_native_operation") is True
        and active.get("codex_native_operation_kind") == "goal_resume"
        and str(current.get("codex_control_reservation_id") or "") == reservation
        and str(active.get("provider_thread_id") or "") == thread_id
        and str(active.get("provider_turn_id") or "") == turn_id
        and active.get("provider_turn_ready") is True
        and not active.get("codex_goal_handoff_closed")
        and goal.get("status") == "active"
        and provider_capability_is_attached_to_live_run(session_id, run_id)
    )


def codex_goals_cli_args() -> list[str]:
    """Apply the server-wide goal policy to every one-shot Codex process."""

    return [] if CODEX_GOALS_ENABLED else ["--disable", "goals"]


async def rollover_codex_provider_session(
    session_id: str,
    run_id: str,
    provider_id: str,
    reason: str,
    *,
    message: str,
    memory_seed_used: bool = True,
) -> tuple[dict[str, Any], str] | None:
    current = STORE.sessions.get(session_id)
    if not current:
        return None
    snapshot = dict(current)
    memory = build_fork_memory(snapshot, session_id, reason=reason, exclude_run_id=run_id)
    quarantined_goal_thread = False
    usage_signal: dict[str, Any] | None = None
    async with STORE.transaction():
        current = STORE.sessions.get(session_id)
        if not current or session_provider_id(current) != provider_id:
            return None
        current_goal = current.get("codex_goal")
        if (
            isinstance(current_goal, dict)
            and str(current_goal.get("status") or "") == "active"
        ):
            # A provider rollover abandons the native thread. Keep the user's
            # objective, but never claim it is actively running without a
            # local owner; the replacement thread will receive it paused.
            current["codex_goal"] = {**current_goal, "status": "paused"}
            quarantined_goal_thread = True
        current["session_id"] = None
        current["codex_thread_id"] = None
        current.pop("codex_instruction_hash", None)
        current.pop("codex_instruction_version", None)
        current.pop("codex_token_usage", None)
        current.pop("codex_token_usage_snapshot", None)
        current.pop("_codex_token_usage_checkpoint", None)
        current.pop("_codex_token_usage_terminal", None)
        usage_signal = clear_provider_context_usage_locked(
            current,
            BACKEND_CODEX,
            provider_session_id=None,
            state="cleared",
        )
        current["memory_seed"] = memory
        current["memory_seed_used"] = memory_seed_used
        current["memory_forked"] = True
        current["memory_fork_reason"] = compact_memory_text(reason, 2000)
        current["updated_at"] = now_iso()
        await STORE.save()
        fresh_session = dict(current)
    if CODEX_THREAD_SESSION_INDEX.get(provider_id) == session_id:
        CODEX_THREAD_SESSION_INDEX.pop(provider_id, None)
    CODEX_GOAL_SYNC_GENERATIONS.pop(session_id, None)
    if quarantined_goal_thread:
        CODEX_QUARANTINED_GOAL_THREADS[provider_id] = session_id
    if usage_signal is not None:
        await broadcast_provider_runtime_changed(session_id, usage_signal)
    await append_event(session_id, "provider_rollover", {
        "run_id": run_id,
        "backend": BACKEND_CODEX,
        "old_provider_session_id": provider_id,
        "message": message,
        "reason": compact_memory_text(reason, 1200),
    })
    return fresh_session, memory


async def send_codex_goal_steer(
    session_id: str, operation_id: str, manager: CodexAppServerManager,
    thread_id: str, reservation_id: str, subscription: Any,
    steer_queue: asyncio.Queue[dict[str, Any]], request: dict[str, Any],
) -> dict[str, Any]:
    """Steer one exact native goal turn without replacing its authority/owner."""
    selected = request["selected"]
    expected_turn_id = str(request.get("expected_provider_turn_id") or "")
    generation = manager.generation

    def delivery_owner_valid() -> bool:
        # Also called synchronously under the transport writer lock. There is
        # no await between that check and stdin.write: Stop/Pause cannot slip
        # into a wait-for-writer window after the last ownership check.
        active = RUNS.active.get(session_id) or {}
        current = RUNS.current_turns.get(session_id) or {}
        session = STORE.sessions.get(session_id) or {}
        goal = session.get("codex_goal")
        goal_identity = (
            str(goal.get("id") or ""), str(goal.get("objective") or ""),
        ) if isinstance(goal, dict) else None
        turn = active.get("codex_app_server_turn")
        same_owner_lane = (
            (
                bool(reservation_id)
                and active.get("codex_native_operation_kind") == "goal_resume"
                and active.get("native_steer_queue") is steer_queue
            ) or (
                not reservation_id
                and not active.get("codex_native_operation_kind")
                and not active.get("standalone_provider_context")
                and active.get("codex_goal_steer_queue") is steer_queue
                and turn is not None
                and not getattr(turn, "_closed", False)
                and not getattr(turn, "_completed", False)
                and getattr(turn, "_subscription", None) is subscription
                and str(getattr(turn, "turn_id", "")) == expected_turn_id
                and str(getattr(turn, "thread_id", "")) == thread_id
            )
        )
        return bool(
            CODEX_GOALS_ENABLED and manager.generation == generation
            and not getattr(subscription, "_closed", False)
            and session_id not in DELETING_SESSIONS
            and session_id not in DELETED_SESSION_TOMBSTONES
            and session_id not in RUNS.maintenance
            and session_id in RUNS.busy
            and session_id not in RUNS.stop_requests
            and operation_id not in RUNS.stopped_runs
            and isinstance(goal, dict) and goal.get("status") == "active"
            and goal_identity == request.get("goal_identity")
            and not codex_goal_time_budget_is_exhausted(session)
            and same_owner_lane
            and not active.get("stop_requested")
            and active.get("provider_turn_ready")
            and expected_turn_id
            and str(active.get("provider_turn_id") or "") == expected_turn_id
            and str(active.get("provider_thread_id") or "") == thread_id
            and str(active.get("run_id") or "") == operation_id
            and str(current.get("run_id") or "") == operation_id
            and str(active.get("codex_control_reservation_id") or "") == reservation_id
            and str(current.get("codex_control_reservation_id") or "") == reservation_id
            and codex_goal_steer_selection_is_plain(selected)
            # Only text/files are sent. The current turn's command, references,
            # runtime and authority stay intact, regardless of picker changes
            # or capability metadata on an older queued message.
        )

    async def validate_delivery_owner() -> None:
        async with RUNS.active_lock:
            allowed = delivery_owner_valid()
        if not allowed:
            raise NativeSteerHandoffError(
                "The goal, native turn, or control owner changed before delivery; the follow-up was not sent",
                safe_to_requeue=True,
            )

    try:
        await validate_delivery_owner()
        prompt = build_user_provider_prompt(
            session_id, str(selected.get("prompt") or ""), list(selected.get("file_ids") or []),
        )
        await fence_native_steer_delivery(session_id, selected, backend=BACKEND_CODEX)
        # Stop/Pause, budget/goal changes, and native turn rollover can win
        # during the durable fence. Check again at the RPC boundary.
        await validate_delivery_owner()
        request["_goal_steer_rpc_started"] = True
        acknowledged_turn_id, watermark = await manager.steer_turn_with_notification_watermark(
            thread_id, expected_turn_id,
            [{"type": "text", "text": prompt, "text_elements": []}],
            client_user_message_id=str(selected.get("queued_id") or ""),
            notification_subscription=subscription,
            before_send=delivery_owner_valid,
        )
        if acknowledged_turn_id != expected_turn_id or isinstance(watermark, bool) or not isinstance(watermark, int) or watermark < 0:
            raise CodexAppServerProtocolError(
                "Codex goal steer returned an invalid acknowledgement boundary",
                request_sent=True, safe_to_retry=False,
            )
        return {"request": request, "watermark": watermark, "provider_turn_id": expected_turn_id}
    except NativeSteerHandoffError:
        raise
    except BaseException as exc:
        safe = not request.get("_goal_steer_rpc_started") or isinstance(exc, CodexAppServerRequestError) or (
            isinstance(exc, CodexAppServerError)
            and getattr(exc, "request_sent", True) is False
            and getattr(exc, "safe_to_retry", False) is True
        )
        if safe:
            request["_goal_steer_rpc_started"] = False
        if isinstance(exc, asyncio.CancelledError):
            raise
        raise NativeSteerHandoffError(
            concise_error_message(exc), safe_to_requeue=bool(safe), delivery_uncertain=not safe,
        ) from exc


async def commit_codex_goal_steer(
    session_id: str, operation_id: str, thread_id: str,
    reservation_id: str, pending: dict[str, Any],
) -> dict[str, Any]:
    """Record accepted input without restarting, resuming, or rebinding a goal."""
    request = pending["request"]
    selected = request["selected"]
    async with RUNS.active_lock:
        active = RUNS.active.get(session_id) or {}
        current = RUNS.current_turns.get(session_id) or {}
        owned = (
            str(active.get("run_id") or "") == operation_id
            and str(current.get("run_id") or "") == operation_id
            and str(active.get("provider_thread_id") or "") == thread_id
            and str(active.get("codex_control_reservation_id") or "") == reservation_id
            and str(current.get("codex_control_reservation_id") or "") == reservation_id
        )
    if not owned:
        raise NativeSteerHandoffError(
            "The goal owner changed after delivery; the follow-up was not replayed",
            safe_to_requeue=False, delivery_uncertain=True,
        )
    display_prompt = str(selected.get("display_prompt") if selected.get("display_prompt") is not None else selected.get("prompt") or "")
    display_file_ids = list(
        selected.get("display_file_ids")
        if selected.get("display_file_ids") is not None
        else selected.get("file_ids") or []
    )
    await append_durable_event_batch(session_id, [
        ("turn_queue_run_now", {
            "queued_id": selected.get("queued_id"), "run_id": operation_id,
            "backend": BACKEND_CODEX, "prompt": display_prompt,
            "file_ids": display_file_ids,
            "native_steer": True, "native_goal_steer": True,
            "interrupted": False, "replays_interrupted_message": False,
            "remaining": request.get("remaining", 0), "superseded_queued_ids": [],
            "message": "Follow-up sent to the active Codex goal without pausing it.",
        }),
        ("turn_steered", {
            "run_id": operation_id, "backend": BACKEND_CODEX,
            "purpose": "codex_goal_resume", "provider_turn_id": pending["provider_turn_id"],
            "queued_id": selected.get("queued_id"), "prompt": display_prompt,
            "file_ids": display_file_ids, "native_steer": True, "native_goal_steer": True,
            "provider_user_authored": True,
        }),
    ])
    return {
        "ok": True, "queued_id": selected.get("queued_id"), "run_id": operation_id,
        "interrupted": False, "native_steer": True, "native_goal_steer": True,
        "replays_interrupted_message": False, "superseded_queued_ids": [],
    }


async def apply_codex_goal_time_budget_limit(
    session_id: str,
    run_id: str,
    manager: CodexAppServerManager,
    provider_id: str,
    turn: Any,
    time_budget_seconds: Any,
) -> None:
    """Persist and enforce an Hyadex time limit without trusting I/O."""
    async with STORE.transaction():
        current = STORE.sessions.get(session_id)
        if current:
            current["codex_goal_time_budget_exhausted"] = True
            current["updated_at"] = now_iso()
            await STORE.save()
    goal: dict[str, Any] | None = None
    with suppress(Exception):
        goal = await manager.set_thread_goal(
            provider_id,
            status="budgetLimited",
        )
    if isinstance(goal, dict):
        async with STORE.transaction():
            current = STORE.sessions.get(session_id)
            if current:
                current["codex_goal"] = goal
                current["updated_at"] = now_iso()
                await STORE.save()
    async with RUNS.active_lock:
        active = RUNS.active.get(session_id)
        if active:
            active["stop_requested"] = True
            RUNS.stopped_runs.add(str(active.get("run_id") or run_id))
    if turn is not None and turn.turn_id:
        with suppress(CodexAppServerError):
            await turn.interrupt()
    try:
        await append_event(
            session_id,
            "codex_goal_budget_limited",
            {
                "run_id": run_id,
                "time_budget_seconds": time_budget_seconds,
                "time_budget_owner": "Hyadex",
                "message": (
                    "The persistent goal reached its Hyadex time limit."
                ),
            },
        )
    except Exception as exc:
        logger.warning(
            "failed to persist Codex goal budget event session=%s: %s",
            session_id,
            concise_error_message(exc),
        )


def codex_goal_api_is_unsupported(exc: BaseException) -> bool:
    return isinstance(exc, CodexAppServerRequestError) and exc.code in {
        -32600,
        -32601,
    }


async def persist_reconciled_codex_goal(
    session_id: str,
    thread_id: str,
    goal: dict[str, Any] | None,
    *,
    allow_unbound_thread: bool = False,
) -> bool:
    """Persist provider-authoritative goal state without reordering the chat."""

    changed = False
    async with STORE.transaction():
        current = STORE.sessions.get(session_id)
        if current is None:
            return False
        current_thread_id = str(session_provider_id(current) or "").strip()
        if current_thread_id != thread_id and not (
            allow_unbound_thread and not current_thread_id
        ):
            return False
        if current.get("codex_goal") != goal:
            current["codex_goal"] = goal
            changed = True
        if goal is None:
            if current.get("codex_goal_time_budget_seconds") is not None:
                current["codex_goal_time_budget_seconds"] = None
                changed = True
            if current.get("codex_goal_time_budget_exhausted") is not False:
                current["codex_goal_time_budget_exhausted"] = False
                changed = True
        elif codex_goal_time_budget_is_exhausted(current):
            if current.get("codex_goal_time_budget_exhausted") is not True:
                current["codex_goal_time_budget_exhausted"] = True
                changed = True
        reconciliation = current.get("_codex_goal_reconciliation_required")
        if isinstance(reconciliation, dict) and (
            allow_unbound_thread
            or str(reconciliation.get("replacement_thread_id") or "")
            == thread_id
            or str(reconciliation.get("thread_id") or "") == thread_id
        ):
            current.pop("_codex_goal_reconciliation_required", None)
            changed = True
        if changed:
            await STORE.save()
    return True


def validate_inherited_codex_goal(
    expected_goal: dict[str, Any],
    native_goal: dict[str, Any] | None,
) -> None:
    """Require a native fork to preserve the parent goal faithfully.

    A forked active goal is deliberately paused before it is exposed because
    the new chat has no local run owner. All other representable fields must
    match the parent snapshot exactly; otherwise the caller must discard the
    native fork and use the bounded-memory fallback.
    """

    if not isinstance(native_goal, dict):
        raise CodexAppServerProtocolError(
            "the native fork did not preserve the inherited goal",
            request_sent=True,
            safe_to_retry=False,
        )

    expected_objective = str(expected_goal.get("objective") or "").strip()
    native_objective = str(native_goal.get("objective") or "").strip()
    if not expected_objective or native_objective != expected_objective:
        raise CodexAppServerProtocolError(
            "the native fork did not preserve the inherited goal objective",
            request_sent=True,
            safe_to_retry=False,
        )

    expected_token_budget = expected_goal.get("tokenBudget")
    if expected_token_budget is None and "tokenBudget" not in expected_goal:
        expected_token_budget = expected_goal.get("token_budget")
    if isinstance(expected_token_budget, bool) or (
        expected_token_budget is not None
        and not isinstance(expected_token_budget, int)
    ):
        raise CodexAppServerProtocolError(
            "the cached inherited goal has an invalid token budget",
            request_sent=False,
            safe_to_retry=False,
        )
    if native_goal.get("tokenBudget") != expected_token_budget:
        raise CodexAppServerProtocolError(
            "the native fork did not preserve the inherited goal token budget",
            request_sent=True,
            safe_to_retry=False,
        )

    expected_status = str(expected_goal.get("status") or "").strip()
    if expected_status in CODEX_GOAL_STATUSES:
        # Reconciliation pauses an inherited active goal before validation so
        # ownerless background work can never escape through the new chat.
        exposed_status = "paused" if expected_status == "active" else expected_status
        if str(native_goal.get("status") or "").strip() != exposed_status:
            raise CodexAppServerProtocolError(
                "the native fork did not preserve the inherited goal status",
                request_sent=True,
                safe_to_retry=False,
            )


async def reconcile_codex_thread_goal(
    manager: CodexAppServerManager,
    session_id: str,
    thread_id: str,
    *,
    new_thread: bool = False,
    force: bool = False,
    require_goal_support: bool = False,
    expected_goal: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Reconcile cached and native goal state once per provider generation.

    Existing threads are read from Codex because the local session cache may
    predate a daemon restart. A fresh rollover thread receives the previous
    objective in the paused state, so migration can never create ownerless
    background work.
    """

    generation_value = getattr(manager, "generation", 0)
    generation = generation_value if isinstance(generation_value, int) else 0
    sync_key = (id(manager), generation, thread_id)
    session = STORE.sessions.get(session_id) or {}
    if (
        expected_goal is None
        and not force
        and CODEX_GOAL_SYNC_GENERATIONS.get(session_id) == sync_key
    ):
        cached = session.get("codex_goal")
        return dict(cached) if isinstance(cached, dict) else None

    cached_goal = session.get("codex_goal")
    native_goal: dict[str, Any] | None
    try:
        if new_thread:
            objective = (
                str(cached_goal.get("objective") or "").strip()
                if isinstance(cached_goal, dict)
                else ""
            )
            if objective:
                cached_status = str(cached_goal.get("status") or "")
                migrated_status = (
                    "paused"
                    if cached_status == "active"
                    else cached_status
                    if cached_status in CODEX_GOAL_STATUSES
                    else "paused"
                )
                kwargs: dict[str, Any] = {
                    "objective": objective,
                    # Never resume a migrated goal before a local turn owns it.
                    "status": migrated_status,
                }
                token_budget = cached_goal.get("tokenBudget")
                if token_budget is None:
                    token_budget = cached_goal.get("token_budget")
                if isinstance(token_budget, int) and not isinstance(
                    token_budget, bool
                ):
                    kwargs["token_budget"] = token_budget
                native_goal = await asyncio.wait_for(
                    manager.set_thread_goal(thread_id, **kwargs),
                    timeout=CODEX_GOAL_CONTROL_TIMEOUT_SECONDS,
                )
            else:
                native_goal = None
        else:
            get_goal = getattr(manager, "get_thread_goal", None)
            if get_goal is None:
                # Test doubles and legacy managers without native goals keep
                # the old behavior. Production managers always expose this.
                if require_goal_support or expected_goal is not None:
                    raise CodexAppServerProtocolError(
                        "the native fork cannot verify the inherited goal",
                        request_sent=False,
                        safe_to_retry=False,
                    )
                CODEX_GOAL_SYNC_GENERATIONS[session_id] = sync_key
                return dict(cached_goal) if isinstance(cached_goal, dict) else None
            get_result = get_goal(thread_id)
            if not inspect.isawaitable(get_result):
                # Keep lightweight protocol test doubles compatible; a real
                # Codex manager always returns an awaitable here.
                if require_goal_support or expected_goal is not None:
                    raise CodexAppServerProtocolError(
                        "the native fork cannot verify the inherited goal",
                        request_sent=False,
                        safe_to_retry=False,
                    )
                CODEX_GOAL_SYNC_GENERATIONS[session_id] = sync_key
                return dict(cached_goal) if isinstance(cached_goal, dict) else None
            native_goal = await asyncio.wait_for(
                get_result,
                timeout=CODEX_GOAL_CONTROL_TIMEOUT_SECONDS,
            )
            if (
                isinstance(native_goal, dict)
                and str(native_goal.get("status") or "") == "active"
                and not await codex_thread_has_local_run_owner(
                    session_id,
                    thread_id,
                )
            ):
                native_goal = await asyncio.wait_for(
                    manager.set_thread_goal(thread_id, status="paused"),
                    timeout=CODEX_GOAL_CONTROL_TIMEOUT_SECONDS,
                )
    except Exception as exc:
        if not codex_goal_api_is_unsupported(exc):
            raise
        if require_goal_support:
            raise
        logger.info(
            "Codex goal reconciliation unsupported session=%s thread=%s",
            session_id,
            thread_id,
        )
        CODEX_GOAL_SYNC_GENERATIONS[session_id] = sync_key
        return dict(cached_goal) if isinstance(cached_goal, dict) else None

    if (require_goal_support or expected_goal is not None) and native_goal is None:
        raise CodexAppServerProtocolError(
            "the native fork did not preserve the inherited goal",
            request_sent=True,
            safe_to_retry=False,
        )
    if native_goal is not None and not isinstance(native_goal, dict):
        raise CodexAppServerProtocolError(
            "thread goal reconciliation returned invalid state",
            request_sent=True,
            safe_to_retry=False,
        )
    if expected_goal is not None:
        validate_inherited_codex_goal(expected_goal, native_goal)
    persisted = await persist_reconciled_codex_goal(
        session_id,
        thread_id,
        native_goal,
        allow_unbound_thread=new_thread,
    )
    if persisted:
        CODEX_GOAL_SYNC_GENERATIONS[session_id] = sync_key
    return native_goal


def build_codex_cmd(
    session_id: str,
    sess: dict[str, Any],
    prompt: str,
    manifest_path: Path,
    *,
    disable_provider_subagents: bool = False,
) -> list[str]:
    provider_id = sess.get("codex_thread_id") or (
        sess.get("session_id") if sess.get("backend") == BACKEND_CODEX else None
    )
    combined_developer_instructions = codex_thread_instructions(session_id, sess)
    model, normalized_effort, effective_service_tier = codex_runtime_settings(
        sess
    )
    cmd = [CODEX_BIN, "exec", *codex_goals_cli_args()]
    if provider_id:
        cmd.append("resume")
    if model:
        cmd.extend(["--model", model])
    if normalized_effort:
        cmd.extend(["-c", f"model_reasoning_effort={normalized_effort}"])
    if effective_service_tier:
        cmd.extend(["-c", f"service_tier={effective_service_tier}"])
    cmd.extend([
        "-c",
        f"developer_instructions={json.dumps(combined_developer_instructions, ensure_ascii=False)}",
    ])
    if disable_provider_subagents:
        # Exec fallback has no first-class tool transport on which to enforce
        # top-level ownership. Its capability lives in the process environment,
        # so provider-created child agents must be disabled at the CLI policy
        # boundary or they would inherit the same authority wholesale.
        cmd.extend([
            "-c",
            "agents.enabled=false",
            "-c",
            "agents.max_concurrent_threads_per_session=1",
        ])
    if provider_id:
        cmd.append(str(provider_id))
    cmd.append("--json")
    cmd.extend(["-c", "model_reasoning_summary=detailed"])
    cmd.extend(["--disable", "image_generation"])
    if not provider_id:
        cmd.append("--skip-git-repo-check")
    cmd.append("--dangerously-bypass-approvals-and-sandbox")
    cmd.append(prompt)
    return cmd


CODEX_GOALS_ADMIN_MAX_BODY_BYTES = 256


def codex_goals_admin_status() -> dict[str, Any]:
    enabled = bool(CODEX_GOALS_ENABLED)
    return {
        "enabled": enabled,
        "configurable": False,
        "default_enabled": bool(CODEX_GOALS_DEFAULT_ENABLED),
        "scope": "server",
        "reconfiguring": bool(CODEX_GOALS_RECONFIGURING),
        "message": "Persistent Codex goals are always available. Manage each goal in its chat.",
    }


async def pause_idle_codex_goals_before_disable() -> dict[str, int]:
    paused = 0
    fenced = 0
    def loaded_provider_goal_may_exist(session: dict[str, Any]) -> bool:
        manager = existing_codex_app_server_manager(session)
        thread_id = str(session_provider_id(session) or "").strip()
        return bool(
            manager is not None
            and thread_id
            and manager.is_thread_loaded(thread_id)
        )

    candidates = [
        session_id
        for session_id, session in list(STORE.sessions.items())
        if str(session.get("backend") or DEFAULT_BACKEND).lower() == BACKEND_CODEX
        and (
            isinstance(session.get("codex_goal"), dict)
            or bool(session.get("_codex_goal_reconciliation_required"))
            or loaded_provider_goal_may_exist(session)
        )
    ]
    for session_id in candidates:
        async with session_lifecycle_lock(session_id):
            if (
                session_id in DELETING_SESSIONS
                or session_id not in STORE.sessions
            ):
                continue
            result = await settle_idle_codex_goal_for_stop(session_id)
            if result.get("goal_paused"):
                paused += 1
            if result.get("goal_fenced"):
                fenced += 1
            current_goal = (STORE.sessions.get(session_id) or {}).get("codex_goal")
            if (
                isinstance(current_goal, dict)
                and str(current_goal.get("status") or "").lower() == "active"
            ):
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "Could not safely pause the active Codex goal in "
                        f"{session_id}; retry after stopping that chat"
                    ),
                )
    return {"paused_goal_count": paused, "fenced_goal_count": fenced}


async def get_codex_goals_admin() -> dict[str, Any]:
    return codex_goals_admin_status()


async def put_codex_goals_admin(
    req: CodexGoalsAdminRequest,
) -> dict[str, Any]:
    # Preserve the authenticated legacy route without allowing an older client
    # to disable goals for every chat or restart the shared provider manager.
    if not req.enabled:
        raise HTTPException(
            status_code=409,
            detail="Persistent Codex goals cannot be disabled server-wide. Pause individual goals in their chats.",
        )
    return codex_goals_admin_status()


def validate_codex_goal_resume_budget(
    session: dict[str, Any], req: CodexGoalRequest,
) -> None:
    if req.status != "active":
        return
    goal = session.get("codex_goal")
    if not isinstance(goal, dict):
        return
    fields = request_fields_set(req)
    if "objective" in fields and str(req.objective or "").strip() != str(goal.get("objective") or "").strip():
        return
    candidate = dict(session)
    if "time_budget_seconds" in fields:
        previous = candidate.get("codex_goal_time_budget_seconds")
        candidate["codex_goal_time_budget_seconds"] = req.time_budget_seconds
        if (
            req.time_budget_seconds is None
            or not isinstance(previous, (int, float))
            or isinstance(previous, bool)
            or req.time_budget_seconds > previous
        ):
            candidate["codex_goal_time_budget_exhausted"] = False
    if codex_goal_time_budget_is_exhausted(candidate):
        raise HTTPException(
            status_code=409,
            detail="The goal's time budget is exhausted. Increase or clear the time limit before resuming.",
        )


async def stop_codex_goal_resume(
    session_id: str,
    manager: CodexAppServerManager,
    thread_id: str,
    reservation_id: str,
) -> None:
    """Fence a failed/cancelled goal activation before releasing its owner."""
    async with RUNS.active_lock:
        active = RUNS.active.get(session_id)
        if (
            not active
            or active.get("codex_native_operation_kind") != "goal_resume"
            or str(active.get("codex_control_reservation_id") or "") != reservation_id
            or str(active.get("provider_thread_id") or "") != thread_id
        ):
            return
        active["stop_requested"] = True
        active_snapshot = dict(active)
    try:
        paused, _changed, error = await pause_active_codex_goal_for_stop(
            session_id, active_snapshot,
        )
    except Exception as exc:
        paused = False
        error = f"Could not persist the paused goal: {concise_error_message(exc)}"
    if not paused:
        try:
            await quarantine_codex_goal_thread(
                session_id, thread_id,
                reason=error or "Could not pause a failed goal resume",
            )
        except Exception as exc:
            logger.warning("failed to quarantine goal resume session=%s thread=%s: %s", session_id, thread_id, concise_error_message(exc))
    # The projector binds early turn/started notifications to this exact
    # reservation even when goal/set fails after accepting the mutation.
    async with RUNS.active_lock:
        active = RUNS.active.get(session_id)
        turn_id = str(active.get("provider_turn_id") or "") if active else ""
        owned = bool(active and str(active.get("codex_control_reservation_id") or "") == reservation_id)
        needs_interrupt = bool(owned and turn_id and not active.get("native_interrupt_sent"))
        if needs_interrupt:
            active["native_interrupt_sent"] = True
    if needs_interrupt:
        try:
            await manager.request(
                "turn/interrupt", {"threadId": thread_id, "turnId": turn_id},
                timeout=CODEX_GOAL_CONTROL_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            async with RUNS.active_lock:
                active = RUNS.active.get(session_id)
                if active and str(active.get("codex_control_reservation_id") or "") == reservation_id:
                    active["native_interrupt_sent"] = False
            try:
                await quarantine_codex_goal_thread(
                    session_id, thread_id,
                    reason=f"Could not interrupt a failed goal resume: {concise_error_message(exc)}",
                )
            except Exception as quarantine_error:
                logger.warning("failed to quarantine interrupted goal session=%s thread=%s: %s", session_id, thread_id, concise_error_message(quarantine_error))


async def quarantine_codex_goal_thread(
    session_id: str,
    thread_id: str,
    *,
    reason: str,
    run_id: str | None = None,
    clear_goal: bool = False,
) -> bool:
    """Fence a provider thread whose goal state cannot be controlled.

    Detaching the provider identity makes the next user turn start a fresh
    thread. The old thread stays in a process-local denylist so late goal
    continuations are interrupted. This is a recovery boundary, not evidence
    that a run remains live.
    """

    thread_id = str(thread_id or "").strip()
    if not thread_id:
        CODEX_GOAL_SYNC_GENERATIONS.pop(session_id, None)
        changed = False
        async with STORE.transaction():
            current = STORE.sessions.get(session_id)
            if current is None:
                return False
            goal = current.get("codex_goal")
            if clear_goal:
                if goal is not None:
                    current["codex_goal"] = None
                    changed = True
                if current.get("codex_goal_time_budget_seconds") is not None:
                    current["codex_goal_time_budget_seconds"] = None
                    changed = True
                if current.get("codex_goal_time_budget_exhausted") is not False:
                    current["codex_goal_time_budget_exhausted"] = False
                    changed = True
            elif (
                isinstance(goal, dict)
                and str(goal.get("status") or "") != "paused"
            ):
                current["codex_goal"] = {**goal, "status": "paused"}
                changed = True
            marker = {
                "thread_id": None,
                "recorded_at": now_iso(),
                "reason": compact_memory_text(reason, 1000),
            }
            current["_codex_goal_reconciliation_required"] = marker
            changed = True
            if changed:
                await STORE.save()
        return True
    CODEX_QUARANTINED_GOAL_THREADS[thread_id] = session_id
    CODEX_GOAL_SYNC_GENERATIONS.pop(session_id, None)
    should_rollover = False
    changed = False
    async with STORE.transaction():
        current = STORE.sessions.get(session_id)
        if current is None:
            return True
        current_thread_id = str(session_provider_id(current) or "").strip()
        # Never let a delayed failure from an old thread overwrite state that
        # has already been rebound to a newer provider thread.
        if current_thread_id not in {"", thread_id}:
            return True
        goal = current.get("codex_goal")
        if clear_goal:
            if goal is not None:
                current["codex_goal"] = None
                changed = True
            if current.get("codex_goal_time_budget_seconds") is not None:
                current["codex_goal_time_budget_seconds"] = None
                changed = True
            if current.get("codex_goal_time_budget_exhausted") is not False:
                current["codex_goal_time_budget_exhausted"] = False
                changed = True
        elif isinstance(goal, dict) and str(goal.get("status") or "") != "paused":
            current["codex_goal"] = {**goal, "status": "paused"}
            changed = True
        marker = {
            "thread_id": thread_id,
            "recorded_at": now_iso(),
            "reason": compact_memory_text(reason, 1000),
        }
        if current.get("_codex_goal_reconciliation_required") != marker:
            current["_codex_goal_reconciliation_required"] = marker
            changed = True
        should_rollover = current_thread_id == thread_id
        if changed:
            await STORE.save()

    if should_rollover:
        await rollover_codex_provider_session(
            session_id,
            run_id or f"goal-fence-{uuid.uuid4().hex[:12]}",
            thread_id,
            reason,
            message=(
                "Detached a Codex thread whose persistent goal could not be "
                "safely reconciled. The next turn will use a fresh thread."
            ),
        )
    logger.warning(
        "quarantined Codex goal thread session=%s thread=%s reason=%s",
        session_id,
        thread_id,
        compact_memory_text(reason, 500),
    )
    return True


async def pause_active_codex_goal_for_stop(
    session_id: str,
    active: dict[str, Any] | None,
) -> tuple[bool, bool, str | None]:
    """Pause native goal continuation before interrupting its current turn.

    Codex goals may start another provider turn immediately after an
    interruption.  A Stop operation therefore is not terminal until the goal
    is paused.  The objective remains persisted so the user can resume it.
    """
    if (
        not active
        or active.get("backend") != BACKEND_CODEX
        or active.get("transport") != CODEX_TRANSPORT_APP_SERVER
    ):
        return True, False, None

    session = STORE.sessions.get(session_id) or {}
    manager = existing_codex_app_server_manager(session)
    thread_id = str(
        active.get("provider_thread_id")
        or active.get("provider_session_id")
        or session_provider_id(session)
        or ""
    ).strip()
    if manager is None or not thread_id:
        cached_goal = session.get("codex_goal")
        if not (
            isinstance(cached_goal, dict)
            and str(cached_goal.get("status") or "") == "active"
        ) and not session.get("_codex_goal_reconciliation_required"):
            return True, False, None
        return (
            False,
            False,
            "The active Codex goal could not be paused because its provider "
            "thread is unavailable. Retry Stop.",
        )

    try:
        goal = await reconcile_codex_thread_goal(
            manager,
            session_id,
            thread_id,
            force=True,
        )
        if (
            not isinstance(goal, dict)
            or str(goal.get("status") or "") != "active"
        ):
            return True, False, None
        paused_goal = await asyncio.wait_for(
            manager.set_thread_goal(thread_id, status="paused"),
            timeout=CODEX_GOAL_CONTROL_TIMEOUT_SECONDS,
        )
        if (
            not isinstance(paused_goal, dict)
            or str(paused_goal.get("status") or "") != "paused"
        ):
            raise CodexAppServerProtocolError(
                "thread/goal/set did not return a paused goal",
                request_sent=True,
                safe_to_retry=False,
            )
    except Exception as exc:
        message = (
            "Timed out while pausing the active Codex goal. Retry Stop."
            if isinstance(exc, asyncio.TimeoutError)
            else f"Could not pause the active Codex goal: {concise_error_message(exc)}"
        )
        logger.warning(
            "Codex goal pause before stop failed session=%s thread=%s error=%s",
            session_id,
            thread_id,
            concise_error_message(exc),
        )
        return False, False, message

    # The native notification normally persists this state too, but persist
    # the direct response before interrupt so a reconnect cannot resurrect an
    # active goal from stale local state.
    await persist_reconciled_codex_goal(
        session_id,
        thread_id,
        paused_goal,
    )
    generation_value = getattr(manager, "generation", 0)
    generation = generation_value if isinstance(generation_value, int) else 0
    CODEX_GOAL_SYNC_GENERATIONS[session_id] = (id(manager), generation, thread_id)
    return True, True, None


async def settle_idle_codex_goal_for_stop(
    session_id: str,
) -> dict[str, Any]:
    """Pause or fence a goal when no local turn owns the chat."""

    session = STORE.sessions.get(session_id) or {}
    if str(session.get("backend") or DEFAULT_BACKEND) != BACKEND_CODEX:
        return {}
    thread_id = str(session_provider_id(session) or "").strip()
    cached_goal = session.get("codex_goal")
    manager = existing_codex_app_server_manager(session)
    should_check_native = bool(
        thread_id
        and manager is not None
        and manager.is_thread_loaded(thread_id)
    )
    if not should_check_native and not (
        isinstance(cached_goal, dict)
        and str(cached_goal.get("status") or "") == "active"
    ) and not session.get("_codex_goal_reconciliation_required"):
        return {}
    synthetic_active = {
        "backend": BACKEND_CODEX,
        "transport": CODEX_TRANSPORT_APP_SERVER,
        "provider_thread_id": thread_id,
        "provider_session_id": thread_id,
    }
    goal_pause_ok, goal_paused, goal_pause_error = (
        await pause_active_codex_goal_for_stop(
            session_id,
            synthetic_active,
        )
    )
    if goal_pause_ok:
        return {"goal_paused": goal_paused}
    fenced = await quarantine_codex_goal_thread(
        session_id,
        thread_id,
        reason=goal_pause_error
        or "Could not reconcile the idle Codex goal while stopping.",
    )
    return {
        "goal_paused": False,
        "goal_fenced": fenced,
        "message": (
            "No local turn was running. The stale Codex goal thread was fenced."
        ),
    }
