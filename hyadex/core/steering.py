"""Steering queued turns into running ones: native steer delivery, Force Send and requeueing."""
from __future__ import annotations

import asyncio
from contextlib import suppress
import logging.handlers
import time
from typing import Any

from hyadex.core.config import DEFAULT_BACKEND
from hyadex.core.runs import RUNS
from hyadex.core.server_logging import concise_error_message
from hyadex.core.sessions import STORE
from hyadex.core.state import LEGACY_STEERING_PREFIX, RUN_NOW_PROVIDER_HANDOFF_TIMEOUT_SECONDS
from hyadex.core.util import merged_file_ids
from hyadex.features.files import split_legacy_scoped_attachments
from hyadex.providers.authority import NativeSteerHandoffError
from hyadex.providers.registry import (
    normalize_steering_lineage,
    queued_turn_uses_retired_routing,
    strip_hyadex_provider_context,
)

logger = logging.getLogger("hyadex-server")


def turn_steering_lineage(turn: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not turn:
        return []
    lineage = normalize_steering_lineage(turn.get("steering_lineage"))
    if lineage:
        return lineage
    prompt = str(turn.get("prompt") or "")
    file_ids_value = turn.get("file_ids")
    file_ids = merged_file_ids(file_ids_value if isinstance(file_ids_value, (list, tuple)) else [])
    if not (prompt.strip() or file_ids):
        return []
    return [{"prompt": prompt, "file_ids": file_ids}]


def parse_legacy_steering_lineage(
    text: str,
    latest_file_ids: list[str] | tuple[str, ...] = (),
    *,
    recover_file_ids: bool = True,
) -> list[dict[str, Any]]:
    """Recover immutable messages from the exact legacy generated replay envelope."""
    if not text.startswith(LEGACY_STEERING_PREFIX):
        return []
    interrupted_start_marker = "[Interrupted message]\n"
    interrupted_end_marker = "\n[End interrupted message]"
    steering_start_marker = "\n\n[Steering message]\n"
    steering_end_marker = "\n[End steering message]"
    if not text.endswith(steering_end_marker):
        return []
    interrupted_start = len(LEGACY_STEERING_PREFIX)
    if not text.startswith(interrupted_start_marker, interrupted_start):
        return []
    steering_start = text.rfind(steering_start_marker)
    interrupted_end = text.rfind(interrupted_end_marker, 0, steering_start)
    if steering_start < 0 or interrupted_end < 0:
        return []
    if interrupted_end + len(interrupted_end_marker) != steering_start:
        return []

    interrupted_text = text[
        interrupted_start + len(interrupted_start_marker):interrupted_end
    ]
    interrupted_text, interrupted_file_ids = split_legacy_scoped_attachments(
        interrupted_text,
        "Interrupted message attachments",
        recover_file_ids=recover_file_ids,
    )
    nested = parse_legacy_steering_lineage(
        interrupted_text,
        interrupted_file_ids,
        recover_file_ids=recover_file_ids,
    )
    if nested:
        if interrupted_file_ids and not nested[-1].get("file_ids"):
            nested[-1]["file_ids"] = interrupted_file_ids
        lineage = nested
    else:
        lineage = [{
            "prompt": strip_hyadex_provider_context(interrupted_text),
            "file_ids": interrupted_file_ids,
        }]

    steering_text = text[
        steering_start + len(steering_start_marker):-len(steering_end_marker)
    ]
    lineage.append({
        "prompt": steering_text,
        "file_ids": merged_file_ids(latest_file_ids),
    })
    return normalize_steering_lineage(lineage)


def prepare_steered_turn(selected: dict[str, Any], interrupted: dict[str, Any] | None) -> dict[str, Any]:
    """Persist flat steering lineage while keeping the selected user text immutable."""
    turn = dict(selected)
    steering_prompt = str(
        selected.get("steering_prompt")
        if selected.get("steering_prompt") is not None
        else selected.get("prompt") or ""
    )
    selected_file_ids = list(
        selected.get("display_file_ids")
        if selected.get("display_file_ids") is not None
        else selected.get("file_ids") or []
    )
    interrupted_lineage = turn_steering_lineage(interrupted)
    if not (steering_prompt.strip() or selected_file_ids) or not interrupted_lineage:
        return turn
    steering_message = {
        "prompt": steering_prompt,
        "file_ids": merged_file_ids(selected_file_ids),
    }
    if steering_message == interrupted_lineage[-1]:
        return turn

    turn["steering_prompt"] = steering_prompt
    turn["display_prompt"] = str(
        selected.get("display_prompt")
        if selected.get("display_prompt") is not None
        else steering_prompt
    )
    turn["prompt"] = steering_prompt
    turn["steering_lineage"] = interrupted_lineage + [steering_message]
    turn["display_file_ids"] = selected_file_ids
    turn["file_ids"] = selected_file_ids
    turn["steer_interrupted_run_id"] = (interrupted or {}).get("run_id")
    turn["replays_interrupted_message"] = False
    return turn


def force_send_task_owner(task: asyncio.Task[Any] | None) -> dict[str, Any]:
    """Return bounded diagnostics for one in-memory Force Send owner."""

    if task is None:
        return {}
    started_at = getattr(task, "_hyadex_force_send_started_at", None)
    age_seconds = (
        max(0.0, time.monotonic() - float(started_at))
        if isinstance(started_at, (int, float))
        and not isinstance(started_at, bool)
        else None
    )
    return {
        "operation_id": str(
            getattr(task, "_hyadex_force_send_operation_id", "") or ""
        )
        or None,
        "queued_id": str(
            getattr(task, "_hyadex_force_send_queued_id", "") or ""
        )
        or None,
        "age_seconds": round(age_seconds, 3) if age_seconds is not None else None,
        "phase": str(
            getattr(task, "_hyadex_force_send_phase", "") or ""
        )
        or None,
        "done": task.done(),
        "cancelled": task.cancelled() if task.done() else False,
    }


def force_send_conflict_detail(
    session_id: str,
    queued_id: str,
    *,
    guard: str,
    message: str,
    action: str,
    retryable: bool,
    owner_queued_id: str | None = None,
    owner_task: asyncio.Task[Any] | None = None,
) -> dict[str, Any]:
    """Log and expose the exact in-memory guard that rejected Force Send."""

    owner = force_send_task_owner(owner_task)
    effective_owner_queued_id = (
        owner_queued_id
        or str(owner.get("queued_id") or "").strip()
        or None
    )
    logger.warning(
        "Force Send rejected session=%s queued_id=%s guard=%s "
        "owner_queued_id=%s owner_operation_id=%s owner_age_seconds=%s "
        "owner_phase=%s owner_done=%s steering=%s promoted=%s active=%s "
        "current=%s busy=%s",
        session_id,
        queued_id,
        guard,
        effective_owner_queued_id,
        owner.get("operation_id"),
        owner.get("age_seconds"),
        owner.get("phase"),
        owner.get("done"),
        session_id in RUNS.steering,
        str((RUNS.run_now.get(session_id) or {}).get("queued_id") or "")
        or None,
        session_id in RUNS.active,
        session_id in RUNS.current_turns,
        session_id in RUNS.busy,
    )
    return {
        "code": "force_send_blocked",
        "message": message,
        "action": action,
        "retryable": retryable,
        "guard": guard,
        "queued_id": queued_id,
        "owner": {
            "queued_id": effective_owner_queued_id,
            "operation_id": owner.get("operation_id"),
            "age_seconds": owner.get("age_seconds"),
            "phase": owner.get("phase"),
        },
        "state": {
            "active": session_id in RUNS.active,
            "current": session_id in RUNS.current_turns,
            "busy": session_id in RUNS.busy,
        },
    }


async def deferred_force_send_outcome(
    session_id: str,
    queued_id: str,
    error: NativeSteerHandoffError,
) -> dict[str, Any]:
    """Describe a retry-safe handoff race without turning it into HTTP 500."""
    async with RUNS.queue_lock:
        remaining = len(RUNS.queued.get(session_id) or ())
    reason = concise_error_message(error)
    return {
        "ok": False,
        "queued_id": queued_id,
        "interrupted": False,
        "deferred": True,
        "retryable": True,
        "delivery_uncertain": False,
        "replays_interrupted_message": False,
        "message": (
            f"Force Send was deferred: {reason}. "
            "The message was kept for normal queue delivery."
        ),
        "remaining": remaining,
        "superseded_queued_ids": [],
    }


def withdraw_unaccepted_native_steer(
    native_steer_queue: asyncio.Queue[dict[str, Any]],
    request: dict[str, Any],
) -> bool:
    """Atomically withdraw a provider command that no consumer accepted."""

    if request.get("phase") != "queued":
        return False
    try:
        candidate = native_steer_queue.get_nowait()
    except asyncio.QueueEmpty:
        return False
    if candidate is request:
        request["phase"] = "withdrawn"
        return True
    # This should be impossible for a maxsize-one queue, but never discard a
    # replacement command merely because an older watchdog raced it.
    with suppress(asyncio.QueueFull):
        native_steer_queue.put_nowait(candidate)
    return False


def mark_native_steer_accepted(request: dict[str, Any]) -> None:
    """Transfer a queued native command to its exact provider consumer."""

    request["phase"] = "accepted"
    owner_task = request.get("owner_task")
    if isinstance(owner_task, asyncio.Task):
        setattr(
            owner_task,
            "_hyadex_force_send_phase",
            "provider_accepted",
        )
    accepted_event = request.get("accepted_event")
    if isinstance(accepted_event, asyncio.Event):
        accepted_event.set()


async def await_native_steer_result(
    session_id: str,
    selected: dict[str, Any],
    *,
    backend: str,
    native_steer_queue: asyncio.Queue[dict[str, Any]],
    request: dict[str, Any],
    future: asyncio.Future[dict[str, Any]],
) -> dict[str, Any]:
    """Bound only pre-accept handoff without guessing post-accept safety."""

    accepted_event = request.get("accepted_event")
    if not isinstance(accepted_event, asyncio.Event):
        accepted_event = asyncio.Event()
        request["accepted_event"] = accepted_event
        if request.get("phase") != "queued":
            accepted_event.set()
    accepted_task = asyncio.create_task(accepted_event.wait())
    try:
        done, _pending = await asyncio.wait(
            {future, accepted_task},
            timeout=RUN_NOW_PROVIDER_HANDOFF_TIMEOUT_SECONDS,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if future in done or future.done():
            return future.result()
        if accepted_task in done or request.get("phase") != "queued":
            # Provider ownership is an irreversible replay boundary. Keep the
            # request owner (and its detached row) until the provider's own
            # bounded lifecycle returns an authoritative result.
            return await asyncio.shield(future)
        if withdraw_unaccepted_native_steer(native_steer_queue, request):
            raise NativeSteerHandoffError(
                "The provider did not accept Force Send before its handoff deadline",
                safe_to_requeue=True,
            )
        # The consumer removed the maxsize-one command in the same event-loop
        # tick in which it marks acceptance. If that transfer won this timeout
        # race, wait for its authoritative result instead of inferring replay.
        return await asyncio.shield(future)
    finally:
        if not accepted_task.done():
            accepted_task.cancel()
        await asyncio.gather(accepted_task, return_exceptions=True)


def native_steer_requeue_event_payload(
    session_id: str,
    selected: dict[str, Any],
    *,
    position: int,
) -> dict[str, Any]:
    """Return a normal, downgrade-safe queue event for a rejected steer."""

    display_prompt = str(
        selected.get("display_prompt")
        if selected.get("display_prompt") is not None
        else selected.get("prompt") or ""
    )
    return {
        "queued_id": selected.get("queued_id"),
        "backend": (
            selected.get("backend")
            or (STORE.sessions.get(session_id) or {}).get("backend")
            or DEFAULT_BACKEND
        ),
        "model": selected.get("model"),
        "effort": selected.get("effort"),
        "prompt": display_prompt,
        "request_prompt": str(selected.get("prompt") or ""),
        "display_prompt": selected.get("display_prompt"),
        "file_ids": list(selected.get("file_ids") or []),
        "display_file_ids": list(
            selected.get("display_file_ids")
            if selected.get("display_file_ids") is not None
            else selected.get("file_ids") or []
        ),
        "position": position,
        "purpose": selected.get("purpose"),
        "client_capabilities": list(selected.get("client_capabilities") or []),
        "message": (
            "Force Send was rejected before provider delivery; the message "
            "returned to normal queue delivery."
        ),
    }


def codex_goal_steer_selection_is_plain(selected: dict[str, Any]) -> bool:
    """User text/attachments may steer without changing runtime authority."""
    return (
        str(selected.get("prompt") or "").strip().split(maxsplit=1)[:1] != ["/mail"]
        and selected.get("skill_selection") is None
        and not selected.get("purpose")
        and not selected.get("secure_peer_route_snapshots")
        and not queued_turn_uses_retired_routing(selected)
    )
