"""Sending turns and managing the queue: post, edit, move, Run now and Stop."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

from hyadex.core.http import router
from hyadex.core.queue import (
    deferred_force_send_outcome,
    move_queued_turn,
    MoveQueuedTurnRequest,
    RunQueuedTurnNowRequest,
    unqueue_turn,
    update_queued_turn,
    UpdateQueuedTurnRequest,
)
from hyadex.core.server_logging import concise_error_message
from hyadex.core.stop import request_explicit_stop, run_queued_turn_now
from hyadex.core.turns import post_turn_with_receipt, post_turn_without_receipt
from hyadex.providers.authority import NativeSteerHandoffError
from hyadex.providers.claude.runtime import TurnRequest

router = APIRouter()


def deferred_force_send_response_for_client(
    result: dict[str, Any],
    request: RunQueuedTurnNowRequest | None,
) -> dict[str, Any]:
    """Expose the additive deferred contract only to clients that opt in."""
    if request is not None and request.accept_deferred_queue_response:
        return result
    raise HTTPException(
        status_code=409,
        detail={
            "code": "force_send_deferred",
            "message": str(
                result.get("message")
                or "Force Send was deferred and the message remains queued."
            ),
            "action": "Refresh the queue before trying Force Send again.",
            "retryable": True,
            "delivery_uncertain": False,
            "queued_id": result.get("queued_id"),
        },
    )


@router.post("/api/sessions/{session_id}/turns")
async def post_turn(session_id: str, req: TurnRequest) -> Any:
    if req.shared_chat_id is not None or req.shared_chat_request_id is not None:
        raise HTTPException(status_code=422, detail="Browser sharing attribution is no longer accepted")
    if req.client_request_id is not None:
        return await post_turn_with_receipt(session_id, req)
    return await post_turn_without_receipt(session_id, req)


@router.delete("/api/sessions/{session_id}/queue/{queued_id}")
async def delete_queued_turn(session_id: str, queued_id: str) -> dict[str, Any]:
    return await unqueue_turn(session_id, queued_id)


@router.patch("/api/sessions/{session_id}/queue/{queued_id}")
async def patch_queued_turn(session_id: str, queued_id: str, req: UpdateQueuedTurnRequest) -> dict[str, Any]:
    return await update_queued_turn(session_id, queued_id, req)


@router.post("/api/sessions/{session_id}/queue/{queued_id}/move")
async def post_move_queued_turn(session_id: str, queued_id: str, req: MoveQueuedTurnRequest) -> dict[str, Any]:
    return await move_queued_turn(session_id, queued_id, req)


@router.post("/api/sessions/{session_id}/queue/{queued_id}/run-now")
async def post_run_queued_turn_now(
    session_id: str,
    queued_id: str,
    req: RunQueuedTurnNowRequest | None = None,
) -> dict[str, Any]:
    try:
        result = await run_queued_turn_now(session_id, queued_id)
        if result.get("deferred"):
            return deferred_force_send_response_for_client(result, req)
        return result
    except NativeSteerHandoffError as exc:
        # Retry-safe races normally become deferred outcomes in the coalesced
        # operation above. Keep this defensive boundary so no future handoff
        # path can leak an expected provider-completion race as a raw 500.
        if exc.safe_to_requeue:
            result = await deferred_force_send_outcome(session_id, queued_id, exc)
            return deferred_force_send_response_for_client(result, req)
        delivery_uncertain = bool(exc.delivery_uncertain)
        raise HTTPException(
            status_code=409,
            detail={
                "code": (
                    "force_send_delivery_uncertain"
                    if delivery_uncertain
                    else "force_send_not_retryable"
                ),
                "message": (
                    "Force Send delivery could not be confirmed: "
                    f"{concise_error_message(exc)}"
                ),
                "action": (
                    "Do not retry automatically. Refresh the chat and verify "
                    "whether the message appeared."
                ),
                "retryable": False,
                "delivery_uncertain": delivery_uncertain,
                "queued_id": queued_id,
            },
        ) from exc


@router.post("/api/sessions/{session_id}/stop")
async def stop_turn_endpoint(session_id: str) -> dict[str, Any]:
    return await request_explicit_stop(session_id)
